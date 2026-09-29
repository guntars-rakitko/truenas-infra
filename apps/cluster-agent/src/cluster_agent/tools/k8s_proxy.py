"""K8s apiserver proxy client — auth-free wrapper for Loki/Prom/AM tools.

The agent runs off-cluster on the NAS. Loki/Prometheus/Alertmanager UIs
are OIDC-gated by traefik-admin (Pocket-ID), so direct HTTPS hits would
fail. Instead, we reach them via the API server's services/proxy
endpoint using the agent's existing kubeconfig SA token — no separate
basic-auth credentials needed.

URL shape for a proxied service:
  {apiserver}/api/v1/namespaces/{ns}/services/{name}:{port}/proxy/{path}

Auth: the kubeconfig from Doppler (`KUBECONFIG_DEV` / `KUBECONFIG_PRD`)
contains the SA token. kube-infra's `cluster-agent-services-proxy` Role
(flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml) grants it
`services/proxy` on exactly Loki, Prometheus and Alertmanager, and `get` ONLY
(kube-infra#1366, 2026-09-28): the apiserver maps a POST to `create`, which
the Role no longer grants, so this module is GET-only by design. Grafana is
not reachable here at all; its annotations go through tools/grafana.py.
"""
from __future__ import annotations
import base64
import os
import tempfile
from typing import Any

import httpx
import yaml


def _kubeconfig_for(cluster: str) -> dict[str, Any]:
    """Load kubeconfig dict from Doppler env (base64-encoded YAML)."""
    raw = os.environ[f"KUBECONFIG_{cluster.upper()}"]
    try:
        decoded = base64.b64decode(raw).decode("utf-8")
    except Exception:
        # Already plain YAML (in tests we may set it unencoded)
        decoded = raw
    return yaml.safe_load(decoded)


def _apiserver_url(kubeconfig: dict[str, Any]) -> str:
    return kubeconfig["clusters"][0]["cluster"]["server"]


def _ca_bundle(kubeconfig: dict[str, Any]) -> str | bool:
    """Return path to a temp file with the CA bundle, or False (skip verify)
    if no CA data is present in the kubeconfig.
    """
    ca_b64 = kubeconfig["clusters"][0]["cluster"].get("certificate-authority-data")
    if not ca_b64:
        return False
    ca_pem = base64.b64decode(ca_b64).decode("utf-8")
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False)
    tmp.write(ca_pem)
    tmp.close()
    return tmp.name


def _bearer_token(kubeconfig: dict[str, Any]) -> str:
    return kubeconfig["users"][0]["user"]["token"]


def _build_proxy_url(kubeconfig: dict[str, Any], namespace: str, service: str, port: int, path: str) -> str:
    base = _apiserver_url(kubeconfig).rstrip("/")
    return (
        f"{base}/api/v1/namespaces/{namespace}/services"
        f"/{service}:{port}/proxy/{path.lstrip('/')}"
    )


def proxy_get(
    cluster: str,
    namespace: str,
    service: str,
    port: int,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> Any:
    """GET via K8s apiserver services/proxy.

    URL shape:
      {apiserver}/api/v1/namespaces/{ns}/services/{name}:{port}/proxy/{path}

    Returns the proxied response's parsed JSON (assumes upstream returns JSON).
    Raises httpx.HTTPStatusError on non-2xx responses.
    """
    kc = _kubeconfig_for(cluster)
    url = _build_proxy_url(kc, namespace, service, port, path)
    verify = _ca_bundle(kc)
    headers = {"Authorization": f"Bearer {_bearer_token(kc)}"}
    r = httpx.get(url, params=params, headers=headers, timeout=timeout, verify=verify)
    r.raise_for_status()
    return r.json()

