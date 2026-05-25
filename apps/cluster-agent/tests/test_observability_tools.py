"""Loki, Prometheus, Alertmanager tools — via K8s apiserver proxy.

All three tools now use k8s_proxy.proxy_get() rather than direct basic-auth
HTTP hits. Tests mock the apiserver proxy URLs (not the *.w1.lv hostnames)
and inject a minimal fake kubeconfig via the KUBECONFIG_DEV env var.
"""
import base64

import httpx
import respx
import yaml

from cluster_agent.tools.loki import loki_query
from cluster_agent.tools.prometheus import prometheus_query, prometheus_query_range
from cluster_agent.tools.alertmanager import alertmanager_alerts


def _fake_kubeconfig() -> str:
    """Return a base64-encoded minimal kubeconfig for tests.

    No CA data — k8s_proxy._ca_bundle() returns False (skip verify),
    which is fine in tests.
    """
    kc = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "dev", "cluster": {
            "server": "https://test-api:6443",
        }}],
        "users": [{"name": "agent", "user": {"token": "fake-token"}}],
        "contexts": [{"name": "dev", "context": {"cluster": "dev", "user": "agent"}}],
        "current-context": "dev",
    }
    return base64.b64encode(yaml.dump(kc).encode()).decode()


@respx.mock
def test_loki_query_via_apiserver_proxy(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/loki:3100/proxy/loki/api/v1/query_range"
    )
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"app": "x"}, "values": []}],
        },
    }))
    result = loki_query("dev", '{app="x"}', limit=10)
    assert result["status"] == "success"
    assert len(result["data"]["result"]) == 1


@respx.mock
def test_prometheus_query_via_apiserver_proxy(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query"
    )
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": {"resultType": "vector", "result": []},
    }))
    result = prometheus_query("dev", "up")
    assert result["status"] == "success"


@respx.mock
def test_prometheus_query_range_via_apiserver_proxy(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query_range"
    )
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": {"resultType": "matrix", "result": []},
    }))
    result = prometheus_query_range(
        "dev", "up", start="2026-05-25T00:00:00Z", end="2026-05-25T01:00:00Z"
    )
    assert result["status"] == "success"


@respx.mock
def test_alertmanager_lists_via_apiserver_proxy(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/alerts"
    )
    respx.get(url).mock(return_value=httpx.Response(200, json=[
        {"labels": {"alertname": "Watchdog"}, "status": {"state": "active"}},
    ]))
    alerts = alertmanager_alerts("dev")
    assert len(alerts) == 1
    assert alerts[0]["labels"]["alertname"] == "Watchdog"
