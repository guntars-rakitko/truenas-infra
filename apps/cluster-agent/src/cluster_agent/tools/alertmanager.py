"""Alertmanager tool — HTTP, read-only.

Cluster endpoints: https://alerts-{dev,prd}.w1.lv. The agent uses this
in Mode A (alert triage) to enumerate currently-firing alerts that need
attention. Inhibited/silenced alerts default to off — we want triage
work, not noise.
"""
from __future__ import annotations
import os
from typing import Any
import httpx

from .audit import audit


def _auth(cluster: str) -> tuple[str, str]:
    raw = os.environ[f"CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_{cluster.upper()}"]
    user, _, password = raw.partition(":")
    return (user, password)


@audit(tool="alertmanager_alerts", redact=["password"])
def alertmanager_alerts(
    cluster: str,
    *,
    active: bool = True,
    silenced: bool = False,
    inhibited: bool = False,
) -> list[dict[str, Any]]:
    """List alerts. Default scope: active only."""
    url = f"https://alerts-{cluster}.w1.lv/api/v2/alerts"
    r = httpx.get(
        url,
        params={
            "active": str(active).lower(),
            "silenced": str(silenced).lower(),
            "inhibited": str(inhibited).lower(),
        },
        auth=_auth(cluster),
        timeout=15,
    )
    r.raise_for_status()
    return r.json()
