"""Prometheus PromQL tool — via K8s apiserver proxy.

Reaches the in-cluster Prometheus Service directly via the K8s API server,
bypassing the OIDC-gated metrics-{env}.w1.lv ingress. Auth: the kubeconfig
SA token from Doppler (KUBECONFIG_DEV / KUBECONFIG_PRD).

Live service (confirmed 2026-05-25 against dev cluster):
  monitoring/kube-prometheus-stack-prometheus:9090

Prerequisite: the cluster-agent-readonly ClusterRole (kube-infra Task 1)
must gain `services/proxy` GET on the monitoring namespace. This is a
kube-infra change — flagged as a follow-up in spec § 6.2. Until that
RBAC update is applied, these calls will return 403.
"""
from __future__ import annotations
from typing import Any

from .audit import audit
from .k8s_proxy import proxy_get

_PROM_NAMESPACE = "monitoring"
_PROM_SERVICE = "kube-prometheus-stack-prometheus"
_PROM_PORT = 9090


@audit(tool="prometheus_query")
def prometheus_query(cluster: str, promql: str) -> dict[str, Any]:
    """Instant PromQL query — current value of the expression."""
    return proxy_get(
        cluster,
        namespace=_PROM_NAMESPACE,
        service=_PROM_SERVICE,
        port=_PROM_PORT,
        path="api/v1/query",
        params={"query": promql},
    )


@audit(tool="prometheus_query_range")
def prometheus_query_range(
    cluster: str,
    promql: str,
    *,
    start: str,
    end: str,
    step: str = "60s",
) -> dict[str, Any]:
    """Range PromQL query. `start`/`end` accept RFC3339 or unix timestamps."""
    return proxy_get(
        cluster,
        namespace=_PROM_NAMESPACE,
        service=_PROM_SERVICE,
        port=_PROM_PORT,
        path="api/v1/query_range",
        params={"query": promql, "start": start, "end": end, "step": step},
        timeout=30.0,
    )
