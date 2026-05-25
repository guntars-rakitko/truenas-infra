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


def test_fail_fast_on_both_auth_envs_set(monkeypatch):
    """Both ANTHROPIC_API_KEY + CLAUDE_CODE_OAUTH_TOKEN set → import raises."""
    import importlib
    import sys

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-fake")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    # Remove cached module so the top-level check runs fresh on re-import.
    sys.modules.pop("main", None)
    try:
        with pytest.raises(RuntimeError, match="shadow"):
            importlib.import_module("main")
    finally:
        # Restore the cached module so subsequent tests can use it normally.
        sys.modules.pop("main", None)
