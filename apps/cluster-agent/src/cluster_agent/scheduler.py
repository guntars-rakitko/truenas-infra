"""APScheduler-based mode scheduler: the Mode A daily digest, one job
per cluster (main.py registers them).

Two-layer kill switches both checked at the wrapped-job level:
  1. ENABLED — global kill (stops every mode)
  2. DISABLED_MODES — comma-separated per-mode disable list

Both are Doppler keys of `cluster-agent/prd`, read from the container's
env when a job fires. The env is rendered into the compose at deploy
time, so a Doppler change reaches it only with
`./manage.sh phase apps --only cluster-agent --apply`, which recreates
the container (truenas-infra CLAUDE.md § cluster-agent ops). Until
2026-10-10 this said the next fire picks a Doppler edit up within 60 s
through "the Doppler operator": that operator runs in the clusters, not
on the NAS.

Timezone: Europe/Riga (operator-facing; matches the cadence specs in
the design doc which are stated in EEST).
"""
from __future__ import annotations
import os
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler


def is_mode_enabled(mode: str) -> bool:
    """Check both kill switches: global + per-mode.

    Called by the scheduler's wrapped-job closure on each fire, so
    operator can disable a mode at runtime via Doppler without
    restarting the container.
    """
    if os.environ.get("ENABLED", "true").lower() != "true":
        return False
    disabled = {
        m.strip()
        for m in os.environ.get("DISABLED_MODES", "").split(",")
        if m.strip()
    }
    return mode not in disabled


class Scheduler:
    """Wraps APScheduler.BackgroundScheduler with our kill-switch.

    Each `add_daily_digest(...)` registers a job whose execution is gated
    on is_mode_enabled("A"); if either kill switch is set when the job
    fires, the closure no-ops. APScheduler still records the fire (for
    health-check liveness), so the agent knows the scheduler itself
    is alive even when modes are off. (`add_mode`, a generic registration
    for the never-built modes B-J, went on 2026-10-10 unused.)
    """

    def __init__(self) -> None:
        self._sched = BackgroundScheduler(timezone="Europe/Riga")
        self.running = False

    def start(self) -> None:
        self._sched.start()
        self.running = True

    def shutdown(self, wait: bool = True) -> None:
        self._sched.shutdown(wait=wait)
        self.running = False

    def add_daily_digest(
        self,
        func: Callable[[], Any],
        *,
        cluster: str,
        hour: int = 6,
        minute: int = 0,
    ) -> None:
        """Mode A daily-digest job (2026-05-26 pivot — replaces the
        5-min add_mode_a_with_alert_gate).

        Fires once per day at hour:minute in the scheduler's timezone
        (Europe/Riga, set in __init__). One job per cluster — dev and
        prd can be staggered by passing different `minute` values so
        their prompts can share Anthropic's 5-min prompt cache.

        No per-fire alert-count gate (unlike the 5-min model) — the
        runner itself decides "quiet day" semantics from the digest
        window, which is the right place for that decision now.
        """
        def wrapped() -> None:
            if not is_mode_enabled("A"):
                return
            func()
        self._sched.add_job(
            wrapped,
            trigger="cron",
            id=f"mode-A-{cluster}",
            hour=hour,
            minute=minute,
        )
