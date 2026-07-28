#!/usr/bin/env python3
"""Generate the Postman collection for pai-openai-wrapper.

Kept as a generator (rather than a hand-maintained 1000-line JSON blob) so the shared
request/test scaffolding stays consistent and the collection is cheap to regenerate when
a route changes.

    python postman/build_collection.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).parent / "pai-openai-wrapper.postman_collection.json"
ENV_OUT = Path(__file__).parent / "pai-openai-wrapper.postman_environment.json"

JSON_HEADER = [{"key": "Content-Type", "value": "application/json"}]
AUTH_HEADER = [{"key": "Authorization", "value": "Bearer {{apiKey}}"}]


def req(
    name: str,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    tests: str = "",
    description: str = "",
    headers: list | None = None,
) -> dict:
    """One Postman request item."""
    hdrs = list(headers if headers is not None else AUTH_HEADER)
    if body is not None:
        hdrs = JSON_HEADER + hdrs
    item: dict = {
        "name": name,
        "request": {
            "method": method,
            "header": hdrs,
            "url": {
                "raw": "{{baseUrl}}" + path,
                "host": ["{{baseUrl}}"],
                "path": [p for p in path.split("/") if p],
            },
            "description": description,
        },
    }
    if body is not None:
        item["request"]["body"] = {
            "mode": "raw",
            "raw": json.dumps(body, indent=2),
            "options": {"raw": {"language": "json"}},
        }
    if tests:
        item["event"] = [
            {
                "listen": "test",
                "script": {"type": "text/javascript", "exec": tests.strip().split("\n")},
            }
        ]
    return item


def folder(name: str, description: str, items: list) -> dict:
    return {"name": name, "description": description, "item": items}


# --------------------------------------------------------------------------- tests
T_OK_JSON = """
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.test("JSON body", () => pm.response.json());
"""

T_CHAT_SHAPE = """
pm.test("200 OK", () => pm.response.to.have.status(200));
const b = pm.response.json();
pm.test("object=chat.completion", () => pm.expect(b.object).to.eql("chat.completion"));
pm.test("id is chatcmpl-*", () => pm.expect(b.id).to.match(/^chatcmpl-/));
pm.test("echoes requested model", () => pm.expect(b.model).to.eql(pm.variables.replaceIn("{{model}}")));
pm.test("assistant content is a non-empty string", () => {
  pm.expect(b.choices[0].message.role).to.eql("assistant");
  pm.expect(b.choices[0].message.content).to.be.a("string").and.not.empty;
});
pm.test("finish_reason=stop, logprobs=null", () => {
  pm.expect(b.choices[0].finish_reason).to.eql("stop");
  pm.expect(b.choices[0].logprobs).to.eql(null);
});
pm.test("usage is populated for cost tracking", () => {
  pm.expect(b.usage.prompt_tokens).to.be.a("number");
  pm.expect(b.usage.completion_tokens).to.be.a("number");
  pm.expect(b.usage.total_tokens).to.be.a("number");
  pm.expect(b.usage.prompt_tokens_details.cached_tokens).to.be.a("number");
  pm.expect(b.usage.completion_tokens_details.reasoning_tokens).to.be.a("number");
});
"""

T_STREAM = """
pm.test("200 OK", () => pm.response.to.have.status(200));
const raw = pm.response.text();
pm.test("content-type is text/event-stream", () =>
  pm.expect(pm.response.headers.get("Content-Type")).to.include("text/event-stream"));
pm.test("terminates with [DONE]", () => pm.expect(raw).to.include("data: [DONE]"));
pm.test("first chunk carries the assistant role", () =>
  pm.expect(raw).to.include('"role": "assistant"'));
pm.test("emits a stop chunk", () => pm.expect(raw).to.include('"finish_reason": "stop"'));
pm.test("no PAI-internal event types leak", () => {
  ["mcp_connect", "agent_run", '"type": "phase"', '"type": "ping"'].forEach(
    (s) => pm.expect(raw).to.not.include(s));
});
// Reassemble the deltas: OpenAI deltas are incremental, PAI's upstream text is cumulative.
const text = raw.split("\\n")
  .filter((l) => l.startsWith("data: ") && !l.includes("[DONE]"))
  .map((l) => { try { return JSON.parse(l.slice(6)); } catch (e) { return null; } })
  .filter(Boolean)
  .flatMap((c) => (c.choices || []).map((ch) => (ch.delta && ch.delta.content) || ""))
  .join("");
pm.test("deltas reassemble into non-empty text", () => pm.expect(text).to.not.be.empty);
console.log("Reassembled:", text);
"""

T_STREAM_USAGE = T_STREAM + """
pm.test("include_usage yields a usage-only chunk", () => {
  const usage = raw.split("\\n")
    .filter((l) => l.startsWith("data: ") && l.includes('"usage"'))
    .map((l) => JSON.parse(l.slice(6)));
  pm.expect(usage.length).to.be.above(0);
  pm.expect(usage[usage.length - 1].usage.total_tokens).to.be.a("number");
});
"""

T_MODELS = """
pm.test("200 OK", () => pm.response.to.have.status(200));
const b = pm.response.json();
pm.test("object=list", () => pm.expect(b.object).to.eql("list"));
pm.test("has at least one model", () => pm.expect(b.data.length).to.be.above(0));
pm.test("every id carries the configured prefix", () =>
  b.data.forEach((m) => pm.expect(m.id).to.match(/^pai\\//)));
pm.test("only OpenAI-defined fields are published", () =>
  b.data.forEach((m) => pm.expect(Object.keys(m).sort()).to.eql(
    ["created", "id", "object", "owned_by"])));
pm.test("no hosted-provider name leaks unprefixed (LiteLLM price-map safety)", () =>
  b.data.forEach((m) => {
    pm.expect(m.id).to.not.match(/^gpt-/);
    pm.expect(m.id).to.not.match(/^claude-/);
  }));
"""


def t_status(code: int, extra: str = "") -> str:
    return f"""
pm.test("{code}", () => pm.response.to.have.status({code}));
const b = pm.response.json();
pm.test("OpenAI-shaped error envelope", () => {{
  pm.expect(b.error).to.be.an("object");
  pm.expect(b.error.message).to.be.a("string").and.not.empty;
  pm.expect(b.error).to.have.property("type");
  pm.expect(b.error).to.have.property("code");
  pm.expect(b.error).to.have.property("param");
}});
pm.test("no successful-completion fields present", () => pm.expect(b.choices).to.be.undefined);
{extra}
"""


# --------------------------------------------------------------------------- items
CHAT = "/v1/chat/completions"
M = "{{model}}"

ops = folder(
    "0 · Ops",
    "Liveness. /healthz probes GET /api/models upstream — never chat, so it cannot burn "
    "PAI's ~15 req/min per-key budget or trip its escalating lockout.",
    [
        req("Health check", "GET", "/healthz", headers=[], tests="""
pm.test("200 OK", () => pm.response.to.have.status(200));
const b = pm.response.json();
pm.test("status ok", () => pm.expect(b.status).to.eql("ok"));
pm.test("upstream reachable", () => pm.expect(b.upstream).to.eql("ok"));
pm.test("reports a version", () => pm.expect(b.version).to.be.a("string"));
""", description="No auth required. 503 = wrapper is up but PAI is unreachable."),
    ],
)

models = folder(
    "1 · Models",
    "Discovery. Sourced from PAI's GET /api/models, filtered to ALLOWED_MODELS (PAI "
    "advertises models your key cannot call), prefixed with MODEL_NAME_PREFIX, and cached.",
    [
        req("List models", "GET", "/v1/models", tests=T_MODELS),
        req("Retrieve model", "GET", "/v1/models/pai/gemma4:26b", tests="""
pm.test("200 OK", () => pm.response.to.have.status(200));
const b = pm.response.json();
pm.test("object=model", () => pm.expect(b.object).to.eql("model"));
pm.test("id round-trips", () => pm.expect(b.id).to.eql("pai/gemma4:26b"));
""", description="Ids contain ':' and '/' (e.g. x/flux2-klein:4b), so the route is a path param."),
        req("Retrieve model — bare id (no prefix) also accepted", "GET",
            "/v1/models/gemma4:26b", tests=T_OK_JSON),
        req("Retrieve model — unknown → 404", "GET", "/v1/models/pai/does-not-exist:1b",
            tests=t_status(404, """
pm.test("code=model_not_found", () => pm.expect(b.error.code).to.eql("model_not_found"));
""")),
    ],
)

chat = folder(
    "2 · Chat Completions",
    "The core endpoint. Streaming translates PAI's cumulative JSONL into OpenAI's "
    "incremental SSE deltas.",
    [
        req("Chat — simple", "POST", CHAT, body={
            "model": M, "messages": [{"role": "user", "content": "Hi how are you"}],
        }, tests=T_CHAT_SHAPE),

        req("Chat — with system message", "POST", CHAT, body={
            "model": M,
            "messages": [
                {"role": "system", "content": "Answer in exactly one word."},
                {"role": "user", "content": "Capital of France?"},
            ],
        }, tests=T_CHAT_SHAPE, description=(
            "PAI silently IGNORES the system role, so the wrapper folds it into the first "
            "user turn (SYSTEM_MESSAGE_POLICY=fold_into_first_user). Without that, the "
            "instruction would vanish."
        )),

        req("Chat — multi-turn history", "POST", CHAT, body={
            "model": M,
            "messages": [
                {"role": "user", "content": "What is ML?"},
                {"role": "assistant", "content": "Machine learning is a subset of AI."},
                {"role": "user", "content": "Give one example, briefly."},
            ],
        }, tests=T_CHAT_SHAPE),

        req("Chat — agent-style history (tool_calls + tool role)", "POST", CHAT, body={
            "model": M,
            "messages": [
                {"role": "user", "content": "What's the weather in Paris?"},
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "get_weather",
                                 "arguments": "{\"city\":\"Paris\"}"}}]},
                {"role": "tool", "tool_call_id": "call_1", "name": "get_weather",
                 "content": "{\"temp_c\": 18}"},
                {"role": "user", "content": "Summarise that in one line."},
            ],
        }, tests=T_CHAT_SHAPE, description=(
            "PAI accepts neither null content nor tool_call ids. The normalizer serializes "
            "the calls to text and flattens the tool turn, so replayed agent histories work."
        )),

        req("Chat — streaming", "POST", CHAT, body={
            "model": M, "stream": True,
            "messages": [{"role": "user", "content": "List three primary colors, one per line."}],
        }, tests=T_STREAM),

        req("Chat — streaming with usage", "POST", CHAT, body={
            "model": M, "stream": True, "stream_options": {"include_usage": True},
            "messages": [{"role": "user", "content": "Name two colors, comma separated."}],
        }, tests=T_STREAM_USAGE, description="LiteLLM sets include_usage to compute spend."),

        req("Chat — reasoning_effort (extended thinking)", "POST", CHAT, body={
            "model": M, "reasoning_effort": "high",
            "messages": [{"role": "user",
                          "content": "If a train covers 60km in 45 minutes, what is its speed in km/h?"}],
        }, tests=T_CHAT_SHAPE, description=(
            "Maps to PAI's think:true. Streaming surfaces it as delta.reasoning_content "
            "(set EMIT_REASONING=false to suppress). 'none' does NOT enable thinking."
        )),

        req("Chat — json_object (best effort)", "POST", CHAT, body={
            "model": M, "response_format": {"type": "json_object"},
            "messages": [{"role": "user",
                          "content": "Give me an object with keys name and city."}],
        }, tests=T_CHAT_SHAPE + """
pm.test("flagged as best-effort, not guaranteed", () =>
  pm.expect(pm.response.headers.get("x-pai-unsupported") ||
            pm.response.headers.get("x-pai-ignored-params") || "").to.be.a("string"));
""", description="A JSON instruction is injected. PAI has no native JSON mode — not guaranteed."),

        req("Chat — no model (uses DEFAULT_MODEL)", "POST", CHAT, body={
            "messages": [{"role": "user", "content": "Say OK"}],
        }, tests="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.test("falls back to DEFAULT_MODEL", () => pm.expect(pm.response.json().model).to.be.a("string"));
"""),
    ],
)

completions = folder(
    "3 · Legacy Completions",
    "OpenAI's older text-completion shape, served from the same PAI chat call.",
    [
        req("Completion — string prompt", "POST", "/v1/completions", body={
            "model": M, "prompt": "Write one short sentence about the sea.",
        }, tests="""
pm.test("200 OK", () => pm.response.to.have.status(200));
const b = pm.response.json();
pm.test("object=text_completion", () => pm.expect(b.object).to.eql("text_completion"));
pm.test("id is cmpl-*", () => pm.expect(b.id).to.match(/^cmpl-/));
pm.test("text is non-empty", () => pm.expect(b.choices[0].text).to.be.a("string").and.not.empty);
pm.test("logprobs present as null", () => pm.expect(b.choices[0].logprobs).to.eql(null));
pm.test("usage populated", () => pm.expect(b.usage.total_tokens).to.be.a("number"));
"""),
        req("Completion — array prompt", "POST", "/v1/completions", body={
            "model": M, "prompt": ["First line.", "Second line."],
        }, tests=T_OK_JSON),
        req("Completion — streaming", "POST", "/v1/completions", body={
            "model": M, "prompt": "Count from 1 to 5.", "stream": True,
        }, tests="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.test("terminates with [DONE]", () => pm.expect(pm.response.text()).to.include("data: [DONE]"));
"""),
    ],
)

degradation = folder(
    "4 · Honest Degradation",
    "PAI accepts these params and silently ignores them (HTTP 200), so the wrapper is the "
    "only thing that can tell a caller what was dropped. Harmless knobs are ignored and "
    "reported in a header; anything unsatisfiable fails loudly. Nothing ever 500s.",
    [
        req("Sampling params are ignored (header reports it)", "POST", CHAT, body={
            "model": M, "messages": [{"role": "user", "content": "Say OK"}],
            "temperature": 0.1, "top_p": 0.9, "max_tokens": 5, "seed": 42,
            "stop": ["\n"], "presence_penalty": 0.5, "frequency_penalty": 0.5,
        }, tests="""
pm.test("200 OK — accepted, not rejected", () => pm.response.to.have.status(200));
const hdr = pm.response.headers.get("x-pai-ignored-params") || "";
pm.test("x-pai-ignored-params lists the dropped params", () => {
  ["temperature", "max_tokens", "seed", "stop", "top_p"].forEach(
    (p) => pm.expect(hdr).to.include(p));
});
console.log("Ignored:", hdr);
""", description=(
            "max_tokens is genuinely unenforceable — verified live: max_tokens:5 still "
            "returned 51 tokens. Also note finish_reason is always 'stop', so a truncated "
            "answer is indistinguishable from a complete one."
        )),

        req("tools with tool_choice auto → ignored", "POST", CHAT, body={
            "model": M, "messages": [{"role": "user", "content": "What is 2+2?"}],
            "tools": [{"type": "function", "function": {
                "name": "get_weather", "description": "Get weather",
                "parameters": {"type": "object",
                               "properties": {"city": {"type": "string"}}}}}],
            "tool_choice": "auto",
        }, tests="""
pm.test("200 OK", () => pm.response.to.have.status(200));
pm.test("x-pai-unsupported flags tools", () =>
  pm.expect(pm.response.headers.get("x-pai-unsupported") || "").to.include("tools"));
pm.test("no fabricated tool_calls", () =>
  pm.expect(pm.response.json().choices[0].message.tool_calls).to.be.undefined);
""", description="PAI accepts no tools field. The model still answers in text."),

        req("n > 1 → 400", "POST", CHAT, body={
            "model": M, "n": 2, "messages": [{"role": "user", "content": "Say OK"}],
        }, tests=t_status(400, """
pm.test("param=n", () => pm.expect(b.error.param).to.eql("n"));
""")),

        req("forced tool_choice → 400", "POST", CHAT, body={
            "model": M, "messages": [{"role": "user", "content": "Weather in Paris?"}],
            "tools": [{"type": "function", "function": {
                "name": "get_weather",
                "parameters": {"type": "object",
                               "properties": {"city": {"type": "string"}}}}}],
            "tool_choice": "required",
        }, tests=t_status(400, """
pm.test("param=tool_choice", () => pm.expect(b.error.param).to.eql("tool_choice"));
"""), description="Can never be satisfied, so it fails loudly instead of returning a wrong answer."),

        req("json_schema strict → 400", "POST", CHAT, body={
            "model": M, "messages": [{"role": "user", "content": "Give me a person object."}],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "person", "strict": True,
                "schema": {"type": "object", "properties": {"name": {"type": "string"}}}}},
        }, tests=t_status(400, """
pm.test("param=response_format", () => pm.expect(b.error.param).to.eql("response_format"));
""")),

        req("Non-entitled model → 404 (never 403)", "POST", CHAT, body={
            "model": "pai/claude-sonnet-4-6",
            "messages": [{"role": "user", "content": "Hello"}],
        }, tests=t_status(404, """
pm.test("code=model_not_found", () => pm.expect(b.error.code).to.eql("model_not_found"));
pm.test("does not leak that the model exists but is gated", () => {
  pm.expect(b.error.message).to.not.include("administrator");
  pm.expect(b.error.message).to.not.include("does not have access");
});
"""), description=(
            "PAI answers 403 {detail, error:'Model not allowed'}. Surfacing that verbatim "
            "would let anyone enumerate which models a key can reach, so it is remapped to "
            "an indistinguishable 404 (so model access can't be used to probe what exists)."
        )),

        req("Empty messages → 400", "POST", CHAT, body={"model": M, "messages": []},
            tests=t_status(400)),
    ],
)

unsupported = folder(
    "5 · Not Supported (501)",
    "PAI has no backend for these. They return 501 rather than a fake response.",
    [
        req("Embeddings → 501", "POST", "/v1/embeddings",
            body={"model": M, "input": "hello world"},
            tests=t_status(501, """
pm.test("code=not_implemented", () => pm.expect(b.error.code).to.eql("not_implemented"));
"""), description="Need embeddings? Register Ollama directly in LiteLLM."),
        req("Moderations → 501", "POST", "/v1/moderations", body={"input": "hello"},
            tests=t_status(501)),
        req("Audio (TTS) → 501", "POST", "/v1/audio/speech",
            body={"model": M, "input": "hello", "voice": "alloy"}, tests=t_status(501)),
        req("Image generation → 501", "POST", "/v1/images/generations",
            body={"prompt": "a cat on a skateboard"}, tests=t_status(501),
            description="No image-capable model is entitled on this key."),
    ],
)

unregistered = folder(
    "6 · Not Implemented (404)",
    "Deliberately unregistered — a partial implementation would be worse than none.",
    [
        req("Assistants → 404", "POST", "/v1/assistants", body={"model": M},
            tests="pm.test(\"404\", () => pm.response.to.have.status(404));"),
        req("Threads → 404", "POST", "/v1/threads", body={},
            tests="pm.test(\"404\", () => pm.response.to.have.status(404));"),
        req("Responses → 404", "POST", "/v1/responses", body={"model": M, "input": "hi"},
            tests="pm.test(\"404\", () => pm.response.to.have.status(404));"),
    ],
)

collection = {
    "info": {
        "name": "PAI OpenAI Wrapper",
        "description": (
            "OpenAI-compatible API in front of a PAI deployment.\n\n"
            "Set `baseUrl` (default http://localhost:8000), `apiKey`, and `model` in the "
            "collection variables or the bundled environment. In single_key mode the "
            "wrapper swaps in its own PAI key, so `apiKey` can be any placeholder unless "
            "WRAPPER_API_KEYS is set.\n\n"
            "Runnable as a contract suite:\n"
            "    newman run postman/pai-openai-wrapper.postman_collection.json \\\n"
            "      -e postman/pai-openai-wrapper.postman_environment.json\n\n"
            "Note: Postman shows SSE responses as raw text; the streaming tests parse and "
            "reassemble the deltas themselves."
        ),
        "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    },
    "variable": [
        {"key": "baseUrl", "value": "http://localhost:8000",
         "description": "Wrapper origin, no trailing slash."},
        {"key": "apiKey", "value": "placeholder-key",
         "description": "Sent as Authorization: Bearer. Ignored in single_key mode."},
        {"key": "model", "value": "pai/gemma4:26b",
         "description": "pai/gemma4:26b (local, fast) or pai/gemma4:cloud (~40s, leaves your network)."},
    ],
    "item": [ops, models, chat, completions, degradation, unsupported, unregistered],
}

environment = {
    "name": "pai-openai-wrapper (local)",
    "values": [
        {"key": "baseUrl", "value": "http://localhost:8000", "enabled": True},
        {"key": "apiKey", "value": "placeholder-key", "enabled": True},
        {"key": "model", "value": "pai/gemma4:26b", "enabled": True},
    ],
    "_postman_variable_scope": "environment",
}

OUT.write_text(json.dumps(collection, indent=2) + "\n", encoding="utf-8")
ENV_OUT.write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")

n_req = sum(len(f["item"]) for f in collection["item"])
print(f"wrote {OUT.name}: {len(collection['item'])} folders, {n_req} requests")
print(f"wrote {ENV_OUT.name}")
