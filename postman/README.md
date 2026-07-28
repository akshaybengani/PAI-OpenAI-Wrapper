# Postman collection

Every route the wrapper exposes — 31 requests, 130 assertions.

## Import into Postman
1. **Import** → both files in this folder.
2. Select the **pai-openai-wrapper (local)** environment.
3. Adjust `baseUrl` if the wrapper isn't on `http://localhost:8000`.

| Variable | Default | Notes |
|---|---|---|
| `baseUrl` | `http://localhost:8000` | Wrapper origin, no trailing slash |
| `apiKey` | `placeholder-key` | Sent as `Authorization: Bearer`. Ignored in `single_key` mode unless `WRAPPER_API_KEYS` is set |
| `model` | `pai/gemma4:26b` | Or `pai/gemma4:cloud` (~40s, and leaves your network) |

## Run it as a contract suite

```bash
newman run postman/pai-openai-wrapper.postman_collection.json \
  -e postman/pai-openai-wrapper.postman_environment.json \
  --timeout-request 180000
```

`--timeout-request` matters: `gemma4:cloud` can take 40s+, and the thinking request is slow too.

## Folders

| Folder | Covers |
|---|---|
| 0 · Ops | `/healthz` (probes models upstream, never chat) |
| 1 · Models | list, retrieve (prefixed + bare ids), unknown → 404 |
| 2 · Chat Completions | simple, system message, multi-turn, agent history, streaming, streaming+usage, reasoning, json_object, default model |
| 3 · Legacy Completions | string prompt, array prompt, streaming |
| 4 · Honest Degradation | ignored sampling params, tools auto, `n>1` → 400, forced `tool_choice` → 400, strict `json_schema` → 400, non-entitled model → 404, empty messages → 400 |
| 5 · Not Supported (501) | embeddings, moderations, audio, images |
| 6 · Not Implemented (404) | assistants, threads, responses |

The streaming tests reassemble the SSE deltas and assert they form non-empty text — Postman
renders SSE as raw text, so the parsing happens in the test script.

## Regenerating

The collection is generated so the shared request/test scaffolding stays consistent:

```bash
python postman/build_collection.py
```

Edit [`build_collection.py`](build_collection.py), not the JSON.
