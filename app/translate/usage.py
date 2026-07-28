"""Usage mapping — TWO input shapes, one output.

PAI is inconsistent here, verified live:

  non-streaming body  -> FLAT:   {"prompt_tokens":798, "completion_tokens":19,
                                  "cached_tokens":0, "thinking_tokens":0, "tool_tokens":0}
                                  ...no `tokens` object, no `model`.
  streaming `done`    -> NESTED: {"tokens":{"prompt":803,"completion":6,"thinking":0,
                                  "cached":0,"tool":0,"total":809}, "model":"gemma4:26b"}

Both collapse to the OpenAI `usage` object here.
"""

from __future__ import annotations

from typing import Any


def map_usage(source: dict[str, Any] | None) -> dict[str, Any]:
    source = source or {}
    nested = source.get("tokens")

    if isinstance(nested, dict):  # streaming `done`
        prompt = int(nested.get("prompt") or 0)
        completion = int(nested.get("completion") or 0)
        cached = int(nested.get("cached") or 0)
        thinking = int(nested.get("thinking") or 0)
        total = int(nested.get("total") or (prompt + completion))
    else:  # flat non-streaming body
        prompt = int(source.get("prompt_tokens") or 0)
        completion = int(source.get("completion_tokens") or 0)
        cached = int(source.get("cached_tokens") or 0)
        thinking = int(source.get("thinking_tokens") or 0)
        total = int(source.get("total_tokens") or (prompt + completion))

    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "prompt_tokens_details": {"cached_tokens": cached},
        "completion_tokens_details": {"reasoning_tokens": thinking},
    }
