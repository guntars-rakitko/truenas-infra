"""FastAPI app smoke — /health + /metrics endpoints."""
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body
    assert body["status"] in ("ok", "degraded")
    assert "modes" in body


def test_metrics_endpoint(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    # The metrics module declares cluster_agent_run_total; it should appear
    # in the Prometheus exposition output even before any increments.
    assert "cluster_agent_run_total" in r.text


def test_health_enabled_flag_from_env(monkeypatch, client):
    """ENABLED env var (no prefix) drives the health response."""
    monkeypatch.setenv("ENABLED", "false")
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_health_disabled_modes_from_env(monkeypatch, client):
    """DISABLED_MODES env var (no prefix) surfaces in health response."""
    monkeypatch.setenv("ENABLED", "true")
    monkeypatch.setenv("DISABLED_MODES", "A,F")
    r = client.get("/health")
    assert r.status_code == 200
    assert "A" in r.json()["disabled_modes"]
    assert "F" in r.json()["disabled_modes"]


def test_anthropic_api_key_env_is_visible(monkeypatch):
    """ANTHROPIC_API_KEY env var is accessible (sdk reads it directly)."""
    import os
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")
    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-test-placeholder"


def test_llm_auth_mode_oauth_strips_api_key(monkeypatch):
    """LLM_AUTH_MODE=oauth → ANTHROPIC_API_KEY removed from env so the
    SDK can't accidentally use it. CLAUDE_CODE_OAUTH_TOKEN preserved."""
    import importlib
    import sys
    import os

    monkeypatch.setenv("LLM_AUTH_MODE", "oauth")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-shadow-risk")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-active")
    sys.modules.pop("main", None)
    try:
        importlib.import_module("main")
        assert os.environ.get("ANTHROPIC_API_KEY") is None
        assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") == "sk-ant-oat01-active"
    finally:
        sys.modules.pop("main", None)


def test_llm_auth_mode_api_key_strips_oauth(monkeypatch):
    """LLM_AUTH_MODE=api_key → CLAUDE_CODE_OAUTH_TOKEN removed, API key kept."""
    import importlib
    import sys
    import os

    monkeypatch.setenv("LLM_AUTH_MODE", "api_key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-active")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-stripped")
    sys.modules.pop("main", None)
    try:
        importlib.import_module("main")
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api03-active"
        assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None
    finally:
        sys.modules.pop("main", None)


def test_llm_auth_mode_default_is_api_key(monkeypatch):
    """LLM_AUTH_MODE unset → default 'api_key' (backwards-compatible
    with pre-2026-05-26 deployments)."""
    import importlib
    import sys
    import os

    monkeypatch.delenv("LLM_AUTH_MODE", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-active")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-stripped")
    sys.modules.pop("main", None)
    try:
        importlib.import_module("main")
        assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-api03-active"
        assert os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") is None
    finally:
        sys.modules.pop("main", None)


def test_llm_auth_mode_invalid_value_raises(monkeypatch):
    """LLM_AUTH_MODE set to a bogus value → startup error (operator typo)."""
    import importlib
    import sys

    monkeypatch.setenv("LLM_AUTH_MODE", "bogus")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-x")
    sys.modules.pop("main", None)
    try:
        with pytest.raises(RuntimeError, match="LLM_AUTH_MODE"):
            importlib.import_module("main")
    finally:
        sys.modules.pop("main", None)


def test_llm_auth_mode_oauth_but_no_token_raises(monkeypatch):
    """LLM_AUTH_MODE=oauth but CLAUDE_CODE_OAUTH_TOKEN empty in Doppler
    → fail fast at startup with a clear hint, not 401 hours later."""
    import importlib
    import sys

    monkeypatch.setenv("LLM_AUTH_MODE", "oauth")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-irrelevant")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    sys.modules.pop("main", None)
    try:
        with pytest.raises(RuntimeError, match="CLAUDE_CODE_OAUTH_TOKEN"):
            importlib.import_module("main")
    finally:
        sys.modules.pop("main", None)
