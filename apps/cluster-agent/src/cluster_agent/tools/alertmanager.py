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
