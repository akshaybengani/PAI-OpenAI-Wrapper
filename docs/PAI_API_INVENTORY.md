# PAI API Inventory (research reference)

## Source and provenance

Compiled from **PAI's official public documentation** at <https://pai.thepsi.com/docs>
(site launched 2026-04-19; pricing table `LAST_UPDATED = 2026-04-17`), supplemented by
behaviour observed while calling a live deployment with a valid API key.

PAI is an enterprise product by **PSI**. This document is an independent third-party
reference written to build a compatibility layer against that public API. It is not an
official PSI document, and nothing here is confidential — every endpoint below is
described in the public docs cited above.

> ⚠️ **PAI has not had a general launch yet, so this API should be treated as unstable.**
> The wrapper deliberately depends on only two endpoints — `POST /api/v4/chat` and
> `GET /api/models` — to keep the blast radius small if the rest changes. The
> live-verified corrections below already show the published docs drifting from real
> behaviour in several places; expect more of that before launch.

Items marked **⚠ unverified** come from the documentation only and have not been
exercised against a live deployment.

> ⚠️ **LIVE-VERIFIED CORRECTIONS (2026-07-27).** Probing `pai-api.thepsi.com` found real behaviour differing from these docs in four places: (1) caller `system` messages are **ignored** on base models; (2) the **non-streaming** response is **flat** (`prompt_tokens`, `completion_tokens`, `cached_tokens`, `thinking_tokens`, `tool_tokens`, `thinking`, `citations`, `iterations`) with **no `tokens` object and no `model` field**, and `duration` is in **ms**; (3) streams emit undocumented `phase` events (`mcp_connect`, `agent_run`, `llm`) plus `citations` on `done`; (4) a disallowed model returns **`403`** with `{detail, error}`. See [DESIGN.md](DESIGN.md) § "Verified live behaviour" — it supersedes this file where they conflict.

## 1. Base facts

- **Base URL**: same origin as the PAI deployment. Dev `http://localhost:5000`; Prod `https://pai-api.thepsi.com`. All paths below are relative to it.
- **Two endpoint families**:
  - **Legacy** `/api/...` — served by `backend/server.py`. Chat, Custom PAIs CRUD, files, feedback, admin users, UI settings. Streaming = **SSE** (`text/event-stream`).
  - **V4** `/api/v4/...` — served by `backend/agent/v4/routes.py` (LangGraph). Newer chat engine, System Genies, Skills, V4 stats. Streaming = **JSONL** (`application/x-ndjson`).
  - Both coexist; **no deprecation plan** for legacy. Intermediate `/api/v2/chat`, `/api/v3/chat` exist but are undocumented (referenced only in the rate-limit config).
- **Content type**: JSON in/out unless noted. Streaming chat = JSONL (V4) or SSE (legacy).
- **Auth**: every endpoint except `GET /api/ui-settings` and `/docs/*` requires `Authorization: Bearer <token-or-key>`.
- **Error shape (flat)**: `{ "error": "<human message>" }`. Some errors add context fields. Note: **not** nested under `error.message` like OpenAI.
- **CORS**: none by default (same-origin only). Cross-origin callers must configure `flask_cors` server-side.

## 2. Authentication

- **API key**: `psai_` + 32 hex chars (total 37 chars), e.g. `psai_<32 hex chars>`. Long-lived, headless. Created from Profile → API Keys (+ optional expiration). Shown once; stored masked (`psai_...3c4`). Inherits the creating user's permissions (no scopes). Rotate = delete + create (no overlap). Stored in `backend/user_api_keys.json`.
- **Bearer (Microsoft JWT / MSAL)**: RS256 JWT from Microsoft SSO. Browser/widget only, ~1h lifetime, silently refreshed. Validated against Microsoft public key; `email`/`preferred_username` → identity, `aud` must match PAI's Entra app id. Not for headless use.
- **Server resolves** header via `verify_auth()` → `{ user_id: <email>, auth_type: "api_key"|"bearer", api_key_id?, api_key_name? }`. `is_admin(user_id)` gates admin routes.
- **Key mgmt API**: `GET /api/user-api-keys/<user_id>`, `POST /api/save-api-keys`.
- Failure: `401` (missing/malformed/invalid/expired), `403` (authed but not permitted).

## 3. Chat

### `POST /api/v4/chat` — primary
Request:
```json
{
  "model": "gemma4:26b" | "custom_<pai-id>" | "system-genie-<id>" | "llama3.2:latest",
  "messages": [ { "role": "user"|"assistant"|"system"|"tool", "content": "..." } ],
  "stream": true,
  "think": false,
  "files": [ { "id": "file-id", "originalName": "...", "size": 0, "isImage": false, "url": "..." } ]
}
```
- Fields: `model` (req), `messages` (req), `stream` (default `true`), `think` (force reasoning; Genie setting usually wins), `files` (already uploaded via `POST /api/files/upload`).
- **No `temperature`/`top_p`/`max_tokens`/`n`/`stop`/`tools` fields.** Sampling knobs and caller-supplied tools are not accepted by V4.
- **Model resolution**: `custom_*` → Custom Genie (ownership/sharing checked); `system-genie-*` → System Genie; else raw base model. A Custom Genie whose base is a System Genie merges the System Genie's base model + MCP servers.

Response — streaming: **JSONL**, one object per line. Event types: `text`, `thinking`, `tool_call`, `tool_result`, `image`, `plan`, `done`, `error` (see §Streaming).

Response — non-streaming (single JSON):
```json
{
  "content": "final text",
  "tokens": { "prompt": 0, "completion": 0, "total": 0, "thinking": 0, "tool": 0, "cached": 0 },
  "generatedImage": { "url": "...", "prompt": "..." } | null,
  "toolsUsed": ["use_skill", "read_url"],
  "model": "gemma4:26b",
  "duration": 3.2
}
```
Errors: `400` malformed/empty; `401` no auth; `404` inaccessible `custom_<id>`; `503` no live System Genie for a default reference.

### `POST /api/chat` — legacy
- Same request shape **plus** `"options": { "temperature": 0.7 }` (the only place sampling params are accepted).
- Streaming = **SSE** (`text/event-stream`), not JSONL. Limited tool events, no built-in tools / skills / MCP routing / extended thinking.
- Kept for existing integrations; no deprecation.

### `GET /api/models`
```json
{ "models": [ { "model": "gemma4:26b", "label": "Gemma 4 (26B)", "provider": "ollama-local" }, ... ] }
```
- Live list depends on what Ollama has pulled + which hosted providers are enabled. Does **not** include System Genies (use `GET /api/v4/system-genies/live`). Polled on startup + every 5 min.

## 4. Streaming contract (V4 JSONL)

- `Content-Type: application/x-ndjson`, `Transfer-Encoding: chunked`. One JSON object per `\n`. **Not SSE — do not use `EventSource`.** Split on `\n` yourself; buffer partial trailing lines.
- Event envelope (every line has `type`):
  - `{ type:"text", content }` — **accumulated-so-far** text (monotonically growing; replace, don't append).
  - `{ type:"thinking", content }` — same accumulation rule.
  - `{ type:"tool_call", tool, status:"executing", args? }`
  - `{ type:"tool_result", tool, status:"success"|"error", summary? }`
  - `{ type:"image", url, prompt }` — url relative, e.g. `/api/generated-images/<uuid>.png`.
  - `{ type:"plan", tasks:[{id,title,status}] }` — full plan state each time; last wins.
  - `{ type:"done", tools_used, duration, tokens, model }` — always last.
  - `{ type:"error", error }` — interrupts; no `done` follows.
- `tokens` breakdown in `done`: `prompt` (system+history+user+KB+tool schemas), `completion` (visible output), `thinking` (subset of completion billing), `tool` (attribution, not additional cost), `cached` (from prompt cache). `total`.
- Abort via `AbortSignal` → tears down HTTP → server cancels in-flight LLM.

## 5. Files

- `POST /api/files/upload` — `multipart/form-data`, field `file`, max 100 MB → `{ id, originalName, size, isImage, url }` (`url` = `/api/files/<id>/download`).
- `GET /api/files/<id>/download` — streams file (uploader only).
- `DELETE /api/files/<id>`.
- **Public image URLs** (no auth, for `<img>` tags): `/api/generated-images/<name>` (AI images), `/file/<id>` (user uploads in `<img>`).
- `POST /api/files/content` — `multipart/form-data`, field `file` (PDF/DOCX/DOC/TXT/CSV) → `{ content, metadata:{pages,format,word_count,file_size} }` (PyMuPDF / python-docx).
- `POST /api/images/analyze` — `multipart/form-data`, field `image` (JPG/PNG/TIFF/BMP/WEBP) → `{ text, analysis:{objects[],scene} }` (Tesseract OCR + CLIP).
- Storage: `backend/files/<user-slug>/<file-id>.<ext>`, per-user scoped. **No list endpoint documented.**

## 6. Genies (Custom PAIs) — CRUD

IDs like `custom_abc123`. "Custom PAI" = legacy name for "Custom Genie".
- `GET /api/custom-pais` — list owned + shared. Array of Genie objects.
- `GET /api/custom-pais/<id>` — one (owner/shared).
- `POST /api/custom-pais` — **upsert** (create if no `id` / not owned; else update). Body fields: `id?`, `name`, `description`, `baseModel`, `systemPrompt`, `conversationStarters[]`, `mcpServers[]` (`{name,url,headers,enabled}`), `showInChat`, `enableStreaming`, `enableThinking`, `enableImageGeneration`, `enablePlanner`, `sharing` (`private|shared`), `sharedWith[]` (`{email,role:viewer|editor}`), `attached_skills[]` (`{scope,id,owner}`), `skillsDisabled`, `disabledSkillRefs[]`. Server-managed fields preserved if omitted (`attached_skills`, `skillsDisabled`, `disabledSkillRefs`, `knowledgeBase`).
- `DELETE /api/custom-pais/<id>` — owner only, irreversible.
- Limits: max **5 owned** Genies/user (`400` beyond); name `"PAI Genie"` reserved (`400`). Permissions: read/chat = owner+editor+viewer; update = owner+editor (editor can't change sharing); delete = owner.

### Genie knowledge base (per-Genie RAG)
- `POST /api/custom-pais/<pai_id>/knowledge` — multipart `file` (.pdf/.txt/.docx, ≤100 MB) → `{ fileId, fileName, extractedChars }`.
- `POST /api/custom-pais/<pai_id>/knowledge/text` — `{ name, content }` (field is `name`, not `title`).
- `GET /api/custom-pais/<pai_id>/knowledge/<file_id>?text=1` — preview/read.
- `POST /api/custom-pais/<pai_id>/knowledge/<file_id>/update` — `{ content }` (text entries only).
- `DELETE /api/custom-pais/<pai_id>/knowledge/<file_id>`.
- RAG: SentenceTransformer (`all-MiniLM-L6-v2`) + ChromaDB, cosine top-K (default 5), injected as "Relevant knowledge:" into the system prompt (~500–3000 extra input tokens/msg).

## 7. System Genies (admin)

- `GET /api/v4/system-genies` — list all (admin).
- `GET /api/v4/system-genies/live` — live only (any user). Used for "pick a System Genie".
- `POST /api/v4/system-genies` — create (admin); body like Custom PAI minus sharing.
- `PUT /api/v4/system-genies/<id>` — update (admin).
- `DELETE /api/v4/system-genies/<id>` — delete (admin); blocked if only live one.
- `PUT /api/v4/system-genies/<id>/live` — `{ isLive: bool, force?: bool }`; can't drop live count to zero without `force`.
- KB (admin): `POST .../knowledge`, `POST .../knowledge/text`, `DELETE .../knowledge/<file_id>`, `GET .../knowledge/<file_id>/preview` (note: `/preview`, not `?text=1`), `.../update`.

## 8. Skills

SKILL.md = YAML frontmatter (`id`, `name`, `description` required; `version` optional) + markdown body. Surface only via the single `use_skill` tool (prompt-injection of the body). Callers cannot supply skills at request time.
- Personal: `GET /api/v4/skills`, `GET /api/v4/skills/<id>`, `POST /api/v4/skills` (`{id,name,description,body}`), `PUT /api/v4/skills/<id>`, `DELETE /api/v4/skills/<id>` (cascade-unbinds → `{status,unbound_from}`).
- System (admin): `GET/POST /api/v4/skills/system`, `GET/PUT/DELETE /api/v4/skills/system/<id>`.
- Genie attachment (references, not copies): `GET /api/v4/skills/genie/<genie_id>`, `POST` (`{scope,id,owner?}`), `DELETE .../<skill_id>?scope=&owner=`, `PUT .../disabled` (`{skillsDisabled,disabledSkillRefs[]}`), `GET /api/v4/skills/picker?genie_id=`.

## 9. Built-in tools (fixed, per-Genie toggles)

All hardcoded in `backend/agent/v4/builtin_tools.py`, gated by per-Genie capability flags. **Callers cannot inject tools.** Schemas are OpenAI function-calling shaped internally.

| Tool | Args | Emits stream event | Gate |
|---|---|---|---|
| `use_skill` | `skill_id` | no (chip only) | ≥1 skill in scope |
| `read_url` | `url` | no | always (blocks private IPs in prod, ~10K char cap) |
| `create_plan` | `tasks[]` | `plan` | `enablePlanner` |
| `update_plan` | `updates[]`,`add_tasks[]` | `plan` | `enablePlanner` |
| `generate_image` | `prompt` | `image` (relative URL) | `enableImageGeneration`; model = Genie `imageModel` (default `x/flux2-klein:4b-bf16`) |
| `web_search` | `query`,`max_results`,`search_depth` | no | `webSearch.enabled` + Tavily key |
| `analyze_attachment` | `dataset_id` | no | `dataAnalysis.enabled` |
| `render_chart` | `dataset_id`,`type`,`x`,`y` | `chart` **⚠ unverified** (not in the Event catalog's stream-event list — catalog or tool page is stale) | `charts.enabled` |
| `report_to_admin` | `feedback_type`,`summary`,`user_consented` | no | `enableAdminFeedback` + **system Genies only** |

- **`generate_image` returns a relative URL, never base64.** One image/call, no editing/inpainting.

## 10. MCP

- PAI is an **HTTP-only** MCP client (Streamable HTTP preferred, SSE legacy; **stdio unsupported**). Attached per-Genie (`{name,url,headers,enabled}`). On first chat: `tools/list` → merged with built-ins → forwarded. ~15s/call, agent loop capped (default 50 iterations, 100 with planner).
- Static Headers apply to all users; per-user OAuth only via widget `getMcpAuth` (browser) — **not available for direct `/api/v4/chat`**.
- MCP tool results: content blocks `text` / `image` (base64+mimeType) / `resource`; errors via `isError:true`.

## 11. Ops / usage / feedback / admin

- **Token stats**: `GET /api/v4/stats/tokens` (admin, org-wide) → `{ totalTokens, promptTokens, completionTokens, toolTokens, uniqueUsers, modelStats{<id>:{total,prompts,completions,tool,displayName}}, dailyUsage[{date,tokens}], sourceStats{app{...},api{...,uniqueKeys}} }`. `GET /api/v4/stats/tokens/<user_id>` (self or admin). Legacy fallbacks `GET /api/admin/stats/tokens`, `GET /api/user/stats/tokens/<user_id>`.
- Source of truth: `backend/token_usage_logs.csv`, columns `timestamp,user_id,row_type(prompt|completion|tool),model,tokens,source(APP|API),auth_type,api_key_id,api_key_name`. 2–3 rows/round; `tool` = attribution not additional; `thinking`/`cached` NOT in CSV (only in `done` event). Computed live, no cache, file grows forever.
- **Feedback**: `POST /api/feedback` (`{type,subject,body,genie_id,model,thread_id,message_id}`); admin `GET /api/admin/feedback`, `PATCH /api/admin/feedback/<id>` (`{status}`), `GET /api/admin/stats/feedback`.
- **Admin users**: `GET/POST/DELETE /api/admin/users` (`{email}`), `GET /api/admin/check-status?email=`.
- **UI settings**: `GET /api/ui-settings` (public) → `{ambientEnabled,glassCardsEnabled}`; `PUT /api/admin/ui-settings` (admin).

## 12. Rate limits — ENFORCED TODAY ⚠️ (doc contradiction)

The **API-overview** page says "None enforced today. Planned." The dedicated **rate-limits reference** says they are enforced now with the caps below. **Treat as enforced** and handle the headers/codes.

- Scope: chat endpoints (`/api/chat`, `/api/v2..v4/chat`) at **per-API-key AND per-user** simultaneously. CRUD unmetered. Minute/hour/day = hard deny; week/month = alert only.
- Default per-key: 15/min, 200/hr, 2000/day requests; 30k/min, 400k/hr, 4M/day tokens. Per-user: 30/min, 400/hr, 4000/day; 60k/min, 800k/hr, 8M/day tokens.
- **429**: `Retry-After: <secs>`, `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Window`, `X-RateLimit-Reset` (unix epoch); body `{ error:"rate limit exceeded", limit_hit:"<scope>:<window>:<resource>", retry_after_seconds }`.
- **403** temporary lockout: `Retry-After`; body `{ error:"temporary lockout", reason:"auto:burst-3min", retry_after_seconds }`. A 403 **without** `Retry-After` = manual block → don't retry.
- **401**: manual block OR invalid/expired/missing key (indistinguishable by design).
- **Token limits enforced post-round** (credited after the LLM completes) → a single huge request always passes; the *next* one is blocked. Request-count caps applied pre-flight.
- **Auto-block escalation:** hitting the per-minute cap 3 minutes running → `403` lockout; lockout duration escalates **1×→2×→4×** within a 7-day window (burst capped 24h, daily-streak capped 7d), resetting after 7 clean days. A misconfigured wrapper (e.g. chat-based health checks) can get the org's key locked for a day — see the design notes.
- Whitelisted keys bypass all caps + the block cascade. Admin API: `GET/PUT /api/admin/rate-limit-settings`, `GET /api/admin/block-log`.

## 13. Model catalog highlights

- Local Ollama (billed **$0**): `gemma4:26b` (default), `gemma3:12b`, `llama3.2:latest`, `llama3.1:latest`, `qwen2.5:7b`, `phi4:latest`, `llava:13b` (vision), `x/flux2-klein:9b|4b` (image gen).
- Hosted Claude (if `ANTHROPIC_API_KEY`+endpoint): `claude-sonnet-4-6`, `claude-haiku-4-5`, `claude-opus-4-7`. Extended thinking when Genie "Reason before replying" on.
- Hosted OpenAI (if Azure OpenAI env): `gpt-4.1-nano`.
- Aliases regex map variants back to catalog; anything `llama|gemma|qwen|mistral|phi|deepseek|command|nomic` → ollama-local ($0). `system-genie-*`, `custom_*`, `pai-*` → local (delegated to resolved base).

## 14. Gaps vs OpenAI (no PAI backend)

- **No embeddings endpoint** (RAG uses SentenceTransformer internally, not exposed).
- **No standalone image endpoint** (image gen only via chat tool, returns URL).
- **No audio** (TTS/STT/translation).
- **No moderations** endpoint.
- **No caller-supplied `tools`/function calling** — tools are Genie-configured server-side.
- **No `n`, `logprobs`, `logit_bias`, `seed`, `stop`, `response_format`/JSON-mode** support in V4 (and only `temperature` via legacy `options`).
- **No file *list* endpoint.**
