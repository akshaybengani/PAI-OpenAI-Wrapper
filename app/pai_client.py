"""Async HTTP client for the PAI backend .

Auth modes:
  single_key  — every outbound request uses the env PAI_API_KEY, whatever the caller sent.
  passthrough — the caller's inbound bearer is forwarded verbatim as the PAI key.

Retry policy: ZERO retries on chat. A retried chat double-charges PAI's token
caps and can double-execute side effects. Idempotent GETs may retry once.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from app.config import Settings
from app.errors import WrapperError, map_pai_error

CHAT_PATH = "/api/v4/chat"
MODELS_PATH = "/api/models"


class PaiClient:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._s = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.pai_base_url.rstrip("/"),
            timeout=httpx.Timeout(
                settings.request_timeout_s,
                connect=settings.upstream_connect_timeout_s,
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- auth -------------------------------------------------------------
    def outbound_key(self, inbound_authorization: str | None) -> str:
        """Resolve the PAI key to present upstream."""
        if self._s.auth_mode == "passthrough":
            token = _strip_bearer(inbound_authorization)
            if not token:
                raise WrapperError(
                    401,
                    "Missing API key. In passthrough mode the request must carry a PAI key "
                    "as 'Authorization: Bearer psai_...'.",
                    "invalid_request_error",
                    "invalid_api_key",
                )
            return token
        return self._s.pai_api_key

    def _headers(self, inbound_authorization: str | None) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.outbound_key(inbound_authorization)}",
            "Content-Type": "application/json",
        }

    # --- calls ------------------------------------------------------------
    async def list_models(self, inbound_authorization: str | None = None) -> dict[str, Any]:
        resp = await self._client.get(MODELS_PATH, headers=self._headers(inbound_authorization))
        if resp.status_code >= 400:
            raise map_pai_error(resp.status_code, _safe_json(resp), dict(resp.headers))
        return resp.json()

    async def chat(
        self, payload: dict[str, Any], inbound_authorization: str | None = None
    ) -> dict[str, Any]:
        """Non-streaming chat. No retries (FR-8)."""
        resp = await self._client.post(
            CHAT_PATH, json={**payload, "stream": False},
            headers=self._headers(inbound_authorization),
        )
        if resp.status_code >= 400:
            raise map_pai_error(resp.status_code, _safe_json(resp), dict(resp.headers))

        body = resp.json()
        # PAI can answer HTTP 200 while having actually failed: it sets a non-null `error`
        # and puts the failure text in `content` (observed when a cloud-hosted model was
        # unavailable). Returning that as an assistant message would hand the caller an
        # error string dressed up as a real answer, so surface it as an error instead.
        if isinstance(body, dict) and body.get("error"):
            raise WrapperError(
                502,
                str(body["error"]),
                "api_error",
                "upstream_error",
            )
        return body

    async def chat_stream(
        self, payload: dict[str, Any], inbound_authorization: str | None = None
    ) -> AsyncIterator[str]:
        """Streaming chat: yields raw JSONL lines. No retries (FR-8).

        Client disconnects propagate as cancellation, which closes this context manager and
        tears down the upstream request rather than leaving PAI generating.
        """
        req = self._client.build_request(
            "POST", CHAT_PATH, json={**payload, "stream": True},
            headers=self._headers(inbound_authorization),
        )
        resp = await self._client.send(req, stream=True)
        try:
            if resp.status_code >= 400:
                raw = await resp.aread()
                raise map_pai_error(resp.status_code, _safe_json_bytes(raw), dict(resp.headers))
            async for line in resp.aiter_lines():
                if line.strip():
                    yield line
        finally:
            await resp.aclose()


def _strip_bearer(value: str | None) -> str | None:
    if not value:
        return None
    parts = value.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return value.strip() or None


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return resp.text


def _safe_json_bytes(raw: bytes) -> Any:
    import json

    try:
        return json.loads(raw)
    except Exception:
        return raw.decode("utf-8", "replace")
