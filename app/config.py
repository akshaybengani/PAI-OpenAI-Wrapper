"""Configuration — every value comes from the environment (12-factor).

All settings are read from process env / the `.env` file that docker-compose loads.
`PAI_BASE_URL` and `PAI_API_KEY` are required and the app fails fast at startup if
they are missing.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["single_key", "passthrough"]
SystemMessagePolicy = Literal["passthrough", "fold_into_first_user"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Required: upstream PAI target ---
    pai_base_url: str = Field(..., description="e.g. https://pai-api.thepsi.com")
    pai_api_key: str = Field(..., description="psai_... used outbound in single_key mode")

    # --- Auth / tenancy ---
    auth_mode: AuthMode = "single_key"
    wrapper_api_keys: str = ""  # comma-separated inbound allowlist (empty = open)

    # --- Model / request behaviour ---
    # PAI's GET /api/models advertises every model the deployment knows about, NOT what
    # this key may actually call — asking for a non-entitled one returns 403 "Model not
    # allowed". Publishing all of them would make LiteLLM register models that 404 on use,
    # so restrict discovery to what the key can really reach. Empty = publish everything.
    allowed_models: str = "gemma4:26b,gemma4:cloud"
    default_model: str = "gemma4:26b"
    # A live probe showed PAI ignores the caller `system` role on base models,
    # so folding it into the first user turn is the only reliable delivery. Default set accordingly.
    system_message_policy: SystemMessagePolicy = "fold_into_first_user"
    model_name_prefix: str = "pai/"
    models_cache_ttl_s: int = 300
    emit_reasoning: bool = True
    sse_keepalive_s: int = 15
    send_unsupported_header: bool = True
    max_request_mb: int = 10

    # --- Timeouts ---
    upstream_connect_timeout_s: int = 15
    request_timeout_s: int = 600

    # --- Server ---
    port: int = 8000
    log_level: str = "info"
    cors_allow_origins: str = ""  # comma-separated; empty = no CORS

    @property
    def inbound_allowlist(self) -> list[str]:
        return [k.strip() for k in self.wrapper_api_keys.split(",") if k.strip()]

    @property
    def model_allowlist(self) -> list[str]:
        """Bare PAI model ids this deployment may serve. Empty = no restriction."""
        return [m.strip() for m in self.allowed_models.split(",") if m.strip()]


@lru_cache
def get_settings() -> Settings:
    """Cached singleton. Raises pydantic ValidationError if required env is missing."""
    return Settings()
