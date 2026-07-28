# pai-openai-wrapper

A **self-hosted, Dockerized OpenAI-compatible API** in front of a **PAI** deployment. Point any OpenAI client at it — only `base_url` changes. Built primarily to register in **LiteLLM** as an `openai/`-compatible provider.

**A translation wrapper, nothing more.** It makes PAI speak OpenAI on the wire. It exposes no PAI concepts (no Genies, Skills, MCP, Knowledge Base) and adds no features PAI lacks.

## Quickstart

```bash
cp .env.example .env      # then set PAI_API_KEY (and PORT if you want)
docker-compose up -d
curl localhost:8000/healthz
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused-in-single-key-mode")
client.chat.completions.create(
    model="pai/gemma4:26b",
    messages=[{"role": "user", "content": "Hi how are you"}],
)
```

## Endpoints

| Endpoint | Status |
|---|---|
| `POST /v1/chat/completions` | ✅ streaming + non-streaming |
| `GET /v1/models`, `GET /v1/models/{id}` | ✅ |
| `POST /v1/completions` (legacy text) | ✅ |
| `GET /healthz` | ✅ liveness + upstream probe |
| `/v1/embeddings`, `/v1/moderations`, `/v1/audio/*` | `501` — no PAI backend |
| Assistants / Threads / Runs / Responses | `404` — not implemented |

## LiteLLM

```yaml
model_list:
  - model_name: pai-gemma4
    litellm_params:
      model: openai/pai/gemma4:26b
      api_base: http://pai-openai-wrapper:8000/v1   # keep this hop internal (see below)
      api_key: os.environ/PAI_WRAPPER_KEY

litellm_settings:
  drop_params: true
```

Two things to set on the LiteLLM side:
- **`disable_background_health_checks: true`** (or a long `health_check_interval`). PAI's per-key cap is ~15 req/min, and three minutes at the cap triggers an escalating lockout. Our `/healthz` never calls chat, but LiteLLM's own per-model `/health` does.
- **Per-model cost** in `model_info` if you want spend figures — LiteLLM's price map won't know PAI models (local ones are effectively $0 anyway).

## Configuration

Everything comes from `.env` (see [.env.example](.env.example)). Required: `PAI_BASE_URL`, `PAI_API_KEY`.

`PORT` is the single source of truth for the port — uvicorn's bind, the compose mapping (`${PORT}:${PORT}`), and the healthcheck all derive from it. Change it in `.env` only.

Two settings worth understanding:

- **`ALLOWED_MODELS`** (default `gemma4:26b,gemma4:cloud`) — PAI's `/api/models` advertises *every* model the deployment knows, not what your key may call; a non-entitled model returns `403 Model not allowed`. Without this filter LiteLLM would register models that fail on first use. Empty = publish everything.
- **`MODEL_NAME_PREFIX`** (default `pai/`) — PAI exposes names like `gpt-4.1-nano` and `claude-sonnet-4-6`. Unprefixed, LiteLLM would price them against its *real* OpenAI/Anthropic price maps. Both prefixed and bare ids are accepted inbound.

## Known limitations (PAI's constraints, not oversights)

- **No tool/function calling.** PAI accepts no `tools` field. A forced `tool_choice` returns `400`; `tool_choice: "auto"` is ignored with an `x-pai-unsupported` header. Agent frameworks that require tool calling won't work.
- **Sampling params are ignored** (`temperature`, `max_tokens`, `seed`, `stop`, penalties) and reported in `x-pai-ignored-params`. PAI accepts but silently discards them — `max_tokens: 5` still returns a full answer.
- **`n>1`** and **`response_format: json_schema` with `strict`** return `400`. `json_object` is best-effort.
- **`finish_reason` is always `stop`** — PAI gives no length/filter signal, so truncation is indistinguishable from completion.
- **PAI ignores the `system` role**, so the wrapper folds it into the first user turn (`SYSTEM_MESSAGE_POLICY`).
- **No embeddings** — register Ollama directly in LiteLLM if you need them.

## Models

| Model | Where it runs | Latency |
|---|---|---|
| `pai/gemma4:26b` | local | ~3–10s |
| `pai/gemma4:cloud` | proxies to `gemma4:31b` on **ollama.com** — traffic leaves your network | ~35–40s |

`gemma4:26b` is the default. Slow generations are held open with SSE keepalive comments (PAI's own `ping` events are translated into them).

## Deployment note

Keep the LiteLLM → wrapper hop **internal** (same host or private network). Don't route the SSE hot path through a CDN/tunnel with a short idle timeout — a 40s `gemma4:cloud` generation would be cut off.

## Postman

A collection covering every route lives in [postman/](postman/) — 31 requests, 130 assertions.
Import both files, or run it as a contract suite:

```bash
newman run postman/pai-openai-wrapper.postman_collection.json \
  -e postman/pai-openai-wrapper.postman_environment.json \
  --timeout-request 180000
```

## Development

```bash
uv venv .venv && . .venv/bin/activate
uv pip install -e ".[dev]"
python -m pytest          # 62 tests
```

Tests run against a mocked PAI (no network). Streaming tests replay JSONL captured from the live deployment in `tests/fixtures/`.

## Documents

- **[docs/DESIGN.md](docs/DESIGN.md)** — requirements, decisions, and the verification plan.
- **[docs/PAI_API_INVENTORY.md](docs/PAI_API_INVENTORY.md)** — the PAI API surface this is built against.
- **[docs/OPENAI_COMPATIBILITY_MATRIX.md](docs/OPENAI_COMPATIBILITY_MATRIX.md)** — field-by-field mapping and the streaming algorithm.

## Upstream

Built against **PAI**, an enterprise product by [PSI](https://pai.thepsi.com), using its
public API documentation at <https://pai.thepsi.com/docs>. This project is independent and
unofficial — not affiliated with or endorsed by PSI or OpenAI.

PAI has not had a general launch yet, so its API may change. The wrapper depends on only
two upstream endpoints (`POST /api/v4/chat`, `GET /api/models`) to limit the impact when
it does; see [docs/PAI_API_INVENTORY.md](docs/PAI_API_INVENTORY.md) for places where the
published docs already differ from live behaviour.

## License

[Apache License 2.0](LICENSE). Use, modify, and distribute it — commercially or otherwise.
Provided **as is**, without warranty or liability of any kind.
