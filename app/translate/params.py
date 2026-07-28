"""Per-param policy for OpenAI fields PAI cannot honour .

Why this module exists: PAI returns **HTTP 200 and silently ignores** anything outside
`model/messages/stream/think/files` (verified — `tools`, `temperature`, and `max_tokens`
all sailed through, and `max_tokens: 5` still produced 51 tokens). Upstream will never
tell the caller a parameter was dropped, so the wrapper is the only honest broker.

The rule: ignore what's harmless (and say so in a header), reject loudly what can
never be satisfied, and never return a 500.
"""

from __future__ import annotations

from typing import Any

from app.errors import bad_request

# Silently unenforceable — accept, ignore, and report in x-pai-ignored-params.
IGNORED_PARAMS = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "stop",
    "logprobs",
    "top_logprobs",
    "logit_bias",
    "max_tokens",
    "max_completion_tokens",
    "parallel_tool_calls",
    "service_tier",
    "store",
    "metadata",
    "user",
    # legacy /v1/completions extras
    "echo",
    "suffix",
    "best_of",
)


def apply_param_policy(body: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Validate/triage caller params.

    Returns (ignored, unsupported) for the response headers.
    Raises 400 for anything that could never be satisfied.
    """
    ignored: list[str] = []
    unsupported: list[str] = []

    # --- hard rejections -------------------------------------------------
    n = body.get("n")
    if n is not None and int(n) > 1:
        raise bad_request(
            "This endpoint supports n=1 only; the upstream model returns a single "
            "completion per request.",
            param="n",
        )

    tools = body.get("tools") or body.get("functions")
    tool_choice = body.get("tool_choice") or body.get("function_call")
    if tools:
        forced = (
            isinstance(tool_choice, dict)
            or (isinstance(tool_choice, str) and tool_choice.lower() in {"required", "any", "none"} and tool_choice.lower() != "none")
        )
        if forced:
            raise bad_request(
                "Tool calling is not supported by this endpoint, so a required or named "
                "'tool_choice' can never be satisfied. Remove 'tool_choice' (or set it to "
                "'auto') to receive a normal text completion.",
                param="tool_choice",
            )
        unsupported.append("tools")

    rf = body.get("response_format")
    json_object = False
    if isinstance(rf, dict):
        rtype = rf.get("type")
        if rtype == "json_schema":
            schema = rf.get("json_schema") or {}
            if schema.get("strict"):
                raise bad_request(
                    "response_format 'json_schema' with strict=true cannot be guaranteed by "
                    "this endpoint. Use {\"type\": \"json_object\"} for best-effort JSON.",
                    param="response_format",
                )
            unsupported.append("response_format.json_schema")
            json_object = True
        elif rtype == "json_object":
            json_object = True

    # --- silent ignores --------------------------------------------------
    for name in IGNORED_PARAMS:
        if body.get(name) is not None:
            ignored.append(name)

    body["_json_object"] = json_object
    return ignored, unsupported


JSON_NUDGE = (
    "Respond with a single valid JSON object and nothing else — no prose, no markdown "
    "fences, no commentary."
)


def wants_thinking(body: dict[str, Any]) -> bool:
    """reasoning_effort / reasoning.* -> PAI think:true.

    'none' is a legal OpenAI value meaning *do not* reason, so it must not enable it.
    """
    effort = body.get("reasoning_effort")
    if effort is None:
        reasoning = body.get("reasoning")
        if isinstance(reasoning, dict):
            effort = reasoning.get("effort")
    if effort is None:
        return False
    return str(effort).strip().lower() not in {"none", ""}
