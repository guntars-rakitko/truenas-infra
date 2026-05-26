"""Loki LogQL query tool — via K8s apiserver proxy.

Reaches the in-cluster Loki Service directly via the K8s API server,
bypassing the OIDC-gated logs-{env}.w1.lv ingress. Auth: the kubeconfig
SA token from Doppler (KUBECONFIG_DEV / KUBECONFIG_PRD).

Live service (confirmed 2026-05-25 against dev cluster):
  monitoring/loki:3100  (single-binary Loki deployment)

Prerequisite: the cluster-agent-readonly ClusterRole (kube-infra Task 1)
must gain `services/proxy` GET on the monitoring namespace. This is a
kube-infra change — flagged as a follow-up in spec § 6.2. Until that
RBAC update is applied, these calls will return 403.
"""
from __future__ import annotations
import datetime as dt
from typing import Any

from .audit import audit
from .k8s_proxy import proxy_get

_LOKI_NAMESPACE = "monitoring"
_LOKI_SERVICE = "loki"
_LOKI_PORT = 3100


@audit(tool="loki_query")
def loki_query(
    cluster: str,
    logql: str,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    limit: int = 100,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """LogQL range query against the in-cluster Loki via apiserver proxy.

    Defaults to the last hour if start/end are not provided.

    Returns Loki's `streams`-shaped response — best for raw log line
    excerpts. For metric-form aggregations (count_over_time, rate,
    etc.), use loki_metric_query_range instead.

    Timeout default: 30s. Heavy queries (regex across all namespaces
    over 24h window) routinely take 5-15s on a busy cluster. The
    digest's tripwire scan uses this — 15s proxy default was tripping
    half of them.
    """
    now = dt.datetime.now(dt.timezone.utc)
    start = start or now - dt.timedelta(hours=1)
    end = end or now
    params = {
        "query": logql,
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": limit,
    }
    return proxy_get(
        cluster,
        namespace=_LOKI_NAMESPACE,
        service=_LOKI_SERVICE,
        port=_LOKI_PORT,
        path="loki/api/v1/query_range",
        params=params,
        timeout=timeout,
    )


@audit(tool="loki_metric_query_range")
def loki_metric_query_range(
    cluster: str,
    logql: str,
    *,
    start: dt.datetime,
    end: dt.datetime,
    step_seconds: int = 3600,
) -> dict[str, Any]:
    """LogQL range query for metric-form expressions (count_over_time,
    rate, etc.). Returns a Prometheus-style `matrix` response: one
    series per label set, each series a list of [timestamp, value]
    pairs at `step_seconds` granularity.

    Used by P3 log-pattern mining to compute per-namespace error/warn
    counts over the 7-day baseline window. Step defaults to 1h
    (3600s) — fine-grained enough to compute daily totals, coarse
    enough to keep response size manageable.

    Note: limit param is irrelevant for metric queries (Loki returns
    the full series). Don't pass one.
    """
    params = {
        "query": logql,
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "step": f"{step_seconds}s",
    }
    return proxy_get(
        cluster,
        namespace=_LOKI_NAMESPACE,
        service=_LOKI_SERVICE,
        port=_LOKI_PORT,
        path="loki/api/v1/query_range",
        params=params,
        timeout=30.0,
    )
