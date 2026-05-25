"""cluster-agent — FastAPI entrypoint.

Mounted at /app by docker-compose; uvicorn invoked as `main:app` per
apps/cluster-agent/docker-compose.yaml's command block.

Exposes:
  - /health  — Docker healthcheck + ops endpoint. Returns ok/degraded
               based on per-mode last-success timestamps. In P0 there
               are no modes running, so this is mostly an "is the
               container alive" check + Doppler-config visibility.
  - /metrics — Prometheus scrape endpoint (both clusters scrape it).
  - /        — basic identity endpoint (version + endpoint list).

Scheduled jobs land in Task 19 (APScheduler wired into the FastAPI
lifespan). P0 has no LLM-driven modes; that's P1+.
"""
from __future__ import annotations
import os
import time
from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST

from cluster_agent.emit.metrics import render


app = FastAPI(title="cluster-agent", version="0.1.0")
_BOOT_TIME = time.time()


@app.get("/health")
async def health() -> dict:
    """Container healthcheck + ops endpoint.

    Returns ok/degraded based on per-mode last-success timestamps.
    In P0 no modes are registered, so the response just reports
    config visibility (enabled flag, disabled modes list) + uptime.
    """
    enabled = os.environ.get("CLUSTER_AGENT_ENABLED", "true").lower() == "true"
    disabled_modes = sorted({
        m.strip()
        for m in os.environ.get("CLUSTER_AGENT_DISABLED_MODES", "").split(",")
        if m.strip()
    })
    return {
        "status": "ok",
        "version": "0.1.0",
        "uptime_seconds": int(time.time() - _BOOT_TIME),
        "enabled": enabled,
        "disabled_modes": disabled_modes,
        "modes": {},   # P1+: per-mode last-run timestamp + status
    }


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus scrape endpoint."""
    return Response(content=render(), media_type=CONTENT_TYPE_LATEST)


@app.get("/")
async def root() -> dict:
    """Basic identity endpoint — version + where to find docs."""
    return {
        "name": "cluster-agent",
        "version": "0.1.0",
        "endpoints": ["/health", "/metrics"],
        "docs": "https://wiki.w1.lv/runbooks/cluster-agent-runbook/",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9595)
