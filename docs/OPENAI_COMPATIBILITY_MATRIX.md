# OpenAI ↔ PAI Compatibility Matrix

How each OpenAI endpoint/field maps onto PAI, what is faithful, what is degraded, and what is unsupported. This is the spec the translation layer implements. Scope is deliberately narrow: a translation wrapper, not a platform.

## 1. Endpoint coverage

| OpenAI endpoint | Phase | Backing PAI endpoint | Notes |
|---|---|---|---|
| `POST /v1/chat/completions` | **1 (core)** | `POST /api/v4/chat` | Streaming + non-streaming. See §2–§4. |
| `GET /v1/models` | **1** | `GET /api/models` (+ `system-genies/live`, `custom-pais`), cached | Merge base models + Genies; apply `MODEL_NAME_PREFIX`. |
| `GET /v1/models/{id:path}` | **1** | derived from merged list | **Route must use `{id:path}`** — ids contain `/` and `:`. Accept prefixed or bare. 404 if unknown. |
| `POST /v1/completions` (legacy text) | 2 | `POST /api/v4/chat` | Wrap `prompt` as user message(s); return `text_completion`. |
| `POST /v1/files` + `GET`/`{id}`/`/content`/`DELETE` | 2 | `/api/files/*` (+ wrapper index for list) | See §6. |
| `POST /v1/images/generations` | 2 best-effort | `POST /api/v4/chat` w/ image-gen Genie | Parse `image` event; absolutize URL (or fetch+encode for `b64_json`). No `edits`/`variations`. |
| `POST /v1/embeddings` | — | — | **`501` out of scope.** Register Ollama in LiteLLM directly for embeddings. |
| `POST /v1/moderations`, `/v1/audio/*` | — | — | **`501` out of scope** (no PAI backend). |
| `/v1/responses`, Assistants/Threads/Runs, Usage API | — | — | **Not planned** (see PRD §3). |

## 2. `chat/completions` request field mapping

| OpenAI field | PAI handling |
|---|---|
| `model` | Strip `MODEL_NAME_PREFIX` if present, then pass through as PAI `model` (base id, `custom_<id>`, `system-genie-<id>`). `DEFAULT_MODEL` backs unknown/omitted. Echo the **requested** string back (R-LL9). |
| `messages[]` | Normalize first — see **§2.1**. |
| `stream` | Pass through. Wrapper always consumes PAI JSONL and re-emits SSE. |
| `stream_options.include_usage` | If true, emit a final usage-only chunk from `done.tokens`. |
| `reasoning_effort` / `reasoning.*` | Any non-null/not-`"none"` → PAI `think:true`. |
| all sampling / `tools` / `response_format` / `n` etc. | See the per-param policy in **§2.2**. |
| `user` | Log only; PAI identity comes from the wrapper's key. |
| `metadata`, `service_tier`, `store` | Ignored. |

### 2.1 Message normalization (inputs → what PAI receives)
PAI V4 accepts roles `system|user|assistant|tool` and a string `content` only. Agent-framework histories must be reshaped:

| Input message | Output to PAI |
|---|---|
| `{role:"system", content}` | Per `SYSTEM_MESSAGE_POLICY` (§ PRD FR-3): `passthrough` / fold into first user / reject on Genie. |
| `{role:"developer", content}` | Treat as `system`, then apply the policy above. |
| `{role:"user"/"assistant", content:"str"}` | Pass through. |
| `content` as **parts array** | Concatenate `text` parts. `image_url`: `data:` URI → upload to `files[]` (Phase 2); http(s) URL → **reject 400** unless `IMAGE_URL_FETCH=allowlist`. |
| `{role:"assistant", content:null, tool_calls:[…]}` | `content:null` is illegal for PAI → serialize a short text summary of the calls (or drop); never send `null`. |
| `{role:"tool", tool_call_id, content}` | Flatten to `user` (or `system`) prefixed `[tool result: <name>]`; PAI has no tool-call-id. |
| empty-content message | Drop. |
| result: no non-empty user turn | **`400`** — don't send a malformed request upstream. |

### 2.2 Per-param policy (baked in; never `500`)
| Param | Behavior |
|---|---|
| `temperature`, `top_p`, `presence_penalty`, `frequency_penalty`, `seed`, `stop`, `logprobs`, `top_logprobs`, `logit_bias`, `max_tokens`/`max_completion_tokens` | **ignore** + list in `x-pai-ignored-params` (harmless / unenforceable). |
| `n > 1` | **reject `400`** — a wrong output count is a correctness bug. |
| `tools`/`functions` with `tool_choice` ∈ {`"required"`, `{…named…}`} | **reject `400`** — PAI can never satisfy it. |
| `tools`/`functions` with `tool_choice:"auto"`/absent | **ignore** + `x-pai-unsupported: tools` (model may still answer usefully). |
| `response_format: {type:"json_object"}` | best-effort: inject "respond only in JSON" system instruction + header; **not guaranteed**. |
| `response_format: {type:"json_schema", strict:true}` | **reject `400`** — cannot guarantee schema. |
| `parallel_tool_calls` | ignore. |

## 3. `chat/completions` non-streaming response mapping

PAI `{content, tokens, toolsUsed, model, duration, generatedImage}` →
```json
{
  "id": "chatcmpl-<uuid>",
  "object": "chat.completion",
  "created": <unix>,
  "model": "<echoed model>",
  "choices": [{
    "index": 0,
    "message": { "role": "assistant", "content": "<content>" },
    "finish_reason": "stop"
  }],
  "usage": {
    "prompt_tokens": tokens.prompt,
    "completion_tokens": tokens.completion,
    "total_tokens": tokens.total,
    "prompt_tokens_details": { "cached_tokens": tokens.cached },
    "completion_tokens_details": { "reasoning_tokens": tokens.thinking }
  }
}
```
- If `generatedImage` present, append a markdown image (`![prompt](abs_url)`) to `content`.
- **`finish_reason` is always `stop`.** PAI emits no length/content-filter signal, so `length` and `content_filter` are never produced — a silently truncated answer is indistinguishable from a complete one. Documented limitation.

## 4. `chat/completions` streaming translation (the critical bit)

PAI streams **JSONL** with **cumulative** `text`; OpenAI streams **SSE** with **incremental** deltas. Object `chat.completion.chunk`; each frame `data: {json}\n\n`; terminate `data: [DONE]\n\n`; `Content-Type: text/event-stream`. Chunk `id` is generated once and **identical across all chunks**; `model` echoes the **requested** id.

Algorithm:
1. Read the PAI body line-by-line: buffer partial lines, split on `\n`. **Never raise on a malformed/partial line — log + skip.**
2. First output chunk: `delta:{role:"assistant"}`.
3. Maintain **two independent accumulators** — `prev_text` and `prev_thinking`. Diff on Unicode **code points** (Python `str`; a Node reimpl must avoid UTF-16 surrogate splits).
4. On `text` (cumulative `content`): if `content` starts with `prev_text`, `delta = content[len(prev_text):]`; **else (non-prefix update) emit the whole `content` and reset** (never a negative slice). Emit `delta:{content:delta}` if non-empty; `prev_text = content`.
5. On `thinking`: same diffing against `prev_thinking` → emit `delta:{reasoning_content:delta}` (`EMIT_REASONING`, default on) or drop.
6. On `tool_call`/`tool_result`/`plan`/`chart`: **drop** from the OpenAI payload (a client can't answer PAI's auto-executed tools; emitting `tool_calls` would hang it). Optional `x-pai-events` debug side-stream behind a flag.
7. On `image`: emit `delta:{content:"![prompt](abs_url)"}`.
8. **Unknown event `type`: drop and continue** (the PAI event set is explicitly extensible — never abort). Same for unknown keys inside known events.
9. On `done`: emit a final chunk `choices:[{index:0,delta:{},finish_reason:"stop"}]`; if `include_usage`, then a usage-only chunk (empty `choices`, `usage` from `done.tokens`); then `data: [DONE]\n\n`.
10. **Keepalive:** if upstream is silent for `SSE_KEEPALIVE_S`, emit a keepalive (`: ping\n\n` or empty-delta chunk) — PAI can spend 60–100s in a tool phase emitting nothing. Set `Cache-Control: no-cache`, `X-Accel-Buffering: no`.

### 4.9 Mid-stream error (two branches)
There is no standard OpenAI "SSE error frame", so behaviour depends on whether bytes were already flushed:
- **Before the first byte** → normal HTTP error status + OpenAI error body (§5).
- **After the first byte** → emit `data: {"error":{"message":…,"type":"api_error","code":…}}\n\n` then `data: [DONE]\n\n` and close. **Do not** emit a terminal `finish_reason:"stop"` first — otherwise the client reads a truncated answer as complete. (This is what LiteLLM and the `openai` SDK both detect.)

## 5. Error mapping

Flat PAI `{ "error": "msg" }` → OpenAI nested:
```json
{ "error": { "message": "<msg>", "type": "<mapped>", "param": null, "code": "<mapped>" } }
```
| PAI | HTTP | OpenAI `type` / `code` |
|---|---|---|
| `401` unauthorized | 401 | `invalid_request_error` / `invalid_api_key` |
| `403` admin required / manual block | 403 | `permission_error` |
| `404` not found | 404 | `invalid_request_error` / `model_not_found` (if model) |
| `429` rate limit | 429 | `rate_limit_error`; **pass through** `Retry-After` + `X-RateLimit-*`. |
| `403` temporary lockout (has `Retry-After`) | 429 (remap) or 403 | Prefer surfacing as 429 so OpenAI SDKs auto-retry; keep `Retry-After`. |
| `500`/`503` | 500/503 | `api_error` |

## 6. Files field mapping

PAI upload `{ id, originalName, size, isImage, url }` → OpenAI file object:
```json
{ "id": "<id>", "object": "file", "bytes": size, "created_at": <unix>,
  "filename": "<originalName>", "purpose": "<from request>", "status": "processed" }
```
`purpose` is **stored** (OpenAI clients read it back and some validate it). Wrapper keeps a small index (id → metadata + purpose + created_at) to back `GET /v1/files` (list) and `GET /v1/files/{id}` since PAI has no list endpoint. Files uploaded implicitly for a multimodal *turn* are GC'd after the response (`TURN_FILE_RETENTION=delete`); files uploaded via `POST /v1/files` are caller-owned and never auto-deleted.

Note two distinct URL classes: `/api/generated-images/<name>` is **public/no-auth** (safe to absolutize into markdown); `/api/files/<id>/download` **requires the bearer** (a raw `<img>` would 401 → proxy via `/v1/files/{id}/content` or base64-inline).

## 7. Models field mapping

Each PAI `{model,label,provider}` → `{ id: "<prefix><model>", object:"model", created:<const/unix>, owned_by: provider }`. Genies: `{ id:"<prefix>custom_<id>" | "<prefix>system-genie-<id>", object:"model", owned_by:"pai-genie" }`. `MODEL_NAME_PREFIX` (default `pai/`) prevents `gpt-*`/`claude-*` from colliding with LiteLLM's real price map. Merge + de-dupe; cache `MODELS_CACHE_TTL_S`; serve stale on upstream failure. Inbound, accept both prefixed and bare ids.

## 8. Auth strategies (wrapper inbound → PAI outbound)

The wrapper receives `Authorization: Bearer <client-key>` and must present a PAI `psai_` key outbound. Two modes (v1):
- **`single_key` (default)**: one `PAI_API_KEY` for all traffic. Optionally gate inbound with `WRAPPER_API_KEYS`. Recommended when LiteLLM sits in front (LiteLLM already does per-team virtual keys). *Recommend whitelisting this PAI key so health/burst traffic bypasses caps.*
- **`passthrough`**: forward the client's bearer as the PAI key (client supplies `psai_...`). No env key needed. Note: `/v1/models` needs a key to reach PAI — define behaviour when unauthenticated (serve cached / `401`).

(`key_map` is cut from v1.)

## 9. Compatibility summary for popular clients

- **openai-python / openai-node** at `base_url=<wrapper>/v1`: chat (stream + non-stream) and models work; files/legacy-completions/images land in Phase 2. Sampling params ignored (header-noted); `n>1`, JSON-schema mode, and forced tool use → `400`.
- **LiteLLM** (primary): register `openai/<prefix-or-bare>`; discovery, streaming w/ `include_usage`, cost logging, `429` backoff all work. Set `drop_params: true` and disable background health checks.
- **LangChain / LlamaIndex `ChatOpenAI`**: chat + streaming work. `bind_tools`/function-calling agents **do not** (no `tools`) — fails loudly on forced tool choice.
- **Embeddings-dependent flows**: not served — register Ollama in LiteLLM directly.
