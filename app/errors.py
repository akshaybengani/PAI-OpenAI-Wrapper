"""PAI errors → OpenAI error shapes .

PAI returns a FLAT body, and it is not uniform:
  - some paths: {"error": "msg"}
  - the model-access 403: {"detail": "...", "error": "Model not allowed"}
OpenAI clients expect {"error": {"message", "type", "param", "code"}}.

The load-bearing rule: a model the key cannot access comes back from PAI as
**403 Model not allowed**. We ACTIVELY REMAP that to 404 model_not_found — PAI does not
404 it. Leaving the 403 intact would let a caller enumerate which models a key can reach.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse

# Header names we use to advertise degradation.
HDR_IGNORED = "x-pai-ignored-params"
HDR_UNSUPPORTED = "x-pai-unsupported"
HDR_RESOLVED_MODEL = "x-pai-resolved-model"

# Rate-limit headers we pass through untouched.
PASSTHROUGH_HEADERS = (
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-window",
)


def openai_error(
    message: str,
    type_: str = "invalid_request_error",
    code: str | None = None,
    param: str | None = None,
) -> dict[str, Any]:
    return {"error": {"message": message, "type": type_, "param": param, "code": code}}


class WrapperError(HTTPException):
    """An error already shaped for an OpenAI client."""

    def __init__(
        self,
        status_code: int,
        message: str,
        type_: str = "invalid_request_error",
        code: str | None = None,
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message, headers=headers)
        self.payload = openai_error(message, type_, code, param)


def bad_request(message: str, param: str | None = None) -> WrapperError:
    return WrapperError(400, message, "invalid_request_error", None, param)


def not_implemented(endpoint: str) -> WrapperError:
    return WrapperError(
        501,
        f"{endpoint} is not supported by this PAI-backed endpoint. "
        "This wrapper exposes chat and model endpoints only; "
        "register a dedicated provider for this capability.",
        "invalid_request_error",
        "not_implemented",
    )


def _pai_message(body: Any) -> str:
    """Compose a message from PAI's non-uniform flat body (reads BOTH detail and error)."""
    if isinstance(body, dict):
        detail = body.get("detail")
        err = body.get("error")
        parts = [str(p) for p in (err, detail) if p]
        if parts:
            # Prefer the more descriptive of the two, keep both when they differ.
            return parts[0] if len(parts) == 1 else f"{parts[0]}: {parts[1]}"
    if isinstance(body, str) and body.strip():
        return body.strip()
    return "Upstream request failed"


def _is_model_not_allowed(body: Any) -> bool:
    if not isinstance(body, dict):
        return False
    blob = " ".join(str(body.get(k, "")) for k in ("error", "detail")).lower()
    return "model not allowed" in blob or "does not have access to" in blob


def _is_lockout(body: Any, headers: dict[str, str]) -> bool:
    if "retry-after" in {k.lower() for k in headers}:
        return True
    if isinstance(body, dict):
        return "lockout" in str(body.get("error", "")).lower()
    return False


def map_pai_error(status: int, body: Any, headers: dict[str, str] | None = None) -> WrapperError:
    """Translate a PAI error response into an OpenAI-shaped WrapperError."""
    headers = headers or {}
    message = _pai_message(body)
    passthrough = {
        k: v for k, v in headers.items() if k.lower() in PASSTHROUGH_HEADERS
    }

    if status == 401:
        return WrapperError(401, message, "invalid_request_error", "invalid_api_key")

    if status == 403:
        # A model-access denial must be indistinguishable from "no such model".
        if _is_model_not_allowed(body):
            return WrapperError(
                404,
                "The model does not exist or you do not have access to it.",
                "invalid_request_error",
                "model_not_found",
                param="model",
            )
        # A temporary lockout carries Retry-After -> surface as 429 so clients back off.
        if _is_lockout(body, headers):
            return WrapperError(429, message, "rate_limit_error", "rate_limit_exceeded",
                                headers=passthrough or None)
        return WrapperError(403, message, "permission_error", "permission_denied")

    if status == 404:
        return WrapperError(404, message, "invalid_request_error", "not_found")

    if status == 429:
        return WrapperError(429, message, "rate_limit_error", "rate_limit_exceeded",
                            headers=passthrough or None)

    if status >= 500:
        return WrapperError(status, message, "api_error", "upstream_error")

    return WrapperError(status if 400 <= status < 500 else 502, message,
                        "invalid_request_error", None)


def error_response(exc: WrapperError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.payload,
                        headers=exc.headers or None)
