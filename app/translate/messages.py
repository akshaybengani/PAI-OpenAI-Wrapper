"""Message normalization + system-message policy .

Two jobs:

1. **Normalize** an OpenAI history into what PAI V4 actually accepts — roles plus a
   *string* content. Agent frameworks replay shapes PAI would choke on or mishandle:
   `assistant.content = null` alongside `tool_calls`, `tool`-role turns keyed by
   `tool_call_id`, `developer` roles, multimodal content-part arrays.

2. **Deliver the system prompt.** Verified against the live API: PAI *silently ignores* the
   `system` role on base models — a system-delivered codeword never reached the model,
   while the same text in the user turn did. So `fold_into_first_user` is the default.
"""

from __future__ import annotations

import json
from typing import Any

from app.errors import bad_request

TOOL_PREFIX = "[tool result: {name}]"
SYSTEM_PREFIX = "[System instruction: {content}]"


def _content_to_text(content: Any) -> str:
    """Flatten OpenAI content (string | parts array | None) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for part in content:
            if isinstance(part, str):
                chunks.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type")
                if ptype == "text" and part.get("text"):
                    chunks.append(str(part["text"]))
                elif ptype in {"image_url", "input_audio", "file"}:
                    # Not supported here. Reject loudly rather than silently dropping an
                    # input the caller believes the model can see.
                    raise bad_request(
                        f"Content parts of type '{ptype}' are not supported by this endpoint "
                        "(text-only). Send text content instead.",
                        param="messages",
                    )
        return "\n".join(c for c in chunks if c)
    return str(content)


def _summarize_tool_calls(tool_calls: Any) -> str:
    """Serialize assistant tool_calls into text — never send content:null upstream."""
    if not isinstance(tool_calls, list):
        return ""
    rendered: list[str] = []
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        fn = call.get("function") or {}
        name = fn.get("name") or call.get("name") or "tool"
        args = fn.get("arguments")
        if isinstance(args, (dict, list)):
            args = json.dumps(args, ensure_ascii=False)
        rendered.append(f"{name}({args})" if args else f"{name}()")
    return "[called: " + "; ".join(rendered) + "]" if rendered else ""


def normalize_messages(
    messages: list[dict[str, Any]] | None,
    system_message_policy: str = "fold_into_first_user",
) -> list[dict[str, str]]:
    """OpenAI messages -> PAI messages. Raises 400 if nothing usable remains."""
    if not messages:
        raise bad_request("'messages' must be a non-empty array.", param="messages")

    system_parts: list[str] = []
    body: list[dict[str, str]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = (msg.get("role") or "").strip().lower()
        raw = msg.get("content")

        if role in {"system", "developer"}:  # developer -> system
            text = _content_to_text(raw)
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            text = _content_to_text(raw)
            if not text:
                continue
            name = msg.get("name") or (msg.get("tool_call_id") or "tool")
            body.append({"role": "user", "content": f"{TOOL_PREFIX.format(name=name)} {text}"})
            continue

        if role == "assistant":
            text = _content_to_text(raw)
            if not text and msg.get("tool_calls"):
                text = _summarize_tool_calls(msg["tool_calls"])
            if text:
                body.append({"role": "assistant", "content": text})
            continue

        if role == "user":
            text = _content_to_text(raw)
            if text:
                body.append({"role": "user", "content": text})
            continue

        # Unknown role: treat as user if it carries text, else drop.
        text = _content_to_text(raw)
        if text:
            body.append({"role": "user", "content": text})

    system_text = "\n\n".join(system_parts).strip()

    if system_text and system_message_policy == "passthrough":
        body.insert(0, {"role": "system", "content": system_text})
        system_text = ""

    if system_text:
        # fold_into_first_user: PAI ignores the system role, so ride along with the
        # first user turn (verified to work).
        for i, msg in enumerate(body):
            if msg["role"] == "user":
                body[i] = {
                    "role": "user",
                    "content": f"{SYSTEM_PREFIX.format(content=system_text)}\n\n{msg['content']}",
                }
                break
        else:
            body.append({"role": "user", "content": SYSTEM_PREFIX.format(content=system_text)})

    if not body or not any(m["role"] == "user" for m in body):
        raise bad_request(
            "After normalization no user content remained to send. "
            "Provide at least one user message with text content.",
            param="messages",
        )
    return body
