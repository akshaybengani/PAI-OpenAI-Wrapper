"""FastAPI application factory — wires config, client, registry, routers, middleware."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import __version__
from app.config import Settings, get_settings
from app.errors import WrapperError, error_response, openai_error
from app.logging_setup import configure_logging
from app.models_registry import ModelRegistry
from app.pai_client import PaiClient
from app.routers import router as v1_router

log = logging.getLogger("pai_wrapper")


def create_app(settings: Settings | None = None,
               http_client: httpx.AsyncClient | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await app.state.pai_client.aclose()

    app = FastAPI(title="pai-openai-wrapper", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.pai_client = PaiClient(settings, client=http_client)
    app.state.registry = ModelRegistry(settings, app.state.pai_client)

    if settings.cors_allow_origins.strip():
        origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"]
        )

    # --- errors: everything reaches the client in OpenAI's shape, never a bare 500 ---
    @app.exception_handler(WrapperError)
    async def _wrapper_error(_request: Request, exc: WrapperError):
        return error_response(exc)

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception):
        log.exception("unhandled error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=openai_error("Internal server error.", "api_error", "internal_error"),
        )

    # --- body-size cap + inbound allowlist + request log ---
    @app.middleware("http")
    async def _guard(request: Request, call_next):
        started = time.perf_counter()

        max_bytes = settings.max_request_mb * 1024 * 1024
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            return JSONResponse(
                status_code=413,
                content=openai_error(
                    f"Request body exceeds the {settings.max_request_mb} MB limit.",
                    "invalid_request_error",
                    "request_too_large",
                ),
            )

        allowlist = settings.inbound_allowlist
        if allowlist and request.url.path.startswith("/v1"):
            token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
            if token not in allowlist:
                return JSONResponse(
                    status_code=401,
                    content=openai_error(
                        "Incorrect API key provided.",
                        "invalid_request_error",
                        "invalid_api_key",
                    ),
                )

        response = await call_next(request)
        log.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            },
        )
        return response

    # --- ops ---
    @app.get("/healthz", tags=["ops"])
    async def healthz():
        """Liveness + upstream reachability.

        Deliberately probes GET /api/models, NEVER chat: PAI's per-key cap is ~15 req/min
        and three minutes at the cap triggers an escalating lockout, so a chat-based health
        check (which LiteLLM would run per registered model) would self-DoS the key.
        """
        try:
            await app.state.pai_client.list_models()
            upstream = "ok"
        except Exception as exc:  # noqa: BLE001 - health must never raise
            log.warning("healthz upstream probe failed: %s", exc)
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "upstream": "unreachable",
                         "version": __version__},
            )
        return {"status": "ok", "upstream": upstream, "version": __version__}

    app.include_router(v1_router, prefix="/v1")
    return app


# Uvicorn entrypoint (factory form, so importing this module never reads env):
#   uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT}
