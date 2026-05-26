"""Grafana annotation API — via K8s apiserver-proxy.

Annotations are how Mode A surfaces findings on the Grafana time-series
dashboards. Operator opens the kube-prometheus-stack dashboard, sees a
vertical line at the moment the agent fired the finding, hovers for
the text. Tags are filterable from the dashboard query.

## Why apiserver-proxy and not direct HTTPS

Originally this tool POSTed direct to `https://grafana-{env}.w1.lv/api/
annotations` (the OIDC-gated admin hostname) with a Grafana SA token
as Bearer. That failed in production with `[Errno 113] Host is
unreachable` because:

  - Grafana's traefik-admin LB IP is on the mgmt VLAN (e.g. 10.10.5.40)
  - The NAS host is ALSO on the mgmt VLAN (10.10.5.10)
  - Kernel sees the LB IP in its directly-connected /24 → ARPs locally
    instead of routing via MikroTik (which has the BGP route)
  - Nothing on the L2 segment answers ARP for the LB IP (it's a
    Cilium-advertised host route, not bound to any L2 interface)

So we route through the apiserver-proxy instead — same pattern as
Loki/Prom/AM. The NAS reaches the apiserver via VIP (held by a cluster
node directly on the VLAN, ARP works). The apiserver then forwards
internally to the Grafana pod via kube-proxy / CNI.

URL shape:
  {apiserver}/api/v1/namespaces/monitoring/services/kube-prometheus-stack-grafana:80/proxy/api/annotations

Auth is two-layer:
  1. K8s SA token from kubeconfig (apiserver authn) — the
     `cluster-agent-services-proxy` Role grants `create` on
     services/proxy for `kube-prometheus-stack-grafana[:80]` (kube-infra
     PR #567 for this fix).
  2. Grafana SA token (`GRAFANA_API_TOKEN_{DEV,PRD}` env var) — sent
     in the proxied request body's Authorization header. Grafana
     enforces this at its own auth layer regardless of how the request
     reached it.
"""
from __future__ import annotations
import os
from typing import Iterable

from .audit import audit
from .k8s_proxy import proxy_post


_GRAFANA_NAMESPACE = "monitoring"
_GRAFANA_SERVICE = "kube-prometheus-stack-grafana"
_GRAFANA_SERVICE_PORT = 80


@audit(tool="grafana_post_annotation")
def post_annotation(
    *,
    cluster: str,
    text: str,
    tags: Iterable[str],
    time_ms: int,
    time_end_ms: int | None = None,
) -> str:
    """Post a Grafana annotation via apiserver-proxy. Returns annotation id."""
    if cluster not in ("dev", "prd"):
        raise ValueError(f"unknown cluster {cluster!r}; expected 'dev' or 'prd'")
    grafana_token = os.environ.get(f"GRAFANA_API_TOKEN_{cluster.upper()}")
    if not grafana_token:
        raise RuntimeError(
            f"GRAFANA_API_TOKEN_{cluster.upper()} not set; cannot authenticate "
            f"to Grafana at the application layer (apiserver auth would still "
            f"pass via SA token, but Grafana itself would 401)."
        )
    payload: dict[str, object] = {
        "time": int(time_ms),
        "tags": list(tags),
        "text": text,
    }
    if time_end_ms is not None:
        payload["timeEnd"] = int(time_end_ms)
    resp = proxy_post(
        cluster=cluster,
        namespace=_GRAFANA_NAMESPACE,
        service=_GRAFANA_SERVICE,
        port=_GRAFANA_SERVICE_PORT,
        path="api/annotations",
        json_body=payload,
        # Grafana-side auth — separate from the K8s SA token used to
        # authenticate the apiserver-proxy hop.
        extra_headers={"Authorization": f"Bearer {grafana_token}"},
    )
    return str(resp["id"])
