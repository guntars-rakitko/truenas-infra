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

# The 24h history query behind the daily digest.
#
# `InfoInhibitor` is excluded at the source (kube-infra #1286). It is
# kube-prometheus-stack's severity=none meta-alert from `general.rules`:
#
#     group by (namespace) (ALERTS{severity="info"} == 1)
#       unless on (namespace) (... any firing warning|critical ...)
#
# It carries no information of its own. It restates "some info alert
# exists in this namespace", and because its selector has no alertstate
# matcher it also fires for info alerts that are only PENDING. So it
# flaps with every CPUThrottlingHigh that crosses its threshold and
# drops back before its `for:` elapses. Measured on prd 2026-09-26:
# doppler-operator-system InfoInhibitor had 37 firing samples in 24h
# while the CPUThrottlingHigh it mirrors had 2 firing and 34 pending.
# The digest read that as "flapping 13x" and filed a finding (#1286,
# plus the InfoInhibitor halves of #1263/#1312/#1313). An info alert
# that actually fires still arrives as its own group.
#
# `Watchdog` is deliberately NOT excluded here. It is dropped later
# (summary rendering + the digest prompt). Because it always fires, the
# history is never empty on a healthy cluster, and
# `daily_digest.run_async` returns early on an empty history BEFORE log
# mining and the summary run. Excluding Watchdog at the source would
# turn every alert-quiet day into a digest with no tripwire scan and no
# summary.
_ALERTS_HISTORY_PROMQL = 'ALERTS{alertstate="firing", alertname!="InfoInhibitor"}'


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
    to AM's grouping window; nothing we'd want to surface), and without
    the `InfoInhibitor` meta-alert (see `_ALERTS_HISTORY_PROMQL`).
    step=60s matches Prom's scrape interval — finer granularity would
    just duplicate samples, coarser would lose short fires.
    """
    import datetime as _dt
    from .prometheus import prometheus_query_range
    end = _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(hours=since_hours)
    return prometheus_query_range(
        cluster,
        promql=_ALERTS_HISTORY_PROMQL,
        start=start.isoformat(timespec="seconds").replace("+00:00", "Z"),
        end=end.isoformat(timespec="seconds").replace("+00:00", "Z"),
        step=f"{step_seconds}s",
    )
