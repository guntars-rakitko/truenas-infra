"""Prometheus PromQL tool — HTTP, read-only.

Cluster endpoints: https://metrics-{dev,prd}.w1.lv. Two query forms:
instant (current value) and range (time series). Both audit-logged.
"""
from __future__ import annotations
import os
from typing import Any
import httpx

from .audit import audit


def _auth(cluster: str) -> tuple[str, str]:
    raw = os.environ[f"CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_{cluster.upper()}"]
    user, _, password = raw.partition(":")
    return (user, password)


@audit(tool="prometheus_query", redact=["password"])
def prometheus_query(cluster: str, promql: str) -> dict[str, Any]:
    """Instant PromQL query — current value of the expression."""
    url = f"https://metrics-{cluster}.w1.lv/api/v1/query"
    r = httpx.get(url, params={"query": promql}, auth=_auth(cluster), timeout=15)
    r.raise_for_status()
    return r.json()


@audit(tool="prometheus_query_range", redact=["password"])
def prometheus_query_range(
    cluster: str,
    promql: str,
    *,
    start: str,
    end: str,
    step: str = "60s",
) -> dict[str, Any]:
    """Range PromQL query. `start`/`end` accept RFC3339 or unix timestamps."""
    url = f"https://metrics-{cluster}.w1.lv/api/v1/query_range"
    r = httpx.get(
        url,
        params={"query": promql, "start": start, "end": end, "step": step},
        auth=_auth(cluster),
        timeout=30,
    )
    r.raise_for_status()
    return r.json()
