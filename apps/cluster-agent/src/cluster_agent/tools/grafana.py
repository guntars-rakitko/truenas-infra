"""Grafana annotation API — one-shot post_annotation().

Annotations are how Mode A surfaces findings on the Grafana time-series
dashboards. Operator opens the kube-prometheus-stack dashboard, sees a
vertical line at the moment the agent fired the finding, hovers for
the text. Tags are filterable from the dashboard query.

Auth: per-cluster service-account token from Doppler
cluster-agent/prd.{GRAFANA_API_TOKEN_DEV,GRAFANA_API_TOKEN_PRD}.

Endpoint: https://grafana-<cluster>.w1.lv/api/annotations (the
OIDC-gated admin hostname). The cluster-agent has no Pocket-ID
session, so the SA token is the only auth path — Grafana accepts
it as a Bearer for the API.

Note: this hits the Grafana HTTPS endpoint, NOT the apiserver-proxy
path. Grafana annotations need a Grafana-side service-account token
(not the K8s SA token); SSO bypass is by design.
"""
from __future__ import annotations
import os
import json
from typing import Iterable

import httpx

from .audit import audit


_ENDPOINTS = {
    "dev": "https://grafana-dev.w1.lv/api/annotations",
    "prd": "https://grafana-prd.w1.lv/api/annotations",
}


@audit(tool="grafana_post_annotation")
def post_annotation(
    *,
    cluster: str,
    text: str,
    tags: Iterable[str],
    time_ms: int,
    time_end_ms: int | None = None,
) -> str:
    """Post a Grafana annotation. Returns the new annotation id as str."""
    if cluster not in _ENDPOINTS:
        raise ValueError(f"unknown cluster {cluster!r}; expected one of {sorted(_ENDPOINTS)}")
    token = os.environ[f"GRAFANA_API_TOKEN_{cluster.upper()}"]
    payload: dict[str, object] = {
        "time": int(time_ms),
        "tags": list(tags),
        "text": text,
    }
    if time_end_ms is not None:
        payload["timeEnd"] = int(time_end_ms)
    r = httpx.post(
        _ENDPOINTS[cluster],
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        content=json.dumps(payload, separators=(",", ":")),
        timeout=15.0,
    )
    r.raise_for_status()
    return str(r.json()["id"])
