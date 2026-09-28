"""Grafana annotation API — a NodePort from the NAS, with a service-account token.

Annotations are how Mode A surfaces findings on the Grafana time-series
dashboards. Operator opens the kube-prometheus-stack dashboard, sees a
vertical line at the moment the agent fired the finding, hovers for
the text. Tags are filterable from the dashboard query.

## The path (kube-infra#1366, 2026-09-28)

  POST {GRAFANA_URLS[key]}/api/annotations
  Authorization: Bearer <GRAFANA_SA_TOKEN_<KEY>>

`GRAFANA_URLS` (clusters.py) is kube-infra's `grafana-nas` Service, NodePort
30030 on the node's mgmt address, which a CiliumNetworkPolicy admits from the
NAS's 10.10.5.10 only. The token belongs to the Grafana service account
`cluster-agent` (Editor) in THAT cluster's Grafana, minted by hand and kept in
Doppler `cluster-agent/prd` as GRAFANA_SA_TOKEN_DEV / GRAFANA_SA_TOKEN_PRD.
Grafana's database is not backed up, so a from-zero rebuild of a cluster loses
the account: every post then gets 401 until it is re-minted (kube-infra
CLAUDE.md § Post-bootstrap operator tasks).

⚠ Never send `X-WEBAUTH-*` headers here. From 10.10.5.10, outside Grafana's
`[auth.proxy]` whitelist (the pod CIDR), Grafana refuses them, and the header
path was the hole #1366 closed.

A failure raises; `dispatch.dispatch` catches it, logs it and counts it
(`cluster_agent_dispatch_errors_total{surface="grafana_annotation"}`, alert
`ClusterAgentDispatchErrors`), so an annotation never breaks a Mode A run.

## History: why not the other paths

  - Direct HTTPS to `https://grafana-{env}.w1.lv/api/annotations` with a
    Bearer token (the first version) failed with `[Errno 113] Host is
    unreachable`: that name is traefik-admin's LB IP on the mgmt VLAN, which
    the NAS shares. The kernel sees the address in its directly-connected /24
    and ARPs for it on-link instead of routing via MikroTik (which has the
    BGP route), and nothing on the L2 segment answers for a BGP-advertised
    address. A node address does answer ARP, hence the NodePort.
  - The apiserver's services/proxy with a Bearer token (PR #39) got 401: the
    apiserver drops `Authorization` after authenticating the caller.
  - The apiserver's services/proxy with `X-WEBAUTH-USER: cluster-agent`
    (2026-05-26, PR #42, until kube-infra#1366): it worked, and it was the
    hole. Grafana's auth.proxy trusts that header from any pod-CIDR address,
    the apiserver proxy arrives from one, and the header can name any user,
    `admin` included. So the "read-only" SA could act as Grafana Admin, and
    the header-created `cluster-agent` user itself was an Admin.
"""
from __future__ import annotations

import os
from collections.abc import Iterable

import httpx

from .. import clusters
from .audit import audit


def _token(cluster: str) -> str:
    """The key's Grafana service-account token, from the environment.

    Stripped: a token piped into Doppler with a trailing newline would otherwise
    make an invalid header. Missing or empty raises with the variable's NAME
    only, never a value."""
    var = f"GRAFANA_SA_TOKEN_{cluster.upper()}"
    token = os.environ.get(var, "").strip()
    if not token:
        raise RuntimeError(f"{var} is not set: no Grafana service-account token for {cluster!r}")
    return token


@audit(tool="grafana_post_annotation")
def post_annotation(
    *,
    cluster: str,
    text: str,
    tags: Iterable[str],
    time_ms: int,
    time_end_ms: int | None = None,
    timeout: float = 15.0,
) -> str:
    """Post a Grafana annotation to `cluster`'s Grafana. Returns its id."""
    base = clusters.GRAFANA_URLS.get(cluster)
    if base is None:
        raise ValueError(
            f"unknown cluster {cluster!r}; expected one of {sorted(clusters.GRAFANA_URLS)}"
        )
    payload: dict[str, object] = {
        "time": int(time_ms),
        "tags": list(tags),
        "text": text,
    }
    if time_end_ms is not None:
        payload["timeEnd"] = int(time_end_ms)
    r = httpx.post(
        f"{base.rstrip('/')}/api/annotations",
        json=payload,
        headers={"Authorization": f"Bearer {_token(cluster)}"},
        timeout=timeout,
    )
    r.raise_for_status()
    return str(r.json()["id"])
