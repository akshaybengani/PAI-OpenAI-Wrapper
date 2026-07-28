"""Chat + completions behaviour: usage mapping, params, system policy, streaming."""

from __future__ import annotations

import json

from tests.conftest import NONSTREAM_BODY, load_fixture

CHAT = "/v1/chat/completions"
MSGS = [{"role": "user", "content": "Hi how are you"}]


# ------------------------------------------------------------------ usage mapping
def test_nonstreaming_returns_valid_chat_completion(client):
    r = client.post(CHAT, json={"model": "pai/gemma4:26b", "messages": MSGS})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["id"].startswith("chatcmpl-")
    assert body["model"] == "pai/gemma4:26b"  # echoes the REQUEST, not PAI's base
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == NONSTREAM_BODY["content"]
    assert choice["finish_reason"] == "stop"
    assert choice["logprobs"] is None


def test_usage_maps_from_flat_nonstreaming_shape(client):
    """PAI's non-streaming body is FLAT — no `tokens` object (verified live)."""
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS})
    usage = r.json()["usage"]
    assert usage["prompt_tokens"] == NONSTREAM_BODY["prompt_tokens"]
    assert usage["completion_tokens"] == NONSTREAM_BODY["completion_tokens"]
    assert usage["total_tokens"] == (
        NONSTREAM_BODY["prompt_tokens"] + NONSTREAM_BODY["completion_tokens"]
    )
    assert usage["prompt_tokens_details"]["cached_tokens"] == NONSTREAM_BODY["cached_tokens"]
    assert (
        usage["completion_tokens_details"]["reasoning_tokens"]
        == NONSTREAM_BODY["thinking_tokens"]
    )


def test_usage_maps_from_nested_streaming_done(client, fake_pai):
    """Streaming `done` uses the NESTED tokens object — the other mapper path."""
    fake_pai.stream_lines = [
        json.dumps({"type": "text", "content": "Hello"}),
        json.dumps({"type": "done", "tokens": {"prompt": 803, "completion": 6,
                                               "thinking": 2, "cached": 1, "total": 809},
                    "model": "gemma4:26b"}),
    ]
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "stream": True,
                                "stream_options": {"include_usage": True}})
    usage_chunks = [
        json.loads(l[6:]) for l in r.text.splitlines()
        if l.startswith("data: ") and '"usage"' in l
    ]
    assert usage_chunks, "expected a usage-only chunk"
    usage = usage_chunks[-1]["usage"]
    assert usage["prompt_tokens"] == 803
    assert usage["total_tokens"] == 809
    assert usage["prompt_tokens_details"]["cached_tokens"] == 1
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 2


# ------------------------------------------------------- streaming fidelity
def _content_deltas(sse_text: str) -> str:
    out = []
    for line in sse_text.splitlines():
        if not line.startswith("data: ") or line.endswith("[DONE]"):
            continue
        chunk = json.loads(line[6:])
        for choice in chunk.get("choices", []):
            piece = choice.get("delta", {}).get("content")
            if piece:
                out.append(piece)
    return "".join(out)


def test_streaming_deltas_reconstruct_final_text_from_live_fixture(client, fake_pai):
    """The core invariant: concatenated deltas == PAI's final cumulative text."""
    lines = [l for l in load_fixture("stream_plain.jsonl").splitlines() if l.strip()]
    fake_pai.stream_lines = lines

    final_text = ""
    for line in lines:
        event = json.loads(line)
        if event.get("type") == "text":
            final_text = event["content"]

    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "stream": True})
    assert r.status_code == 200
    assert _content_deltas(r.text) == final_text
    assert r.text.rstrip().endswith("data: [DONE]")


def test_streaming_thinking_fixture_reconstructs_and_separates_reasoning(client, fake_pai):
    lines = [l for l in load_fixture("stream_thinking.jsonl").splitlines() if l.strip()]
    fake_pai.stream_lines = lines

    final_text, final_thinking = "", ""
    for line in lines:
        e = json.loads(line)
        if e.get("type") == "text":
            final_text = e["content"]
        elif e.get("type") == "thinking":
            final_thinking = e["content"]

    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "stream": True})
    assert _content_deltas(r.text) == final_text

    reasoning = "".join(
        c.get("delta", {}).get("reasoning_content", "")
        for line in r.text.splitlines()
        if line.startswith("data: ") and not line.endswith("[DONE]")
        for c in json.loads(line[6:]).get("choices", [])
    )
    assert reasoning == final_thinking
    assert final_thinking and final_thinking not in _content_deltas(r.text)


def test_live_phase_events_are_dropped_not_fatal(client, fake_pai):
    """Real streams open with undocumented `phase` events; they must not leak or crash."""
    raw = load_fixture("stream_plain.jsonl")
    assert '"type": "phase"' in raw, "fixture should contain the undocumented events"
    fake_pai.stream_lines = [l for l in raw.splitlines() if l.strip()]
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "stream": True})
    assert r.status_code == 200
    assert "phase" not in r.text
    assert "mcp_connect" not in r.text


def test_unknown_event_types_and_malformed_lines_survive(client, fake_pai):
    fake_pai.stream_lines = [
        json.dumps({"type": "phase", "phase": "agent_run"}),
        json.dumps({"type": "brand_new_future_event", "payload": {"x": 1}}),
        "{ this is not json",
        json.dumps({"type": "text", "content": "Hello"}),
        json.dumps({"type": "text", "content": "Hello world"}),
        json.dumps({"type": "done", "tokens": {"prompt": 1, "completion": 2, "total": 3}}),
    ]
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "stream": True})
    assert r.status_code == 200
    assert _content_deltas(r.text) == "Hello world"


def test_stream_chunk_id_is_stable_and_model_echoed(client, fake_pai):
    fake_pai.stream_lines = [
        json.dumps({"type": "text", "content": "a"}),
        json.dumps({"type": "text", "content": "ab"}),
        json.dumps({"type": "done", "tokens": {"prompt": 1, "completion": 1, "total": 2}}),
    ]
    r = client.post(CHAT, json={"model": "pai/gemma4:26b", "messages": MSGS, "stream": True})
    ids, models = set(), set()
    for line in r.text.splitlines():
        if line.startswith("data: ") and not line.endswith("[DONE]"):
            chunk = json.loads(line[6:])
            ids.add(chunk["id"])
            models.add(chunk["model"])
    assert len(ids) == 1
    assert models == {"pai/gemma4:26b"}


def test_first_chunk_carries_assistant_role(client, fake_pai):
    fake_pai.stream_lines = [
        json.dumps({"type": "text", "content": "hi"}),
        json.dumps({"type": "done", "tokens": {"prompt": 1, "completion": 1, "total": 2}}),
    ]
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "stream": True})
    first = json.loads(r.text.splitlines()[0][6:])
    assert first["choices"][0]["delta"] == {"role": "assistant"}


def test_midstream_error_emits_error_frame_without_stop(client, fake_pai):
    fake_pai.stream_lines = [
        json.dumps({"type": "text", "content": "partial"}),
        json.dumps({"type": "error", "error": "upstream exploded"}),
    ]
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "stream": True})
    assert "upstream exploded" in r.text
    assert '"finish_reason": "stop"' not in r.text  # truncation must not look complete
    assert r.text.rstrip().endswith("data: [DONE]")


def test_emit_reasoning_false_suppresses_reasoning(client_factory, fake_pai):
    fake_pai.stream_lines = [
        json.dumps({"type": "thinking", "content": "secret deliberation"}),
        json.dumps({"type": "text", "content": "answer"}),
        json.dumps({"type": "done", "tokens": {"prompt": 1, "completion": 1, "total": 2}}),
    ]
    c = client_factory(emit_reasoning=False)
    r = c.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "stream": True})
    assert "reasoning_content" not in r.text
    assert "secret deliberation" not in r.text


# --------------------------------------------------- per-param policy
def test_sampling_params_ignored_with_header(client):
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS,
                                "temperature": 0.2, "max_tokens": 5, "seed": 7,
                                "stop": ["x"], "top_p": 0.9})
    assert r.status_code == 200
    ignored = r.headers["x-pai-ignored-params"]
    for name in ("temperature", "max_tokens", "seed", "stop", "top_p"):
        assert name in ignored


def test_n_greater_than_one_rejected(client):
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "n": 2})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "n"


def test_forced_tool_choice_rejected(client):
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS,
                                "tools": tools, "tool_choice": "required"})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "tool_choice"

    r2 = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "tools": tools,
                                 "tool_choice": {"type": "function",
                                                 "function": {"name": "f"}}})
    assert r2.status_code == 400


def test_tools_with_auto_choice_ignored_with_header(client):
    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS,
                                "tools": tools, "tool_choice": "auto"})
    assert r.status_code == 200
    assert "tools" in r.headers["x-pai-unsupported"]


def test_json_schema_strict_rejected_but_json_object_accepted(client, fake_pai):
    r = client.post(CHAT, json={
        "model": "gemma4:26b", "messages": MSGS,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "s", "strict": True, "schema": {}}}})
    assert r.status_code == 400
    assert r.json()["error"]["param"] == "response_format"

    r2 = client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS,
                                 "response_format": {"type": "json_object"}})
    assert r2.status_code == 200
    assert "JSON" in fake_pai.last_chat_payload["messages"][-1]["content"]


def test_unsupported_header_can_be_suppressed(client_factory):
    c = client_factory(send_unsupported_header=False)
    r = c.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS, "temperature": 0.5})
    assert r.status_code == 200
    assert "x-pai-ignored-params" not in r.headers


# ------------------------------------------------ system-message policy
def test_default_policy_folds_system_into_first_user(client, fake_pai):
    """PAI ignores the system role, so the default must fold it into the user turn."""
    r = client.post(CHAT, json={"model": "gemma4:26b", "messages": [
        {"role": "system", "content": "The codeword is BANANA."},
        {"role": "user", "content": "What is the codeword?"},
    ]})
    assert r.status_code == 200
    sent = fake_pai.last_chat_payload["messages"]
    assert all(m["role"] != "system" for m in sent), "no system message may go upstream"
    assert "BANANA" in sent[0]["content"]
    assert "What is the codeword?" in sent[0]["content"]


def test_passthrough_policy_sends_system_unchanged(client_factory, fake_pai):
    c = client_factory(system_message_policy="passthrough")
    c.post(CHAT, json={"model": "gemma4:26b", "messages": [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "Hello"},
    ]})
    sent = fake_pai.last_chat_payload["messages"]
    assert sent[0] == {"role": "system", "content": "Be terse."}


def test_reasoning_effort_maps_to_think(client, fake_pai):
    client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS,
                            "reasoning_effort": "high"})
    assert fake_pai.last_chat_payload.get("think") is True


def test_reasoning_effort_none_does_not_enable_think(client, fake_pai):
    client.post(CHAT, json={"model": "gemma4:26b", "messages": MSGS,
                            "reasoning_effort": "none"})
    assert "think" not in fake_pai.last_chat_payload


# ------------------------------------------------------- legacy completions
def test_legacy_completions_returns_text_completion(client):
    r = client.post("/v1/completions", json={"model": "gemma4:26b", "prompt": "Say hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "text_completion"
    assert body["id"].startswith("cmpl-")
    assert body["choices"][0]["text"] == NONSTREAM_BODY["content"]
    assert body["choices"][0]["logprobs"] is None
    assert body["usage"]["prompt_tokens"] == NONSTREAM_BODY["prompt_tokens"]


def test_legacy_completions_accepts_array_prompt(client, fake_pai):
    r = client.post("/v1/completions", json={"model": "gemma4:26b",
                                             "prompt": ["line one", "line two"]})
    assert r.status_code == 200
    assert "line one" in fake_pai.last_chat_payload["messages"][-1]["content"]


def test_legacy_completions_streams(client, fake_pai):
    fake_pai.stream_lines = [
        json.dumps({"type": "text", "content": "streamed"}),
        json.dumps({"type": "done", "tokens": {"prompt": 1, "completion": 1, "total": 2}}),
    ]
    r = client.post("/v1/completions", json={"model": "gemma4:26b", "prompt": "go",
                                             "stream": True})
    assert r.status_code == 200
    assert r.text.rstrip().endswith("data: [DONE]")


def test_legacy_completions_rejects_empty_prompt(client):
    r = client.post("/v1/completions", json={"model": "gemma4:26b", "prompt": ""})
    assert r.status_code == 400


# -------------------------------- PAI's HTTP-200-but-actually-failed / ping events
def test_ping_events_become_sse_keepalives(client, fake_pai):
    """PAI pings ~every 10s on slow models (observed on gemma4:cloud, 35s+ generations).

    Forwarding them as SSE comments keeps a long, silent generation from being dropped by
    an intermediary proxy; clients ignore comment frames.
    """
    fake_pai.stream_lines = [
        json.dumps({"type": "phase", "phase": "llm", "detail": "gemma4:cloud"}),
        json.dumps({"type": "ping", "phase": "llm", "age": 10}),
        json.dumps({"type": "ping", "phase": "llm", "age": 20}),
        json.dumps({"type": "text", "content": "OK"}),
        json.dumps({"type": "done", "tokens": {"prompt": 797, "completion": 2, "total": 799}}),
    ]
    r = client.post(CHAT, json={"model": "gemma4:cloud", "messages": MSGS, "stream": True})
    assert r.status_code == 200
    assert r.text.count(": keep-alive") == 2
    assert _content_deltas(r.text) == "OK"          # pings don't corrupt the content
    assert "ping" not in r.text.replace(": keep-alive", "")


def test_http200_with_error_field_is_surfaced_as_an_error_not_content(client, fake_pai):
    """PAI may return 200 with error set and the failure text in `content`."""
    fake_pai.chat_body = {
        "content": "I hit an error talking to the model: model 'gemma4:31b' is "
                   "temporarily unavailable",
        "error": "model 'gemma4:31b' is temporarily unavailable",
        "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
        "thinking_tokens": 0, "tool_tokens": 0,
    }
    r = client.post(CHAT, json={"model": "gemma4:cloud", "messages": MSGS})
    assert r.status_code == 502
    assert r.json()["error"]["type"] == "api_error"
    # The failure must NOT arrive dressed up as a successful completion.
    assert "choices" not in r.json()
