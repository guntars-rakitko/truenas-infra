"""Loki LogQL query tool — via K8s apiserver proxy.

Reaches the in-cluster Loki Service directly via the K8s API server,
bypassing the admin plane (Loki has no ingress of its own). Auth: the kubeconfig
SA token from Doppler (KUBECONFIG_DEV / KUBECONFIG_PRD).

Live service (confirmed 2026-05-25 against dev cluster):
  monitoring/loki:3100  (single-binary Loki deployment)

Access: kube-infra's `cluster-agent-services-proxy` Role grants the
agent's ServiceAccount `services/proxy` `get` on exactly Loki, Prometheus
and Alertmanager (flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml;
`get` only since kube-infra#1366). Until 2026-10-10 this said the calls
would 403 until that grant existed; it has existed since before Mode A
went live (2026-05).
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
    timeout: float = 60.0,
) -> dict[str, Any]:
    """LogQL range query against the in-cluster Loki via apiserver proxy.

    Defaults to the last hour if start/end are not provided.

    Returns Loki's `streams`-shaped response — best for raw log line
    excerpts. For metric-form aggregations (count_over_time, rate,
    etc.), use loki_metric_query_range instead.

    Timeout default: 60s. Heavy queries — regex across all namespaces
    over 24h — can take 30s+ on real Loki, especially for rare-match
    regexes (fatal, certificate_expired, etc.) which need to scan
    everything to confirm "no matches". Bumped from 30s 2026-05-26
    when 5 of 10 P3 tripwire queries were still ReadTimeout-ing.
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

    Used by P3 log-pattern mining for per-namespace counts: error-level
    lines for the ratio outlier (24h against the 6 days before) and each
    tripwire's 24h count. Step defaults to 1h (3600s): the aggregator sums
    hourly points over hour-aligned windows, so Loki's
    `align_queries_with_step` moves no window edge (digest_aggregator
    _hour_windows).

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
        timeout=60.0,
    )
