"""K8s apiserver proxy client — auth-free wrapper for Loki/Prom/AM tools.

The agent runs off-cluster on the NAS. Loki/Prometheus/Alertmanager UIs
are OIDC-gated by traefik-admin (Pocket-ID), so direct HTTPS hits would
fail. Instead, we reach them via the API server's services/proxy
endpoint using the agent's existing kubeconfig SA token — no separate
basic-auth credentials needed.

URL shape for a proxied service:
  {apiserver}/api/v1/namespaces/{ns}/services/{name}:{port}/proxy/{path}

Auth: the kubeconfig from Doppler (`KUBECONFIG_DEV` / `KUBECONFIG_PRD`)
contains the SA token. The cluster-agent-readonly ClusterRole (kube-infra
Task 1) must be extended with `services/proxy` GET on the monitoring
namespace before these tools work live — tracked as a follow-up (not
done in this task). See spec § 6.2 and the kube-infra runbook.
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
    base = _apiserver_url(kc).rstrip("/")
    url = (
        f"{base}/api/v1/namespaces/{namespace}/services"
        f"/{service}:{port}/proxy/{path.lstrip('/')}"
    )
    verify = _ca_bundle(kc)
    headers = {"Authorization": f"Bearer {_bearer_token(kc)}"}
    r = httpx.get(url, params=params, headers=headers, timeout=timeout, verify=verify)
    r.raise_for_status()
    return r.json()
