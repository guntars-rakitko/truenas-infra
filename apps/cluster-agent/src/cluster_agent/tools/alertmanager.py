"""Alertmanager tool — via K8s apiserver proxy.

Reaches the in-cluster Alertmanager Service directly via the K8s API server,
bypassing the OIDC-gated alerts-{env}.w1.lv ingress. Auth: the kubeconfig
SA token from Doppler (KUBECONFIG_DEV / KUBECONFIG_PRD).

Live service (confirmed 2026-05-25 against dev cluster):
  monitoring/kube-prometheus-stack-alertmanager:9093

The agent uses this in Mode A (alert triage) to enumerate currently-firing
alerts that need attention. Inhibited/silenced alerts default to off —
we want triage work, not noise.

Prerequisite: the cluster-agent-readonly ClusterRole (kube-infra Task 1)
must gain `services/proxy` GET on the monitoring namespace. This is a
kube-infra change — flagged as a follow-up in spec § 6.2. Until that
RBAC update is applied, these calls will return 403.
"""
from __future__ import annotations
from typing import Any

from .audit import audit
from .k8s_proxy import proxy_get

_AM_NAMESPACE = "monitoring"
_AM_SERVICE = "kube-prometheus-stack-alertmanager"
_AM_PORT = 9093


@audit(tool="alertmanager_alerts")
def alertmanager_alerts(
    cluster: str,
    *,
    active: bool = True,
    silenced: bool = False,
    inhibited: bool = False,
) -> list[dict[str, Any]]:
    """List alerts via apiserver proxy. Default scope: active only."""
    return proxy_get(
        cluster,
        namespace=_AM_NAMESPACE,
        service=_AM_SERVICE,
        port=_AM_PORT,
        path="api/v2/alerts",
        params={
            "active": str(active).lower(),
            "silenced": str(silenced).lower(),
            "inhibited": str(inhibited).lower(),
        },
    )


@audit(tool="alertmanager_history")
def alertmanager_history(
    cluster: str,
    *,
    since_hours: int = 24,
    step_seconds: int = 60,
) -> dict[str, Any]:
    """Fetch ALL alert fire activity over the trailing window.

    AM's `/api/v2/alerts` only returns alerts that are CURRENTLY firing
    or in the limited resolved-cache (last few hours). For a true 24h
    history we query Prometheus directly for the `ALERTS` metric —
    Prometheus exposes a `ALERTS{alertstate="firing"|"pending"}` series
    for every alert ever fired in the retention window.

    Returns the raw `query_range` response (Prom v1 shape). Each
    `result[i]` is one (alertname, labels) time-series with `values=[[ts,v]]`
    where v="1" while firing. Caller (digest_aggregator) groups these
    into fire→resolve cycles and computes chronicity.

    We query with `alertstate="firing"` only (pending state is internal
    to AM's grouping window; nothing we'd want to surface). step=60s
    matches Prom's scrape interval — finer granularity would just
    duplicate samples, coarser would lose short fires.
    """
    import datetime as _dt
    from .prometheus import prometheus_query_range
    end = _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(hours=since_hours)
    return prometheus_query_range(
        cluster,
        promql='ALERTS{alertstate="firing"}',
        start=start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        end=end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        step=f"{step_seconds}s",
    )
