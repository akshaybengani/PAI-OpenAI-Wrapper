"""HTTP surface: chat, completions, models, health, and the honest 501s."""

from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.errors import (
    HDR_IGNORED,
    HDR_RESOLVED_MODEL,
    HDR_UNSUPPORTED,
    WrapperError,
    bad_request,
    not_implemented,
)
from app.translate.messages import normalize_messages
from app.translate.params import JSON_NUDGE, apply_param_policy, wants_thinking
from app.translate.stream import StreamTranslator, translate_stream
from app.translate.usage import map_usage

router = APIRouter()

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # don't let an intermediary proxy buffer the stream
}


def _deps(request: Request):
    st = request.app.state
    return st.settings, st.pai_client, st.registry


def _degradation_headers(
    settings, ignored: list[str], unsupported: list[str], resolved: str | None = None
) -> dict[str, str]:
    headers: dict[str, str] = {}
    if resolved:
        headers[HDR_RESOLVED_MODEL] = resolved
    if settings.send_unsupported_header:
        if ignored:
            headers[HDR_IGNORED] = ",".join(sorted(set(ignored)))
        if unsupported:
            headers[HDR_UNSUPPORTED] = ",".join(sorted(set(unsupported)))
    return headers


def _model_not_found() -> WrapperError:
    # Same response for "doesn't exist" and "not entitled" — never reveal which.
    return WrapperError(
        404,
        "The model does not exist or you do not have access to it.",
        "invalid_request_error",
        "model_not_found",
        param="model",
    )


async def _build_pai_payload(body: dict[str, Any], settings, registry) -> tuple[dict, list, list]:
    ignored, unsupported = apply_param_policy(body)
    pai_model = registry.to_pai_model(body.get("model"))
    if not registry.is_allowed(pai_model):
        # Reject here rather than upstream: saves a round trip against PAI's ~15 req/min
        # cap, and returns the identical 404 the upstream 403 would have been remapped to.
        raise _model_not_found()
    messages = normalize_messages(body.get("messages"), settings.system_message_policy)

    if body.get("_json_object"):
        messages.append({"role": "user", "content": JSON_NUDGE})

    payload: dict[str, Any] = {"model": pai_model, "messages": messages}
    if wants_thinking(body):
        payload["think"] = True
    return payload, ignored, unsupported


# --------------------------------------------------------------------------- chat
@router.post("/chat/completions")
async def chat_completions(request: Request):
    settings, client, registry = _deps(request)
    try:
        body = await request.json()
    except Exception:
        raise bad_request("Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise bad_request("Request body must be a JSON object.")

    requested_model = body.get("model") or settings.default_model
    payload, ignored, unsupported = await _build_pai_payload(body, settings, registry)
    auth = request.headers.get("authorization")
    headers = _degradation_headers(settings, ignored, unsupported, payload["model"])

    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        translator = StreamTranslator(
            chunk_id=f"chatcmpl-{uuid.uuid4().hex}",
            model=requested_model,
            emit_reasoning=settings.emit_reasoning,
            include_usage=include_usage,
        )

        async def body_iter():
            try:
                lines = client.chat_stream(payload, auth)
                async for frame in translate_stream(lines, translator):
                    yield frame
            except WrapperError as exc:
                # Upstream failed before we flushed anything meaningful -> error frame.
                for frame in translator.error_frames(exc.payload["error"]["message"]):
                    yield frame

        return StreamingResponse(
            body_iter(), media_type="text/event-stream", headers={**SSE_HEADERS, **headers}
        )

    result = await client.chat(payload, auth)
    content = result.get("content") or ""
    response = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,  # echo the request, not PAI's resolved base
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": map_usage(result),
    }
    return JSONResponse(response, headers=headers)


# -------------------------------------------------------------- legacy completions
@router.post("/completions")
async def completions(request: Request):
    settings, client, registry = _deps(request)
    try:
        body = await request.json()
    except Exception:
        raise bad_request("Request body must be valid JSON.")
    if not isinstance(body, dict):
        raise bad_request("Request body must be a JSON object.")

    prompt = body.get("prompt")
    if isinstance(prompt, list):
        prompt = "\n".join(str(p) for p in prompt if p is not None)
    if not isinstance(prompt, str) or not prompt.strip():
        raise bad_request("'prompt' must be a non-empty string or array of strings.",
                          param="prompt")

    requested_model = body.get("model") or settings.default_model
    shim = {**body, "messages": [{"role": "user", "content": prompt}]}
    shim.pop("prompt", None)
    payload, ignored, unsupported = await _build_pai_payload(shim, settings, registry)
    auth = request.headers.get("authorization")
    headers = _degradation_headers(settings, ignored, unsupported, payload["model"])

    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        translator = StreamTranslator(
            chunk_id=f"cmpl-{uuid.uuid4().hex}",
            model=requested_model,
            emit_reasoning=False,
            include_usage=include_usage,
        )

        async def body_iter():
            try:
                lines = client.chat_stream(payload, auth)
                async for frame in translate_stream(lines, translator):
                    yield frame
            except WrapperError as exc:
                for frame in translator.error_frames(exc.payload["error"]["message"]):
                    yield frame

        return StreamingResponse(
            body_iter(), media_type="text/event-stream", headers={**SSE_HEADERS, **headers}
        )

    result = await client.chat(payload, auth)
    response = {
        "id": f"cmpl-{uuid.uuid4().hex}",
        "object": "text_completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "text": result.get("content") or "",
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": map_usage(result),
    }
    return JSONResponse(response, headers=headers)


# ------------------------------------------------------------------------- models
@router.get("/models")
async def list_models(request: Request):
    _settings, _client, registry = _deps(request)
    data = await registry.list_models(request.headers.get("authorization"))
    return {"object": "list", "data": data}


@router.get("/models/{model_id:path}")  # ids contain ':' and '/'
async def retrieve_model(model_id: str, request: Request):
    _settings, _client, registry = _deps(request)
    found = await registry.find(model_id, request.headers.get("authorization"))
    if not found:
        raise _model_not_found()
    return found


# ------------------------------------------------------- deliberately unsupported
@router.post("/embeddings")
async def embeddings():
    raise not_implemented("Embeddings")


@router.post("/moderations")
async def moderations():
    raise not_implemented("Moderations")


@router.post("/audio/{rest:path}")
async def audio(rest: str):
    raise not_implemented("Audio")


@router.post("/images/{rest:path}")
async def images(rest: str):
    raise not_implemented("Image generation")
