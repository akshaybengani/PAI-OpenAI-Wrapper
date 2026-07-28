"""Auth modes, error mapping, rate-limit passthrough, models, 501s, middleware."""

from __future__ import annotations

from app.translate.messages import normalize_messages
from tests.conftest import make_settings

CHAT = "/v1/chat/completions"
MSGS = [{"role": "user", "content": "hi"}]


# ------------------------------------------------------------- auth modes
def test_single_key_mode_uses_env_key_regardless_of_caller(client, fake_pai):
    client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS},
                headers={"Authorization": "Bearer whatever-the-client-sent"})
    assert fake_pai.last_auth == "Bearer psai_" + "a" * 32


def test_passthrough_mode_forwards_caller_key(client_factory, fake_pai):
    c = client_factory(auth_mode="passthrough")
    caller = "psai_" + "b" * 32
    c.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS},
           headers={"Authorization": f"Bearer {caller}"})
    assert fake_pai.last_auth == f"Bearer {caller}"


def test_passthrough_mode_requires_a_key(client_factory):
    c = client_factory(auth_mode="passthrough")
    r = c.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_inbound_allowlist_rejects_unknown_key(client_factory):
    c = client_factory(wrapper_api_keys="sk-good,sk-also-good")
    bad = c.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS},
                 headers={"Authorization": "Bearer sk-bad"})
    assert bad.status_code == 401
    ok = c.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS},
                headers={"Authorization": "Bearer sk-good"})
    assert ok.status_code == 200


# --------------------------------------------------------------- model-access 403 -> 404
def test_model_not_allowed_403_becomes_404_model_not_found(client, fake_pai):
    """PAI 403s a model the key can't reach; leaving it 403 would be an existence oracle."""
    fake_pai.chat_status = 403
    fake_pai.chat_body = {
        "detail": "Your API key does not have access to 'llama3.2:latest'. "
                  "Contact your administrator to request access.",
        "error": "Model not allowed",
    }
    r = client.post(CHAT, json={"model": "llama3.2:latest", "messages": MSGS})
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "model_not_found"
    # Must not disclose that the model exists but is gated.
    assert "administrator" not in err["message"]
    assert "does not have access" not in err["message"]


def test_generic_403_stays_permission_error(client, fake_pai):
    fake_pai.chat_status = 403
    fake_pai.chat_body = {"error": "Admin access required"}
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS})
    assert r.status_code == 403
    assert r.json()["error"]["type"] == "permission_error"


# ------------------------------------------------------- rate limiting
def test_429_passes_through_with_rate_limit_headers(client, fake_pai):
    fake_pai.chat_status = 429
    fake_pai.chat_body = {"error": "rate limit exceeded",
                          "limit_hit": "key:minute:requests", "retry_after_seconds": 23}
    fake_pai.chat_headers = {"Retry-After": "23", "X-RateLimit-Limit": "15",
                             "X-RateLimit-Remaining": "0", "X-RateLimit-Window": "minute",
                             "X-RateLimit-Reset": "1764594823"}
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS})
    assert r.status_code == 429
    assert r.json()["error"]["type"] == "rate_limit_error"
    assert r.headers["retry-after"] == "23"
    assert r.headers["x-ratelimit-limit"] == "15"
    assert r.headers["x-ratelimit-remaining"] == "0"


def test_403_lockout_remaps_to_429_so_clients_back_off(client, fake_pai):
    fake_pai.chat_status = 403
    fake_pai.chat_body = {"error": "temporary lockout", "reason": "auto:burst-3min",
                          "retry_after_seconds": 3540}
    fake_pai.chat_headers = {"Retry-After": "3540", "X-RateLimit-Reset": "1764598360"}
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS})
    assert r.status_code == 429
    assert r.headers["retry-after"] == "3540"


def test_401_maps_to_invalid_api_key(client, fake_pai):
    fake_pai.chat_status = 401
    fake_pai.chat_body = {"error": "Unauthorized"}
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_api_key"


def test_upstream_500_maps_to_api_error(client, fake_pai):
    fake_pai.chat_status = 500
    fake_pai.chat_body = {"error": "Failed to save"}
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS})
    assert r.status_code == 500
    assert r.json()["error"]["type"] == "api_error"


# ----------------------------------------------------- models
def test_models_list_is_prefixed_and_openai_shaped(client):
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    ids = [m["id"] for m in body["data"]]
    assert ids == ["pai/gemma4:cloud", "pai/gemma4:26b"]
    entry = body["data"][0]
    assert set(entry) == {"id", "object", "created", "owned_by"}  # no PAI extras leak
    assert entry["object"] == "model"


def test_prefixed_and_bare_model_ids_resolve_identically(client, fake_pai):
    client.post(CHAT, json={"model": "pai/gemma4:26b", "messages": MSGS})
    prefixed = fake_pai.last_chat_payload["model"]
    client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS})
    bare = fake_pai.last_chat_payload["model"]
    assert prefixed == bare == "gemma4:26b"


def test_model_retrieval_handles_slash_and_colon_ids(client_factory, fake_pai):
    fake_pai.models_body = {"models": [{"model": "x/flux2-klein:4b",
                                        "modified_at": "2026-07-01T00:00:00+00:00"}]}
    client = client_factory(allowed_models="")  # no restriction for this shape test
    r = client.get("/v1/models/pai/x/flux2-klein:4b")
    assert r.status_code == 200
    assert r.json()["id"] == "pai/x/flux2-klein:4b"


def test_unknown_model_retrieval_is_404(client):
    r = client.get("/v1/models/pai/nope:1b")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_models_are_cached_then_served_stale_on_upstream_failure(client, fake_pai):
    first = client.get("/v1/models")
    assert first.status_code == 200
    calls = sum(1 for r in fake_pai.requests if r.url.path == "/api/models")

    client.get("/v1/models")  # within TTL -> no new upstream call
    assert sum(1 for r in fake_pai.requests if r.url.path == "/api/models") == calls

    fake_pai.models_status = 500
    fake_pai.models_body = {"error": "down"}
    stale = client.get("/v1/models")
    assert stale.status_code == 200
    assert stale.json() == first.json()  # stale, not an error


# ------------------------------------------------------ out of scope
def test_unsupported_endpoints_return_501(client):
    for path, payload in (
        ("/v1/embeddings", {"model": "m", "input": "x"}),
        ("/v1/moderations", {"input": "x"}),
        ("/v1/audio/speech", {"model": "m", "input": "x"}),
        ("/v1/images/generations", {"prompt": "x"}),
    ):
        r = client.post(path, json=payload)
        assert r.status_code == 501, path
        assert r.json()["error"]["code"] == "not_implemented"


def test_assistants_and_responses_are_not_registered(client):
    for path in ("/v1/assistants", "/v1/threads", "/v1/responses"):
        assert client.post(path, json={}).status_code == 404


# ---------------------------------------------------------------- healthz
def test_healthz_probes_models_never_chat(client, fake_pai):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["upstream"] == "ok"
    assert any(rq.url.path == "/api/models" for rq in fake_pai.requests)
    assert not any(rq.url.path == "/api/v4/chat" for rq in fake_pai.requests)


def test_healthz_reports_503_when_upstream_down(client, fake_pai):
    fake_pai.models_status = 503
    fake_pai.models_body = {"error": "down"}
    r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"


# ------------------------------------------------- message normalization
def test_normalizes_assistant_tool_calls_without_null_content():
    out = normalize_messages([
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": None,
         "tool_calls": [{"type": "function",
                         "function": {"name": "get_weather",
                                      "arguments": '{"city":"Paris"}'}}]},
        {"role": "tool", "tool_call_id": "call_1", "name": "get_weather",
         "content": '{"temp":18}'},
        {"role": "user", "content": "and tomorrow?"},
    ])
    assert all(isinstance(m["content"], str) and m["content"] for m in out)
    assert all(m["role"] in {"user", "assistant"} for m in out)
    assert any("get_weather" in m["content"] for m in out)
    assert any("tool result" in m["content"] for m in out)


def test_developer_role_becomes_system_content():
    out = normalize_messages([
        {"role": "developer", "content": "Be brief."},
        {"role": "user", "content": "Explain gravity."},
    ])
    assert all(m["role"] != "system" for m in out)
    assert "Be brief." in out[0]["content"]


def test_empty_messages_and_contentless_history_rejected():
    import pytest

    from app.errors import WrapperError

    with pytest.raises(WrapperError) as e1:
        normalize_messages([])
    assert e1.value.status_code == 400

    with pytest.raises(WrapperError) as e2:
        normalize_messages([{"role": "assistant", "content": ""}])
    assert e2.value.status_code == 400


def test_multimodal_image_part_rejected_not_silently_dropped():
    import pytest

    from app.errors import WrapperError

    with pytest.raises(WrapperError) as e:
        normalize_messages([{"role": "user", "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ]}])
    assert e.value.status_code == 400


# ---------------------------------------------------------------- middleware
def test_oversized_body_returns_413(client):
    big = "x" * (2 * 1024 * 1024)
    r = client.post(CHAT, json={"model": "gemma4:26b",
                                "messages": [{"role": "user", "content": big}]},
                    headers={"content-length": str(11 * 1024 * 1024)})
    assert r.status_code == 413


def test_default_model_used_when_model_omitted(client, fake_pai):
    r = client.post(CHAT, json={"messages": MSGS})
    assert r.status_code == 200
    assert fake_pai.last_chat_payload["model"] == make_settings().default_model


def test_cors_headers_present_only_when_configured(client_factory):
    plain = client_factory()
    r1 = plain.get("/v1/models", headers={"Origin": "https://app.test"})
    assert "access-control-allow-origin" not in r1.headers

    corsed = client_factory(cors_allow_origins="https://app.test")
    r2 = corsed.get("/v1/models", headers={"Origin": "https://app.test"})
    assert r2.headers.get("access-control-allow-origin") == "https://app.test"


def test_malformed_json_body_is_400_not_500(client):
    r = client.post(CHAT, content=b"{not json", headers={"content-type": "application/json"})
    assert r.status_code == 400


# ------------------------------------------- model allowlist (only entitled models)
def test_models_list_only_advertises_allowed_models(client_factory, fake_pai):
    """PAI advertises every model the deployment knows; the key may call only some.

    Publishing the full list would make LiteLLM register models that 404 on first use.
    """
    fake_pai.models_body = {"models": [
        {"model": "gemma4:26b", "modified_at": "2026-07-01T00:00:00+00:00"},
        {"model": "gemma4:cloud", "modified_at": "2026-07-01T00:00:00+00:00"},
        {"model": "claude-sonnet-4-6", "modified_at": "2026-07-01T00:00:00+00:00"},
        {"model": "gpt-4.1-nano", "modified_at": "2026-07-01T00:00:00+00:00"},
        {"model": "llama3.2:latest", "modified_at": "2026-07-01T00:00:00+00:00"},
    ]}
    c = client_factory(allowed_models="gemma4:26b,gemma4:cloud")
    ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    assert ids == ["pai/gemma4:26b", "pai/gemma4:cloud"]
    # The hosted-provider names must not leak into LiteLLM's price-map namespace.
    assert not any("claude" in i or "gpt-" in i for i in ids)


def test_non_allowed_model_is_404_without_calling_upstream(client_factory, fake_pai):
    c = client_factory(allowed_models="gemma4:26b,gemma4:cloud")
    before = len([r for r in fake_pai.requests if r.url.path == "/api/v4/chat"])
    r = c.post(CHAT, json={"model": "claude-sonnet-4-6", "messages": MSGS})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
    after = len([r for r in fake_pai.requests if r.url.path == "/api/v4/chat"])
    assert after == before, "must not spend an upstream call on a known-disallowed model"


def test_both_allowed_models_are_usable(client_factory, fake_pai):
    c = client_factory(allowed_models="gemma4:26b,gemma4:cloud")
    for model in ("pai/gemma4:26b", "pai/gemma4:cloud", "gemma4:26b", "gemma4:cloud"):
        r = c.post(CHAT, json={"model": model, "messages": MSGS})
        assert r.status_code == 200, model
        assert fake_pai.last_chat_payload["model"] == model.removeprefix("pai/")


def test_empty_allowlist_means_no_restriction(client_factory, fake_pai):
    c = client_factory(allowed_models="")
    r = c.post(CHAT, json={"model": "anything:1b", "messages": MSGS})
    assert r.status_code == 200
