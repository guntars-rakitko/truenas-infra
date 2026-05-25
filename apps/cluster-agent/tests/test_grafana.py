"""Grafana annotation API client."""
import respx
import httpx
import pytest

from cluster_agent.tools.grafana import post_annotation


@respx.mock
def test_post_annotation_dev(monkeypatch):
    """post_annotation hits the dev Grafana annotations endpoint with the
    DEV token and returns the new annotation id."""
    monkeypatch.setenv("GRAFANA_API_TOKEN_DEV", "test-token-dev")
    route = respx.post("https://grafana-dev.w1.lv/api/annotations").mock(
        return_value=httpx.Response(200, json={"id": 12345, "message": "Annotation added"})
    )
    ann_id = post_annotation(
        cluster="dev",
        text="cluster-agent Mode A: PodCrashLooping in pocket-id",
        tags=["cluster-agent", "mode:A", "severity:medium"],
        time_ms=1700000000000,
    )
    assert ann_id == "12345"
    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer test-token-dev"
    body = req.read().decode()
    assert "PodCrashLooping" in body
    assert '"tags":["cluster-agent","mode:A","severity:medium"]' in body
    assert '"time":1700000000000' in body


def test_post_annotation_unknown_cluster_raises():
    """Unknown cluster name → ValueError so callers can't silently miss the wrong Grafana."""
    with pytest.raises(ValueError, match="unknown cluster"):
        post_annotation(cluster="stg", text="x", tags=[], time_ms=0)
