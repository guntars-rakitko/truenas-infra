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


# Fail-fast on auth-shadowing footgun: the Agent SDK silently uses
# ANTHROPIC_API_KEY over CLAUDE_CODE_OAUTH_TOKEN if both are set,
# meaning we'd quietly burn API-key credit instead of Max subscription.
# Our docker-compose passes only one at a time — this catches future
# regressions / operator-side mistakes.
if os.environ.get("ANTHROPIC_API_KEY") and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
    raise RuntimeError(
        "Both ANTHROPIC_API_KEY and CLAUDE_CODE_OAUTH_TOKEN are set — the SDK "
        "would silently shadow OAuth with API key. Pass only one in container env."
    )

_BOOT_TIME = time.time()
_scheduler = Scheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    _scheduler.start()

    # ── Mode A registration (P1) ─────────────────────────────────────
    # Fires every 5 min, but only on clusters with active alerts. The
    # cluster-side kill switch (DISABLED_MODES=A) lets the operator
    # disable Mode A at runtime via Doppler without restarting the
    # container.
    from cluster_agent.modes.alert_triage import run as run_mode_a
    from cluster_agent.tools.alertmanager import alertmanager_alerts

    def count_active(cluster: str) -> int:
        return len(alertmanager_alerts(cluster, active=True, silenced=False, inhibited=False))

    for cluster_name in ("dev", "prd"):
        if f"KUBECONFIG_{cluster_name.upper()}" in os.environ:
            _scheduler.add_mode_a_with_alert_gate(
                func=lambda c=cluster_name: run_mode_a(cluster=c),
                cluster=cluster_name,
                check_alerts_func=count_active,
                minutes=5,
            )
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
    # P1: per-mode last-run / status from scheduler job inspection
    modes: dict[str, dict] = {}
    for job in _scheduler._sched.get_jobs():
        if job.id.startswith("mode-A-"):
            cluster = job.id.removeprefix("mode-A-")
            next_run = job.next_run_time.isoformat() if job.next_run_time else None
            modes.setdefault("A", {})[cluster] = {"next_run": next_run}
    return {
        "status": "ok",
        "version": "0.1.0",
        "uptime_seconds": int(time.time() - _BOOT_TIME),
        "enabled": enabled,
        "disabled_modes": disabled_modes,
        "scheduler_running": _scheduler.running,
        "modes": modes,
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
