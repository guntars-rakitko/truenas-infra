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

    # ── Mode A registration (P2 — daily digest, replaces 5-min poll) ─
    # Fires once per day at DAILY_DIGEST_HOUR:DAILY_DIGEST_MINUTE in
    # the scheduler's TZ (Europe/Riga by default). One job per cluster.
    #
    # Gating:
    #   - MODE_A_CLUSTERS env (comma-separated; default "dev,prd").
    #     Compile-time gate — clusters not listed get no job at all.
    #   - DISABLED_MODES env — per-mode kill switch checked at fire time.
    #     Operator flips this without restarting the container.
    #
    # Schedule:
    #   - DAILY_DIGEST_HOUR (default "06") — local hour to fire
    #   - DAILY_DIGEST_MINUTE (default "00")
    #   - dev fires at HH:MM, prd at HH:(MM+1) so the two calls land
    #     within Anthropic's 5-min prompt-cache TTL → cache read on the
    #     second call (~85% input-cost savings on the cached prefix).
    from cluster_agent.modes.daily_digest import run as run_daily_digest

    digest_hour = int(os.environ.get("DAILY_DIGEST_HOUR", "6"))
    digest_minute = int(os.environ.get("DAILY_DIGEST_MINUTE", "0"))
    mode_a_clusters = {
        c.strip()
        for c in os.environ.get("MODE_A_CLUSTERS", "dev,prd").split(",")
        if c.strip()
    }
    for offset, cluster_name in enumerate(("dev", "prd")):
        if cluster_name not in mode_a_clusters:
            continue
        if f"KUBECONFIG_{cluster_name.upper()}" not in os.environ:
            continue
        _scheduler.add_daily_digest(
            func=lambda c=cluster_name: run_daily_digest(cluster=c),
            cluster=cluster_name,
            hour=digest_hour,
            minute=(digest_minute + offset) % 60,   # stagger by 1 min for cache sharing
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
