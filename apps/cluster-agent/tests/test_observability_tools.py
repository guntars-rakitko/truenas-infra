"""Loki, Prometheus, Alertmanager tools — HTTP, read-only, basic-auth.

All three follow the same pattern: read creds from a Doppler-sourced env
var, fire an httpx GET against the cluster-specific URL, return parsed
JSON. Audit-logged via @audit; password param redacted from the log.
"""
import respx
import httpx
import pytest

from cluster_agent.tools.loki import loki_query
from cluster_agent.tools.prometheus import prometheus_query
from cluster_agent.tools.alertmanager import alertmanager_alerts


@respx.mock
def test_loki_query_returns_streams(monkeypatch):
    monkeypatch.setenv("CLUSTER_AGENT_LOKI_BASIC_AUTH_DEV", "user:pass")
    url = "https://logs-dev.w1.lv/loki/api/v1/query_range"
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
def test_prometheus_query_returns_vector(monkeypatch):
    monkeypatch.setenv("CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_DEV", "user:pass")
    url = "https://metrics-dev.w1.lv/api/v1/query"
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": {"resultType": "vector", "result": []},
    }))
    result = prometheus_query("dev", "up")
    assert result["status"] == "success"


@respx.mock
def test_alertmanager_lists_active(monkeypatch):
    monkeypatch.setenv("CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_DEV", "user:pass")
    url = "https://alerts-dev.w1.lv/api/v2/alerts"
    respx.get(url).mock(return_value=httpx.Response(200, json=[
        {"labels": {"alertname": "Watchdog"}, "status": {"state": "active"}},
    ]))
    alerts = alertmanager_alerts("dev")
    assert len(alerts) == 1
    assert alerts[0]["labels"]["alertname"] == "Watchdog"
