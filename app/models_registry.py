"""Model discovery + name mapping .

Source of truth is `GET /api/models` — a single source (single source).

Two behaviours worth naming:
  * **Prefix**: PAI exposes ids like `gpt-4.1-nano` and `claude-sonnet-4-6`. Passed
    through bare, LiteLLM would price them against its *real* OpenAI/Anthropic price map.
    So every id we publish is prefixed (default `pai/`), and inbound we accept either form.
  * **Cache with stale-on-failure**: model lists are polled often; an upstream blip
    shouldn't take discovery down.
"""

from __future__ import annotations

import time
from typing import Any

from app.config import Settings
from app.pai_client import PaiClient


class ModelRegistry:
    def __init__(self, settings: Settings, client: PaiClient) -> None:
        self._s = settings
        self._client = client
        self._cache: list[dict[str, Any]] | None = None
        self._fetched_at = 0.0

    @property
    def prefix(self) -> str:
        return self._s.model_name_prefix or ""

    def to_pai_model(self, requested: str | None) -> str:
        """Accept a prefixed or bare id; return the bare PAI model string."""
        name = (requested or "").strip()
        if not name:
            return self._s.default_model
        if self.prefix and name.startswith(self.prefix):
            name = name[len(self.prefix):]
        return name or self._s.default_model

    def to_openai_id(self, pai_model: str) -> str:
        return f"{self.prefix}{pai_model}"

    def is_allowed(self, pai_model: str) -> bool:
        """Is this bare model id one the deployment may serve? Empty allowlist = all."""
        allowlist = self._s.model_allowlist
        return not allowlist or pai_model in allowlist

    def _map_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """PAI entry -> OpenAI model object. Only OpenAI-defined fields are published;
        PAI's extras (capabilities, details, digest, remote_host) are deliberately dropped."""
        pai_id = entry.get("model") or entry.get("name") or ""
        return {
            "id": self.to_openai_id(pai_id),
            "object": "model",
            "created": _as_epoch(entry.get("modified_at")),
            "owned_by": "pai",
        }

    async def list_models(self, inbound_authorization: str | None = None) -> list[dict[str, Any]]:
        fresh_enough = (
            self._cache is not None
            and (time.time() - self._fetched_at) < self._s.models_cache_ttl_s
        )
        if fresh_enough:
            return self._cache  # type: ignore[return-value]

        try:
            payload = await self._client.list_models(inbound_authorization)
            entries = payload.get("models") or []
            mapped = [self._map_entry(e) for e in entries if isinstance(e, dict)]
            self._cache = [
                m for m in mapped
                if m["id"] != self.prefix and self.is_allowed(self.to_pai_model(m["id"]))
            ]
            self._fetched_at = time.time()
        except Exception:
            if self._cache is None:
                raise
            # Serve stale rather than failing discovery.
        return self._cache  # type: ignore[return-value]

    async def find(
        self, model_id: str, inbound_authorization: str | None = None
    ) -> dict[str, Any] | None:
        wanted_bare = self.to_pai_model(model_id)
        for entry in await self.list_models(inbound_authorization):
            if entry["id"] == model_id or self.to_pai_model(entry["id"]) == wanted_bare:
                return entry
        return None


def _as_epoch(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        from datetime import datetime

        try:
            cleaned = value.replace("Z", "+00:00")
            return int(datetime.fromisoformat(cleaned).timestamp())
        except ValueError:
            pass
    return 0
