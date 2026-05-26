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


def proxy_post(
    cluster: str,
    namespace: str,
    service: str,
    port: int,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> Any:
    """POST via K8s apiserver services/proxy.

    Used by Mode A's Grafana annotation dispatch where the LB-IP path
    (https://grafana-{env}.w1.lv) fails because the NAS is on the same
    VLAN as the LB IP and ARPs locally instead of routing via MikroTik.
    The apiserver-proxy bypass routes through the apiserver VIP (which
    answers ARP directly from a cluster node), then forwards internally.

    `json_body` is serialised with the default JSON encoder and POSTed
    with content-type application/json.

    `extra_headers` is merged into the request (after the apiserver
    Authorization header) — used to pass the Grafana SA token through
    to the proxied destination, which authenticates separately at the
    application layer.

    Returns the proxied response's parsed JSON. Raises
    httpx.HTTPStatusError on non-2xx.
    """
    kc = _kubeconfig_for(cluster)
    url = _build_proxy_url(kc, namespace, service, port, path)
    verify = _ca_bundle(kc)
    headers = {"Authorization": f"Bearer {_bearer_token(kc)}"}
    if extra_headers:
        headers.update(extra_headers)
    r = httpx.post(url, json=json_body, headers=headers, timeout=timeout, verify=verify)
    r.raise_for_status()
    return r.json()
