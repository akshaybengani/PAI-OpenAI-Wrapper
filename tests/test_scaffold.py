""" scaffold tests: boot, /healthz, config fail-fast, secret redaction."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.logging_setup import redact


def _settings(**over) -> Settings:
    base = dict(pai_base_url="https://pai-api.thepsi.com", pai_api_key="psai_" + "a" * 32)
    base.update(over)
    return Settings(_env_file=None, **base)


def test_app_boots_and_serves_healthz(client):
    """Boot + liveness. Uses the mocked upstream — never touches the network.

    (Live-server boot on an env-supplied PORT is verified separately in the Docker task;
    upstream-down behaviour is covered in test_auth_errors_models.py.)
    """
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_config_fails_fast_when_required_missing(monkeypatch):
    # Clear env so nothing satisfies the required fields, and ignore any .env.
    for var in ("PAI_BASE_URL", "PAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_port_is_configurable_from_env():
    assert _settings(port=9000).port == 9000


def test_redact_masks_psai_key_and_bearer():
    secret = "psai_0123456789abcdef0123456789abcdef"
    out = redact(f"calling PAI with key {secret}")
    assert secret not in out
    assert "psai_…def" in out

    out2 = redact("Authorization: Bearer psai_0123456789abcdef0123456789abcdef")
    assert "0123456789abcdef0123456789abcdef" not in out2
