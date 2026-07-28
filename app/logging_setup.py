"""Structured JSON logging with secret redaction.

Never let a full `psai_` key reach the logs — redact to `psai_…<last3>` (see AC).
The same redaction is applied to Authorization headers and any bearer token.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

# psai_ + 32 hex is the documented shape, but redact any psai_<token>.
_SECRET_RE = re.compile(r"(psai_)([A-Za-z0-9]{4,})")
_BEARER_RE = re.compile(r"(Bearer\s+)(\S+)", re.IGNORECASE)


def redact(text: str) -> str:
    """Mask secrets in an arbitrary string."""
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}…{m.group(2)[-3:]}", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)}…{m.group(2)[-3:]}", text)
    return text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        for key in ("method", "path", "model", "status", "latency_ms", "upstream_status"):
            if (val := getattr(record, key, None)) is not None:
                payload[key] = val
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "info") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
