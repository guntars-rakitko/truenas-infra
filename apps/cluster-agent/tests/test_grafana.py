"""Grafana annotation API client — apiserver-proxy + auth.proxy path."""
import pytest

from cluster_agent.tools.grafana import post_annotation
from cluster_agent.tools import grafana as grafana_mod


def test_post_annotation_dev(monkeypatch):
    """post_annotation calls proxy_post against the dev cluster's Grafana
    service. Auth via Grafana's auth.proxy mode — X-WEBAUTH-USER header
    identifies us, Grafana trusts the proxy (whitelist=""), no Bearer
    token needed."""
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
    # auth.proxy headers — Grafana trusts the proxy + auto-creates user
    assert captured["extra_headers"]["X-WEBAUTH-USER"] == "cluster-agent"
    assert "X-WEBAUTH-EMAIL" in captured["extra_headers"]
    assert "X-WEBAUTH-NAME" in captured["extra_headers"]
    # And NOT the old Bearer token (which apiserver-proxy was stripping)
    assert "Authorization" not in captured["extra_headers"]


def test_post_annotation_unknown_cluster_raises():
    """Unknown cluster name → ValueError so callers can't silently miss the wrong Grafana."""
    with pytest.raises(ValueError, match="unknown cluster"):
        post_annotation(cluster="stg", text="x", tags=[], time_ms=0)
