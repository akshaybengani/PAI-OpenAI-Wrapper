"""PAI JSONL -> OpenAI SSE . The crux of the wrapper.

PAI sends **cumulative** text: every `text` event carries the whole answer so far.
OpenAI sends **incremental** deltas. So we diff each cumulative event against what we
already emitted.

Robustness rules, all earned from live observation:
  * Only `text`, `thinking`, `done`, `error` mean anything. Real streams also carry
    undocumented `phase` events (`mcp_connect`, `agent_run`, `llm`) and PAI adds event
    types freely — so anything unrecognised is dropped and the stream continues.
  * A malformed/partial line is skipped, never fatal.
  * Diffing is on Python `str` (code points), so a multi-byte emoji can't be split.
  * If a cumulative update is not a prefix-extension of what we saw, emit the whole new
    value and reset — never a negative slice.
"""

from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Iterator

from app.translate.usage import map_usage

KNOWN_TYPES = {"text", "thinking", "done", "error"}
DONE_SENTINEL = "data: [DONE]\n\n"
# A valid SSE comment: ignored by every client, but it keeps the socket warm so an
# intermediary proxy doesn't drop a long, silent generation.
KEEPALIVE = ": keep-alive\n\n"


def sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


class StreamTranslator:
    """Stateful PAI-event -> OpenAI-chunk converter.

    One instance per response: it owns the stable chunk id and the two
    independent accumulators.
    """

    def __init__(self, chunk_id: str, model: str, emit_reasoning: bool = True,
                 include_usage: bool = False) -> None:
        self.id = chunk_id
        self.model = model
        self.emit_reasoning = emit_reasoning
        self.include_usage = include_usage
        self.created = int(time.time())
        self._prev_text = ""
        self._prev_thinking = ""
        self._role_sent = False
        self._finished = False

    # --- chunk builders ---------------------------------------------------
    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "chat.completion.chunk",
            "created": self.created,
            "model": self.model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason,
                         "logprobs": None}],
        }

    def _diff(self, cumulative: str, previous: str) -> tuple[str, str]:
        """Return (delta, new_previous). Falls back to the whole value on a non-prefix update."""
        if not isinstance(cumulative, str):
            return "", previous
        if cumulative == previous:
            return "", previous
        if cumulative.startswith(previous):
            return cumulative[len(previous):], cumulative
        return cumulative, cumulative  # reset: never emit a negative slice

    def role_chunk(self) -> str:
        self._role_sent = True
        return sse(self._chunk({"role": "assistant"}))

    # --- event handling ---------------------------------------------------
    def handle(self, event: dict[str, Any]) -> Iterator[str]:
        """Translate one PAI event into zero or more SSE frames."""
        etype = event.get("type")

        if etype == "ping":
            # PAI emits these ~every 10s while a slow model generates (observed on
            # gemma4:cloud, which can take 35s+). Convert upstream liveness into
            # downstream liveness instead of discarding it.
            yield KEEPALIVE
            return

        if etype not in KNOWN_TYPES:
            return  # drop phase/plan/chart/tool_call/unknown and keep going

        if etype == "text":
            delta, self._prev_text = self._diff(event.get("content") or "", self._prev_text)
            if delta:
                yield sse(self._chunk({"content": delta}))
            return

        if etype == "thinking":
            delta, self._prev_thinking = self._diff(
                event.get("content") or "", self._prev_thinking
            )
            if delta and self.emit_reasoning:
                yield sse(self._chunk({"reasoning_content": delta}))
            return

        if etype == "error":
            yield from self.error_frames(str(event.get("error") or "Upstream stream error"))
            return

        if etype == "done":
            self._finished = True
            yield sse(self._chunk({}, finish_reason="stop"))
            if self.include_usage:
                usage_chunk = {
                    "id": self.id,
                    "object": "chat.completion.chunk",
                    "created": self.created,
                    "model": self.model,
                    "choices": [],
                    "usage": map_usage(event),
                }
                yield sse(usage_chunk)
            yield DONE_SENTINEL

    def error_frames(self, message: str) -> Iterator[str]:
        """Mid-stream failure after bytes are already flushed.

        Deliberately no terminal finish_reason:"stop" — that would let a client read a
        truncated answer as complete. Emit the error, then close the stream.
        """
        self._finished = True
        yield sse({
            "error": {
                "message": message,
                "type": "api_error",
                "param": None,
                "code": "upstream_error",
            }
        })
        yield DONE_SENTINEL

    @property
    def finished(self) -> bool:
        return self._finished


def parse_line(line: str) -> dict[str, Any] | None:
    """Parse one JSONL line; None if it isn't usable (never raises)."""
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


async def translate_stream(
    lines: AsyncIterator[str], translator: StreamTranslator
) -> AsyncIterator[str]:
    """Full pipeline: PAI JSONL lines -> OpenAI SSE frames."""
    yield translator.role_chunk()
    async for line in lines:
        event = parse_line(line)
        if event is None:
            continue
        for frame in translator.handle(event):
            yield frame
    if not translator.finished:
        # Upstream ended without a `done` event — close the stream cleanly anyway.
        yield sse(translator._chunk({}, finish_reason="stop"))
        yield DONE_SENTINEL
