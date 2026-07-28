"""Shared fixtures: a fake PAI backend so tests never touch the network."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# Real captured PAI payloads. Flat shape: no `tokens` object, no `model`.
NONSTREAM_BODY = json.loads(load_fixture("nonstream_plain.json"))

MODELS_BODY = {
    "models": [
        {"model": "gemma4:cloud", "name": "gemma4:cloud",
         "capabilities": ["completion", "thinking", "vision"],
         "modified_at": "2026-07-26T21:35:13.030031135+05:30", "digest": "b06ba"},
        {"model": "gemma4:26b", "name": "gemma4:26b",
         "capabilities": ["completion", "thinking", "vision"],
         "modified_at": "2026-07-26T21:30:00.000000000+05:30", "digest": "a17cf"},
    ]
}


def make_settings(**over) -> Settings:
    base = dict(
        pai_base_url="https://pai-api.test",
        pai_api_key="psai_" + "a" * 32,
        models_cache_ttl_s=300,
        allowed_models="gemma4:26b,gemma4:cloud",
    )
    base.update(over)
    return Settings(_env_file=None, **base)


class FakePai:
    """Records outbound requests and replays canned PAI responses."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.chat_status = 200
        self.chat_body: object = NONSTREAM_BODY
        self.chat_headers: dict[str, str] = {}
        self.stream_lines: list[str] | None = None
        self.models_status = 200
        self.models_body: object = MODELS_BODY

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/models":
            return httpx.Response(self.models_status, json=self.models_body)
        if request.url.path == "/api/v4/chat":
            if self.chat_status >= 400:
                return httpx.Response(self.chat_status, json=self.chat_body,
                                      headers=self.chat_headers)
            body = json.loads(request.content or b"{}")
            if body.get("stream") and self.stream_lines is not None:
                text = "".join(f"{ln}\n" for ln in self.stream_lines)
                return httpx.Response(200, text=text,
                                      headers={"content-type": "application/x-ndjson"})
            return httpx.Response(200, json=self.chat_body, headers=self.chat_headers)
        return httpx.Response(404, json={"error": "no such route"})

    @property
    def last_chat_payload(self) -> dict:
        for req in reversed(self.requests):
            if req.url.path == "/api/v4/chat":
                return json.loads(req.content or b"{}")
        raise AssertionError("no chat request recorded")

    @property
    def last_auth(self) -> str | None:
        return self.requests[-1].headers.get("authorization") if self.requests else None


@pytest.fixture
def fake_pai() -> FakePai:
    return FakePai()


@pytest.fixture
def client_factory(fake_pai):
    created: list[TestClient] = []

    def _factory(**settings_over) -> TestClient:
        transport = httpx.MockTransport(fake_pai.handler)
        http_client = httpx.AsyncClient(
            base_url="https://pai-api.test", transport=transport
        )
        app = create_app(make_settings(**settings_over), http_client=http_client)
        tc = TestClient(app, raise_server_exceptions=False)
        created.append(tc)
        return tc

    yield _factory
    for tc in created:
        tc.close()


@pytest.fixture
def client(client_factory) -> TestClient:
    return client_factory()
