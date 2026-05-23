"""Loki LogQL query tool — HTTP, read-only.

Cluster endpoint pattern: https://logs-{dev,prd}.w1.lv. Basic-auth
credentials are stored in Doppler as `<user>:<password>` per cluster:
  - CLUSTER_AGENT_LOKI_BASIC_AUTH_DEV
  - CLUSTER_AGENT_LOKI_BASIC_AUTH_PRD
The container env (Task 7) surfaces them; we parse at call time.
"""
from __future__ import annotations
import datetime as dt
import os
from typing import Any
import httpx

from .audit import audit


def _auth(cluster: str) -> tuple[str, str]:
    raw = os.environ[f"CLUSTER_AGENT_LOKI_BASIC_AUTH_{cluster.upper()}"]
    user, _, password = raw.partition(":")
    return (user, password)


@audit(tool="loki_query", redact=["password"])
def loki_query(
    cluster: str,
    logql: str,
    *,
    start: dt.datetime | None = None,
    end: dt.datetime | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """LogQL range query. Defaults to the last hour."""
    now = dt.datetime.now(dt.timezone.utc)
    start = start or now - dt.timedelta(hours=1)
    end = end or now
    params = {
        "query": logql,
        "start": str(int(start.timestamp() * 1e9)),
        "end": str(int(end.timestamp() * 1e9)),
        "limit": limit,
    }
    url = f"https://logs-{cluster}.w1.lv/loki/api/v1/query_range"
    r = httpx.get(url, params=params, auth=_auth(cluster), timeout=15)
    r.raise_for_status()
    return r.json()
