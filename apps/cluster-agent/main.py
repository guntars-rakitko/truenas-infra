"""cluster-agent — FastAPI entrypoint.

Mounted at /app by docker-compose; uvicorn invoked as `main:app` per
apps/cluster-agent/docker-compose.yaml's command block.

Exposes:
  - /health  — Docker healthcheck + ops endpoint.
  - /metrics — Prometheus scrape endpoint.
  - /        — basic identity endpoint.

APScheduler runs in a BackgroundScheduler thread; lifecycle managed
by FastAPI's lifespan context-manager. P0 registers no modes (the
scheduler starts empty). P1+ adds modes via _scheduler.add_mode(...)
inside the lifespan startup block.
"""
from __future__ import annotations
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST

from cluster_agent.emit.metrics import render
from cluster_agent.scheduler import Scheduler


_BOOT_TIME = time.time()
_scheduler = Scheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _scheduler.start()
    # P0: no modes registered. P1+ will add via:
    #   _scheduler.add_mode("A", run_mode_a, trigger="interval", minutes=5)
    # etc. — see spec § 4.5 for the cadence table.
    yield
    # Shutdown
    _scheduler.shutdown(wait=False)


app = FastAPI(title="cluster-agent", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    """Container healthcheck + ops endpoint.

    Returns ok/degraded based on per-mode last-success timestamps.
    In P0 no modes are registered, so the response just reports
    config visibility (enabled flag, disabled modes list) + uptime
    + scheduler liveness.
    """
    enabled = os.environ.get("ENABLED", "true").lower() == "true"
    disabled_modes = sorted({
        m.strip()
        for m in os.environ.get("DISABLED_MODES", "").split(",")
        if m.strip()
    })
    return {
        "status": "ok",
        "version": "0.1.0",
        "uptime_seconds": int(time.time() - _BOOT_TIME),
        "enabled": enabled,
        "disabled_modes": disabled_modes,
        "scheduler_running": _scheduler.running,
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
