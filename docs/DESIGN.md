# Design Notes — pai-openai-wrapper

**Status:** Implemented. This records the requirements, decisions, and
verification plan the code was built against.
**Date:** 2026-07-27
**Owner:** Akshay Bengani
**Companion docs:** [PAI_API_INVENTORY.md](PAI_API_INVENTORY.md) · [OPENAI_COMPATIBILITY_MATRIX.md](OPENAI_COMPATIBILITY_MATRIX.md)

---



## 1. Summary

A **self-hostable, Dockerized** stateless HTTP service that exposes an **OpenAI-compatible API surface** (`/v1/...`) and translates each request into calls against a **PAI deployment** (`thepsi.com` PAI backend), translating responses back into OpenAI shapes — including SSE streaming, models, files, and best-effort image generation.

**Primary target consumer: [LiteLLM](https://docs.litellm.ai/).** The wrapper is designed first and foremost to be registered in LiteLLM as an `openai`-compatible provider (`model: openai/<pai-model-or-genie>`, `api_base: http://<host>:8000/v1`), so that LiteLLM's routing, key management, cost tracking, and fallbacks work against PAI with no bespoke integration. Because LiteLLM is itself the universal OpenAI-compatible gateway, hitting its bar also makes the wrapper work for the broader ecosystem (`openai` SDKs, LangChain `ChatOpenAI`, LlamaIndex, Continue, Cursor, `curl`) — all by only changing `base_url` + key, **no client code changes**.

**Final deliverable:** a Docker image + `docker-compose.yml` + `.env` where the operator supplies `PAI_BASE_URL` and `PAI_API_KEY` (plus options), runs `docker compose up`, and gets an OpenAI endpoint that LiteLLM can point at.

## 2. Problem & motivation

- PAI has a rich but **bespoke** API: two endpoint families (`/api/...` legacy SSE, `/api/v4/...` JSONL), Genie-based model resolution, cumulative-text streaming, flat error shapes, and server-side (not caller-supplied) tools.
- The entire ecosystem of AI tooling assumes the **OpenAI wire format**. Integrating PAI today means writing a custom client per language/tool.
- A thin, well-specified compatibility layer unlocks that ecosystem instantly and centralizes the translation logic (auth, streaming diff, error mapping) in one place instead of duplicating it in every consumer.

**This is a translation wrapper, nothing more.** It makes PAI speak OpenAI on the wire. It does not add features PAI lacks, store state, or become a platform. When in doubt, do less.

### Goals
1. **Drop-in OpenAI compatibility** for the surface LiteLLM actually uses: `chat/completions` (stream + non-stream) and `models`. Everything else is secondary.
2. **Faithful streaming**: convert PAI's cumulative JSONL into OpenAI incremental SSE deltas correctly, robustly, and forward-compatibly.
3. **Config-only deployment**: one container, all inputs (`PAI_BASE_URL`, `PAI_API_KEY`, tuning flags) from `.env` via `docker-compose`.
4. **Honest degradation**: unsupported OpenAI features fail predictably (clear `400`/`501`, or a documented no-op with a response header) — **never silently wrong, never a `500`**.
5. **Pass-through of operational signals**: rate-limit headers (`429`/`Retry-After`/`X-RateLimit-*`) surfaced so LiteLLM/SDK backoff works.

### Non-goals (v1) — explicitly out of scope
- **Anything PAI has no backend for**: embeddings, audio (TTS/STT), moderations. Return `501`. *We do not build an Ollama embeddings proxy* — operators who need embeddings register Ollama in LiteLLM directly (documented in the README). (Decision D-3.)
- **Assistants / Threads / Runs, and the Responses API.** Dropped entirely — Assistants is deprecated upstream and the mapping fights PAI's per-user Genie cap. Not "phase 3, optional"; just out. (Decision D-8.)
- **Caller-supplied function/tool calling.** PAI cannot accept it (see §6); the wrapper rejects the cases it could never satisfy and no-ops the rest.
- **`temperature`/sampling via the legacy endpoint.** The legacy `/api/chat` is a *different engine* (no skills/MCP/tools, SSE not JSONL). Not worth a second translator for one knob. (Decision D-4.)
- Hosting or changing PAI; conversation-state storage; billing/quota logic beyond passing PAI's own through.

## 4. Personas & use cases

- **Platform/LLM-ops engineer (primary)** who runs **LiteLLM** as their org's model gateway and wants to add PAI as just another backend model — register `openai/<pai-model>` with an `api_base` and let LiteLLM handle keys, routing, budgets, fallbacks, and cost logs.
- **App developer** who already wrote against the `openai` SDK and wants to point it at PAI (`base_url=".../v1"`) directly (bypassing LiteLLM).
- **Tooling users** (Continue, Cursor, Open WebUI, Chatbox) that only accept an OpenAI base URL + key.
- **Data/RAG engineers** who need chat + files; will hit the embeddings gap (documented).

## 5. Scope — endpoint matrix (v1)

Full detail in [`OPENAI_COMPATIBILITY_MATRIX.md`](OPENAI_COMPATIBILITY_MATRIX.md). Summary:

| Endpoint | Phase | Backing PAI call |
|---|---|---|
| `POST /v1/chat/completions` | **1 (core)** | `POST /api/v4/chat` |
| `GET /v1/models`, `GET /v1/models/{id:path}` | **1** | `GET /api/models` (cached, single source) |
| `GET /healthz` | **1** | `GET /api/models` (never chat — see P0-3 / §10) |
| `POST /v1/completions` (legacy text) | **1** | `POST /api/v4/chat` |
| `POST /v1/files`, `GET`, `DELETE`, `/content` | 2 | `/api/files/*` (+ wrapper index for list) |
| `POST /v1/images/generations` | 2 candidate | needs an image-capable model (none today) |
| `POST /v1/embeddings`, `/v1/moderations`, `/v1/audio/*` | — | `501` (out of scope) |

The `{id:path}` on models retrieval is required: PAI model ids contain `/` and `:` (e.g. `x/flux2-klein:4b`), which a plain path param won't match.

## 5.1 LiteLLM integration (primary target — design contract)

LiteLLM treats any `openai/`-prefixed model as an OpenAI-compatible backend and appends `/chat/completions` to `api_base`. The wrapper must satisfy LiteLLM's concrete expectations:

### How an operator wires it (LiteLLM Proxy `config.yaml`)
```yaml
model_list:
  - model_name: pai-gemma4              # the alias LiteLLM callers use
    litellm_params:
      model: openai/pai/gemma4:26b      # openai/ = OpenAI-compatible; pai/ = our prefix
      api_base: http://pai-openai-wrapper:8000/v1
      api_key: os.environ/PAI_WRAPPER_KEY
  - model_name: pai-gemma4-cloud
    litellm_params:
      model: openai/pai/gemma4:cloud    # any base model the key allows
      api_base: http://pai-openai-wrapper:8000/v1
      api_key: os.environ/PAI_WRAPPER_KEY

litellm_settings:
  drop_params: true                     # let LiteLLM strip params PAI can't take
```
(Same three params — `model=openai/...`, `api_base`, `api_key` — when using the LiteLLM Python SDK directly.)

### Requirements this imposes on the wrapper
- **R-LL1 · `/v1/chat/completions` exactness.** Standard request/response + SSE streaming ending in `data: [DONE]`. This is LiteLLM's hot path.
- **R-LL2 · `/v1/models` discovery.** LiteLLM (and its `/model/info`, admin UI, and auto-population) reads this; it must list the base models the key allows.
- **R-LL3 · Usage for cost tracking.** LiteLLM computes spend from `usage`. Non-streaming must always include `usage`; streaming must honor `stream_options.include_usage` (LiteLLM sets it) and emit the final usage-only chunk. Map PAI `done.tokens` → `prompt_tokens`/`completion_tokens`/`total_tokens` (+ `prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens`).
- **R-LL4 · Model naming / no price-map collisions.** PAI exposes `gpt-4.1-nano` and `claude-sonnet-4-6` verbatim. If those reach LiteLLM as-is, LiteLLM prices them against its **real** OpenAI/Anthropic price map and `gpt-*`-special-casing tools misbehave. → `/v1/models` applies `MODEL_NAME_PREFIX` (recommend `pai/`) so ids are unambiguous; inbound the wrapper accepts **both** prefixed and bare ids. Operators still set per-model cost in LiteLLM `model_info` if they want spend numbers (most PAI models are $0 local).
- **R-LL5 · Health checks must not burn the rate budget.** ⚠️ The per-key cap is **15 req/min**, and 3 consecutive minutes at the cap triggers a **1-hour `403` lockout**. LiteLLM's background `/health` hits *every* registered model — chat-based health checks would self-DoS the key. → `/healthz` and health probing use **`GET /api/models` (unmetered CRUD), never chat.** Document `disable_background_health_checks: true` + a long `health_check_interval`, and recommend **whitelisting** the wrapper's PAI key (whitelisted keys bypass all caps).
- **R-LL6 · Param tolerance.** Never `500` on unknown/unsupported OpenAI params. Apply the per-param policy table (§7 FR-7 / matrix §2.2). Recommend operators also set `drop_params: true`.
- **R-LL7 · Error + retry semantics.** OpenAI-shaped errors with correct status codes so LiteLLM's retry/fallback/cooldown works; **pass through `429` + `Retry-After` + `X-RateLimit-*`**, and remap PAI `403 temporary lockout` (has `Retry-After`) → `429` so LiteLLM backs off instead of cooling the deployment down permanently.
- **R-LL8 · Auth.** LiteLLM sends the configured `api_key` as `Authorization: Bearer <key>`. In `single_key` mode the wrapper swaps it for `PAI_API_KEY`; in `passthrough` mode operators put a real `psai_...` in the LiteLLM config.
- **R-LL9 · Stable echoed model.** Echo the **requested** model string in the response `model` (LiteLLM keys logging/cost off it), not PAI's resolved base; surface the resolved value in `x-pai-resolved-model`. Streaming chunk `id` is identical across all chunks of one response.
- **R-LL10 · Known no-ops surfaced.** `tools`/function calling, `n>1`, JSON-schema mode can't work through PAI — documented so operators don't route tool-calling agents at PAI models (§6 constraint 1, §7 FR-7).

## 6. Key constraints discovered in research (must-read)

These shape the whole design and are documented so expectations are correct:

1. **No caller-supplied tools / function calling.** PAI's tools (`use_skill`, `read_url`, `generate_image`, `web_search`, plans, charts, MCP) are **fixed server-side and enabled per-Genie**. The V4 chat body accepts *no* `tools` field. → OpenAI `tools`/`tool_choice`/`functions` cannot be honored; tool availability is a property of the **target Genie**, chosen via the `model` field.
2. **Cumulative streaming.** PAI `text`/`thinking` events carry the **full accumulated text**, not deltas. The wrapper must diff to produce OpenAI deltas. (JSONL over chunked HTTP — *not* SSE; do not use `EventSource` upstream.)
3. **Limited sampling controls.** V4 chat accepts only `model`, `messages`, `stream`, `think`, `files`. `temperature` exists **only** on legacy `/api/chat` via `options`. `max_tokens`, `n>1`, `stop`, `seed`, `logprobs`, `logit_bias`, penalties, and `response_format`/JSON-mode have **no** PAI equivalent.
4. **No embeddings / audio / moderations** endpoints in PAI.
5. **Image generation is chat-embedded**, requires a Genie with `enableImageGeneration:true`, and returns a **relative URL** (never base64).
6. **Rate limits are enforced today** (per-key AND per-user) despite one doc page claiming otherwise. Token limits are credited **post-round**, so the wrapper cannot reliably pre-reject on token budget; it must pass `429`/`403`+`Retry-After`+`X-RateLimit-*` through.
7. **Auth**: PAI uses `Authorization: Bearer psai_<32hex>`. The wrapper holds/forwards this (three modes — §9).
8. **Flat error shape** `{ "error": "msg" }` must be re-nested into OpenAI's `{ "error": { message, type, param, code } }`.
9. **No file-list endpoint** in PAI → wrapper keeps a small index for `GET /v1/files` (Phase 2).
10. **System-message collision is UNSPECIFIED by PAI and must be verified.** Genies carry their own `systemPrompt`; PAI docs never say what happens when the caller *also* sends a `system` message (append? replace? ignore?). Every LangChain/LiteLLM/Continue request sends one — so this decides basic usability. Verify live per model class (base vs `custom_*` vs `system-genie-*`) before building the translator; expose `SYSTEM_MESSAGE_POLICY` (§9). **(Open verification O-1.)**
11. **`finish_reason` is always `stop`.** PAI emits no length/content-filter signal, so `length` and `content_filter` are never produced — silent truncation is indistinguishable from normal completion. Documented, not fixable.
12. **Per-user Genie cap (~5 Custom Genies/user — unverified).** Since a Genie is the *only* way to select a tool/capability profile, a single-key deployment can expose at most ~5 custom capability profiles (plus System Genies). Caps how many distinct "tool-enabled models" the wrapper can offer. **(Open verification O-2.)**

Phase-1 (core) FRs are FR-1…FR-6 and FR-9. FR-7-files/FR-8-images are Phase 2.

### FR-1 Chat completions (core)
- Accept the OpenAI `chat/completions` body; map per matrix §2.
- Non-streaming: single `chat.completion` with `usage` from PAI `done.tokens` (incl. `cached_tokens`, `reasoning_tokens`).
- Streaming: SSE `chat.completion.chunk` deltas via the diff algorithm (matrix §4), `role` first, `finish_reason:"stop"` last, optional usage-only chunk when `include_usage`, terminated by `data: [DONE]`.
- Map `reasoning_effort`/`reasoning.*` (non-null) → PAI `think:true`.
- Echo the **requested** model (R-LL9); stable chunk `id`.

### FR-2 Message normalization (core — its own module + test suite)
Agent frameworks replay histories PAI can't take verbatim. Before sending, normalize per matrix §2.1:
- `assistant` message with `content:null` + `tool_calls[]` → serialize the tool calls into a text summary (or drop), never send `null` content.
- `tool` role message (`{tool_call_id, content}`) → flatten to a `user` (or `system`) message prefixed `[tool result: <name>]`; PAI has no tool-call-id notion.
- `developer` role → `system` (then apply FR-3 policy).
- Drop empty-content messages; collapse nothing else silently.
- Guarantee a non-empty final user turn. If normalization empties the array → `400` (don't send a malformed request upstream).

### FR-3 System-message policy (core)
- `SYSTEM_MESSAGE_POLICY`: **`fold_into_first_user` (DEFAULT — required, PAI ignores the `system` role)** or `passthrough`. (`reject_on_genie` was removed with Genie scope.)

### FR-4 Models
- Read `GET /api/models` (**single source** — Genie sources removed). Apply `MODEL_NAME_PREFIX`. **Cache** the merged list (`MODELS_CACHE_TTL_S`, default 300; serve stale on upstream failure). `GET /v1/models/{id:path}` retrieves one (accepts prefixed or bare); `404` otherwise.

### FR-5 Streaming translator (core — the crux)
- **Two independent accumulators** for `text` and `thinking`; diff each against its own `prev`. Diff on Unicode code points (Python `str`).
- **Non-prefix fallback**: if a cumulative update doesn't start with `prev`, emit the whole new value and reset (never emit a negative slice).
- `thinking` deltas → `delta.reasoning_content` (de-facto extension; `EMIT_REASONING`, default on).
- **Forward-compatible**: default-drop unknown event `type`s and continue; never raise on an unrecognized type or a malformed/partial line (log + skip); ignore unknown keys inside known events. Drop `tool_call`/`tool_result`/`plan`/`chart` from the OpenAI payload (a client can't answer PAI's auto-executed tools — emitting them as `tool_calls` would hang it); optional `x-pai-events` debug side-stream behind a flag.
- **Mid-stream error** (matrix §4.9): before first byte → HTTP error status + OpenAI error body; after first byte → emit `data: {"error":{...}}\n\n` then `data: [DONE]\n\n` and close (no terminal `finish_reason:"stop"`, so a truncated answer isn't read as complete).
- **Keepalive / anti-buffering** (§10): emit an SSE keepalive every `SSE_KEEPALIVE_S` when upstream is silent (PAI can spend 60–100s in a tool phase emitting nothing); set `Cache-Control: no-cache`, `X-Accel-Buffering: no` on streaming responses.

### FR-6 Auth & tenancy
- `single_key` (default) and `passthrough` modes (§9). `key_map` deferred (D-1). Optional inbound allowlist `WRAPPER_API_KEYS`. Never log full keys (redact to `psai_…xxx`).

### FR-7 Unsupported-param policy (core — per-param, baked in)
Never `500`; behave per this table (only a couple of knobs exposed):

| Param | Behavior |
|---|---|
| `temperature`, `top_p`, penalties, `seed`, `stop`, `logprobs`, `logit_bias`, `max_tokens`/`max_completion_tokens` | **ignore** + note in `x-pai-ignored-params` header (harmless / unenforceable) |
| `n > 1` | **reject `400`** (wrong output count is a correctness bug) |
| `tools`/`functions` with `tool_choice` ∈ {`"required"`, `{…}`} | **reject `400`** (can never be satisfied) |
| `tools` with `tool_choice:"auto"`/absent | **ignore** + header (model may still answer) |
| `response_format: json_object` | best-effort: inject a "respond in JSON" system instruction + header; no guarantee (D-2) |
| `response_format: json_schema` (`strict:true`) | **reject `400`** (can't guarantee) |

### FR-8 Errors & rate limits (core)
- Normalize all PAI errors to OpenAI shape (matrix §5); map status codes; pass through `429` + `Retry-After` + `X-RateLimit-*`; remap `403 temporary lockout` (has `Retry-After`) → `429`. **Zero upstream retries on chat** (a retry double-charges token caps and may double-execute MCP side effects — FR: retries only on idempotent GETs; LiteLLM owns retry/fallback).

### FR-9 Observability
- `GET /healthz` (liveness + **`GET /api/models`** upstream ping, never chat), structured request logs (method, path, model, latency, upstream status, token counts, **redacted** keys), and `x-pai-*` response headers describing any degradation applied.

### FR-10 Files & images (Phase 2)
- Files: `POST /v1/files` → `POST /api/files/upload`, **stream** multipart through (no full-body buffering), store `purpose` in the index; `GET /v1/files` (from index), `GET /v1/files/{id}`, `GET /v1/files/{id}/content` → PAI download, `DELETE`. Multimodal `image_url` parts: `data:` URIs uploaded; http(s) URLs governed by `IMAGE_URL_FETCH` (default `off` → `400`) to keep the SSRF stance (§10). Turn-files auto-deleted after the response (`TURN_FILE_RETENTION=delete`), since PAI has no list endpoint to reclaim orphans.
- Images: Phase-2 *candidate only* — needs an image-capable model on the key (none available today).

## 8. Architecture

```
OpenAI SDK / tool
      │  Authorization: Bearer <client key>   (OpenAI wire format)
      ▼
┌─────────────────────────────────────────────┐
│  pai-openai-wrapper  (FastAPI, async)         │
│  ├─ routers: /v1/chat, /v1/models, /v1/files, │
│  │            /v1/completions, /v1/images     │
│  ├─ translate/ : request map, response map,   │
│  │               stream diff (JSONL→SSE)      │
│  ├─ auth/ : inbound gate + outbound key mode  │
│  ├─ pai_client/ : httpx async client to PAI   │
│  ├─ files_index/ : tiny store for file list   │
│  └─ errors/ : PAI flat → OpenAI nested        │
└─────────────────────────────────────────────┘
      │  Authorization: Bearer psai_...  (PAI wire format)
      ▼
   PAI backend  (/api/v4/chat, /api/models, /api/files/*, ...)
```

- **Stack recommendation:** **Python 3.12 + FastAPI + httpx + Pydantic v2 + uvicorn**. Rationale: first-class async streaming (`StreamingResponse`) for the JSONL→SSE translation, Pydantic models mirror OpenAI schemas cleanly, matches PAI's Python ecosystem, small image. (Alternative: Node/Fastify — equally viable; pick one.)
- **Stateless** except the optional lightweight `files_index` (JSON file or SQLite, mountable volume). No conversation state stored (OpenAI chat is stateless too).
- **Concurrency:** async end-to-end; stream upstream→downstream without buffering the whole body; honor client aborts by cancelling the upstream request.

## 9. Configuration (`.env` / docker-compose)

All inputs are env vars. Proposed set:

| Var | Required | Default | Purpose |
|---|---|---|---|
| `PAI_BASE_URL` | ✅ | — | e.g. `https://pai-api.thepsi.com` |
| `PAI_API_KEY` | ✅ (single_key) | — | `psai_...` used outbound |
| `AUTH_MODE` | | `single_key` | `single_key` \| `passthrough` |
| `WRAPPER_API_KEYS` | | — | comma-list of allowed inbound keys (optional gate) |
| `DEFAULT_MODEL` | | `gemma4:26b` | fallback when `model` omitted/unknown |

| `MODEL_NAME_PREFIX` | | `pai/` | avoid `gpt-*`/`claude-*` collisions in LiteLLM's price map; both prefixed + bare accepted inbound |
| `MODELS_CACHE_TTL_S` | | `300` | cache the merged model list; serve stale on upstream failure |
| `EMIT_REASONING` | | `true` | expose `thinking` as `delta.reasoning_content` |
| `SSE_KEEPALIVE_S` | | `15` | keepalive during silent upstream tool execution |
| `SEND_UNSUPPORTED_HEADER` | | `true` | emit `x-pai-ignored-params` / `x-pai-unsupported` |
| `MAX_REQUEST_MB` | | `10` | JSON body cap |
| `UPSTREAM_CONNECT_TIMEOUT_S` | | `15` | connect timeout (separate from read) |
| `REQUEST_TIMEOUT_S` | | `600` | upstream read timeout (long streams) |
| `PORT` | | `8000` | listen port — **customizable via the same `.env`**; single source of truth for uvicorn bind, `EXPOSE`, the compose port mapping (`${PORT}:${PORT}`), and the healthcheck. No port is hard-coded. |
| `LOG_LEVEL` | | `info` | |
| `CORS_ALLOW_ORIGINS` | | — | if browser clients call the wrapper directly |
| *Phase-2 candidates (not committed):* `IMAGE_URL_FETCH` (`off`), `MAX_UPLOAD_MB` (`100`) | | | vision/files |

Example `docker-compose.yml` (to be authored in the build phase):
```yaml
services:
  pai-openai-wrapper:
    image: pai-openai-wrapper:latest
    build: .
    env_file: .env                          # single source for all config, incl. PORT
    ports:
      - "${PORT:-8000}:${PORT:-8000}"       # port comes from .env; change PORT to move both sides
    volumes:
      - ./data:/app/data                     # files_index persistence (Phase 2)
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "sh", "-c", "curl -fsS http://localhost:${PORT:-8000}/healthz"]
      interval: 30s
      timeout: 5s
      retries: 3
```
The container runs `uvicorn ... --port ${PORT:-8000}`, so `PORT` in `.env` is the only place the port is set — app bind, published port, and healthcheck all derive from it. `.env.example` mirrors the config table (including `PORT`).

## 10. Non-functional requirements

- **Performance:** streaming first-token overhead < ~50 ms over direct PAI; no full-body buffering (stream upstream→downstream).
- **Streaming reliability:** SSE keepalive during silent upstream tool phases (`SSE_KEEPALIVE_S`); `Cache-Control: no-cache`, `X-Accel-Buffering: no` so intermediary proxies don't buffer or idle-drop. Keep the LiteLLM→wrapper hop **internal** (don't route the SSE hot path through a CDN/tunnel with a short idle timeout).
- **Reliability:** upstream failures map to OpenAI 5xx; client aborts cancel the upstream request; **zero retries on chat** (idempotent GETs only); separate connect vs read timeouts.
- **Security / SSRF:** the wrapper's *only* outbound host is `PAI_BASE_URL`. Client-supplied URLs are **not** fetched by default — `image_url` http(s) parts are rejected unless `IMAGE_URL_FETCH=allowlist`, which additionally blocks RFC1918/loopback/link-local/metadata IPs after DNS resolution, caps size/content-type, and forbids redirects to private space. Never log secrets (redact keys); optional inbound allowlist; body size caps (`MAX_REQUEST_MB`, `MAX_UPLOAD_MB`).
- **Portability:** single small Docker image (`python:3.12-slim`), 12-factor config, non-root user.
- **Observability:** structured JSON logs, `/healthz`, optional Prometheus `/metrics` (phase 2).
- **Testability:** fixtures captured from a live `/api/v4/chat` (see §12 test plan); streaming-diff property tests; a real LiteLLM proxy in CI, not just the SDK.

## 11. Milestones / phased roadmap

- **Phase 0 — Research & PRD** ✅ (this document + companion docs).
- **Phase 1 — Core MVP** (ordered so the unknowns surface first):
  1. **Live-verification spike (½ day, needs a real `psai_` key):** resolve O-1 (system-message behaviour per model class), O-2 (Genie cap), confirm the snapshot's non-chat endpoint shapes, and **capture JSONL fixtures** (plain text, `think:true`, tool-using Genie, image, mid-stream `error`, aborted request). *Do this before writing the translator.*
  2. `pai_client` + auth (`single_key` + `passthrough`) + error mapping + rate-limit passthrough (+ `403`→`429` remap).
  3. Message-normalization module (FR-2) with its own tests.
  4. `chat/completions` non-streaming with full `usage`.
  5. Streaming translator (FR-5): dual accumulators, Unicode-safe diff, keepalive, unknown-event tolerance, mid-stream error, `[DONE]`.
  6. `/v1/models` (merge + cache + prefix + `{id:path}`) and `/healthz` (models-based, never chat).
  7. Per-param policy (FR-7) + `x-pai-*` headers.
  8. Docker + compose + `.env.example` + non-root image.
  9. **LiteLLM-in-CI contract test** (not just the SDK).

  Note: `/v1/models` caching, SSE keepalive, and health design are **in Phase 1** — they're LiteLLM-critical, not polish.
- **Phase 2 — Files, legacy `completions`, images (best-effort), metrics.**
- **Later (only if asked):** a small stateless subset of the Responses API. Assistants/Threads/Runs and an embeddings proxy are **not planned** (§3 non-goals).

## 12. Decisions, risks, open verifications, test plan

### 12.1 Decisions (resolved — from review)
| # | Decision |
|---|---|
| D-1 | **Auth: `single_key` is primary** (LiteLLM already owns per-team virtual keys, so passthrough would duplicate that). Ship `passthrough` as a documented mode; **`key_map` cut from v1.** |
| D-2 | **`response_format: json_object`** = best-effort system instruction + header. **`json_schema`+`strict` → `400`.** No wrapper-side JSON repair in v1. |
| D-3 | **No embeddings proxy.** Return `501`; README tells operators to register Ollama in LiteLLM directly. |
| D-4 | **`ALLOW_LEGACY_SAMPLING` cut.** Not worth a second (SSE) translator for a different engine that loses skills/MCP/tools. |
| D-5 | `thinking` → `delta.reasoning_content` **on by default** (LiteLLM understands it; vanilla OpenAI SDK ignores it harmlessly). |
| D-6 | **Drop `tool_call`/`tool_result` from the OpenAI payload** by default (emitting un-answerable `tool_calls` would hang clients). Optional `x-pai-events` debug side-stream behind a flag. |
| D-7 | **Stack: FastAPI + httpx** (confirmed, closed). |
| D-8 | **Assistants/Threads/Runs dropped entirely** (deprecated upstream; fights the Genie cap). |

### 12.2 Residual risks
| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | Tool/function-calling clients (LangChain agents) can't work through PAI | High for agentic users | Loud `400` on unsatisfiable `tool_choice`; documented; don't route agents at PAI models. |
| R2 | Health checks / bursty callers trip PAI's per-key cap → 1-h `403` lockout (escalates 1×→2×→4×) | High | Health uses `/api/models` not chat; recommend key **whitelisting** + `disable_background_health_checks`; pass `Retry-After` through. |
| R3 | Silent truncation indistinguishable from completion (`finish_reason` always `stop`) | Medium | Documented limitation; nothing to fix upstream. |
| R4 | Genie-scale cap limits how many tool-profiles the wrapper can expose | Medium | Documented; use System Genies; revisit if it bites. |
| R5 | PAI intermediate `/api/v2,v3/chat` + hand-maintained changelog drift | Low | Pin `/api/v4/chat`; `/healthz` upstream ping. |

### 12.3 Open verifications (block Phase-1 translator; the spike resolves them)
- **O-1 System-message behaviour** per model class (base vs `custom_*` vs `system-genie-*`) → sets `SYSTEM_MESSAGE_POLICY` default.
- **O-2 Per-user Genie cap** (~5?) — confirm the number.
- **O-3** Does `files[]` on `/api/v4/chat` produce the `dataset_id` that `analyze_attachment` needs, or is CSV/PDF analysis web-UI-only? (Phase 2 gating.)
- **O-4** Smoke-test snapshot-only shapes: `POST /api/files/upload` fields, `POST /api/files/content`, `POST /api/images/analyze`, `/file/<id>` public URL, Custom PAI CRUD fields, System-Genie endpoints. (Phase 2, except any needed earlier.)

### 12.4 Test plan (highlights)
1. **Capture live JSONL fixtures first** (spike step 1) — every translator test replays them.
2. **Streaming-diff property test:** concatenation of all emitted deltas == final cumulative `text`, for every fixture; plus adversarial cases (non-prefix update, duplicate identical `text`, emoji/CJK split across events, `done` with no preceding `text`, `error` with no `done`, unknown event type).
3. **Real LiteLLM proxy in CI:** model discovery, non-stream, stream w/ `include_usage`, spend logging, `429` backoff, `/health`.
4. **LangChain `ChatOpenAI` smoke test** pinning the *documented failure* of `bind_tools` (the honest-degradation contract).
5. **Long-stream test:** >120s response with a silent tool phase (keepalive keeps it alive).
6. **Rate-limit test:** drive to `429`; assert `Retry-After` + `X-RateLimit-*` survive translation and `403`+`Retry-After` remaps to `429`.

## 13. Success criteria

- **Primary (LiteLLM):** an operator registers the wrapper in LiteLLM as `model: openai/<pai-model-or-genie>` with `api_base`, and LiteLLM can: discover it via `/v1/models`, complete a non-streaming chat, complete a streaming chat with correct incremental deltas + a final `usage` for cost tracking, pass its `/health` check, and back off correctly on a `429` — all against a real PAI deployment configured only through `.env`.
- The `openai` Python/Node SDK pointed directly at the wrapper's `/v1` can do the same (list models, chat stream/non-stream, upload+download a file, generate an image).
- Unsupported features (`tools`, JSON-mode, `n>1`, embeddings/audio/moderations) return documented, OpenAI-shaped errors — never a malformed or misleading success, and never a 500.
- `docker compose up` with a filled `.env` yields a working self-hosted endpoint with a green `/healthz`.

---

*Next step after sign-off: implement Phase 1. No code is written yet, per scope of this task.*
