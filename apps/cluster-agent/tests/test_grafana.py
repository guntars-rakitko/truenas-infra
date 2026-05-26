"""Grafana annotation API client — apiserver-proxy path."""
import pytest

from cluster_agent.tools.grafana import post_annotation
from cluster_agent.tools import grafana as grafana_mod


def test_post_annotation_dev(monkeypatch):
    """post_annotation calls proxy_post against the dev cluster's Grafana
    service with the DEV token in the request's Bearer header (passed
    through to Grafana for app-layer auth)."""
    monkeypatch.setenv("GRAFANA_API_TOKEN_DEV", "test-token-dev")

    captured = {}

    def fake_proxy_post(*, cluster, namespace, service, port, path, json_body, extra_headers, timeout=15.0):
        captured["cluster"] = cluster
        captured["namespace"] = namespace
        captured["service"] = service
        captured["port"] = port
        captured["path"] = path
        captured["json_body"] = json_body
        captured["extra_headers"] = extra_headers
        return {"id": 12345, "message": "Annotation added"}

    monkeypatch.setattr(grafana_mod, "proxy_post", fake_proxy_post)

    ann_id = post_annotation(
        cluster="dev",
        text="cluster-agent Mode A: PodCrashLooping in pocket-id",
        tags=["cluster-agent", "mode:A", "severity:medium"],
        time_ms=1700000000000,
    )

    assert ann_id == "12345"
    # Routing
    assert captured["cluster"] == "dev"
    assert captured["namespace"] == "monitoring"
    assert captured["service"] == "kube-prometheus-stack-grafana"
    assert captured["port"] == 80
    assert captured["path"] == "api/annotations"
    # Payload
    assert captured["json_body"]["text"] == "cluster-agent Mode A: PodCrashLooping in pocket-id"
    assert captured["json_body"]["tags"] == ["cluster-agent", "mode:A", "severity:medium"]
    assert captured["json_body"]["time"] == 1700000000000
    # Grafana-side auth header (separate from the apiserver SA token)
    assert captured["extra_headers"]["Authorization"] == "Bearer test-token-dev"


def test_post_annotation_unknown_cluster_raises():
    """Unknown cluster name → ValueError so callers can't silently miss the wrong Grafana."""
    with pytest.raises(ValueError, match="unknown cluster"):
        post_annotation(cluster="stg", text="x", tags=[], time_ms=0)


def test_post_annotation_missing_token_raises(monkeypatch):
    """No Grafana token in env → RuntimeError before any HTTP call.
    Defense-in-depth: apiserver SA auth would pass but Grafana would 401.
    Surface the misconfig with a clear message instead of letting it
    fail at the upstream layer."""
    monkeypatch.delenv("GRAFANA_API_TOKEN_DEV", raising=False)
    with pytest.raises(RuntimeError, match="GRAFANA_API_TOKEN_DEV"):
        post_annotation(cluster="dev", text="x", tags=[], time_ms=0)
