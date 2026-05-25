"""APScheduler-based mode scheduler.

In P0: empty — no modes registered. The skeleton + kill-switch logic
lands here so P1's mode registration is a one-liner add per mode (see
spec § 4.5 for the cadence table).

Two-layer kill switches both checked at the wrapped-job level:
  1. ENABLED — global kill (stops every mode)
  2. DISABLED_MODES — comma-separated per-mode disable list

Both are Doppler-controlled at runtime (no container restart needed —
the wrapped job reads env on every fire). Operator flips a key in
Doppler → next scheduled fire honors the new value (~Doppler operator
60s reconcile interval to refresh the container env).

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

    Each `add_mode(...)` registers a job whose execution is gated on
    is_mode_enabled(mode); if either kill switch is set when the job
    fires, the closure no-ops. APScheduler still records the fire (for
    health-check liveness), so the agent knows the scheduler itself
    is alive even when modes are off.
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

    def add_mode(
        self,
        mode: str,
        func: Callable[[], Any],
        trigger: str,
        **trigger_kwargs: Any,
    ) -> None:
        """Register a mode runner with the kill switch wrapping execution.

        Args:
            mode:           one-letter mode identifier (A/B/D/E/F/G/H/I/J)
            func:           the mode-runner coroutine wrapper (sync callable)
            trigger:        APScheduler trigger type ("interval", "cron", ...)
            **trigger_kwargs: trigger-specific kwargs (e.g. minutes=5,
                              day_of_week='mon', hour=9, ...)
        """
        def wrapped() -> None:
            if not is_mode_enabled(mode):
                return
            func()
        self._sched.add_job(
            wrapped,
            trigger=trigger,
            id=f"mode-{mode}",
            **trigger_kwargs,
        )

    def add_mode_a_with_alert_gate(
        self,
        func: Callable[[], Any],
        *,
        cluster: str,
        check_alerts_func: Callable[[str], int],
        minutes: int = 5,
    ) -> None:
        """Mode A specifically — fire only if Alertmanager has >0 active alerts.

        check_alerts_func(cluster) returns the count of active alerts. If 0,
        skip the LLM call entirely (saves cost on idle clusters).
        """
        def wrapped() -> None:
            if not is_mode_enabled("A"):
                return
            try:
                count = check_alerts_func(cluster)
            except Exception:
                # If we can't reach AM, skip silently — the alertmanager
                # tool's own audit log captures the failure
                return
            if count == 0:
                return
            func()
        self._sched.add_job(wrapped, trigger="interval", id=f"mode-A-{cluster}", minutes=minutes)
