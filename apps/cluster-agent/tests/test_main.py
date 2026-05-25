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
