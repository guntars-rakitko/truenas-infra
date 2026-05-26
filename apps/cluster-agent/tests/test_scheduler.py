"""Scheduler — APScheduler skeleton + kill-switch logic.

P0 lands the infrastructure; no modes are registered yet. P1+ wires
Mode A, then progressively the rest. The kill switches operate at
two levels:
  - global   (ENABLED=false) — stop all modes
  - per-mode (DISABLED_MODES=A,F,J) — stop a subset
"""
from cluster_agent.scheduler import Scheduler, is_mode_enabled


def test_mode_enabled_when_default(monkeypatch):
    """ENABLED=true (or unset) + DISABLED_MODES empty → mode enabled."""
    monkeypatch.setenv("ENABLED", "true")
    monkeypatch.delenv("DISABLED_MODES", raising=False)
    assert is_mode_enabled("A") is True


def test_mode_disabled_globally(monkeypatch):
    """ENABLED=false → no mode is enabled, regardless of per-mode list."""
    monkeypatch.setenv("ENABLED", "false")
    assert is_mode_enabled("A") is False


def test_mode_disabled_individually(monkeypatch):
    """Per-mode list disables specific modes; others stay enabled."""
    monkeypatch.setenv("ENABLED", "true")
    monkeypatch.setenv("DISABLED_MODES", "A,F,J")
    assert is_mode_enabled("A") is False
    assert is_mode_enabled("B") is True
    assert is_mode_enabled("J") is False


def test_scheduler_starts_and_stops():
    """Scheduler lifecycle: start() sets running=True; shutdown() resets it."""
    s = Scheduler()
    s.start()
    assert s.running
    s.shutdown(wait=False)
    assert not s.running


def test_add_daily_digest_registers_cron_job_per_cluster():
    """add_daily_digest creates a cron-triggered job at hour:minute,
    one per cluster, with the job-id pattern the /health endpoint reads."""
    s = Scheduler()
    s.add_daily_digest(func=lambda: None, cluster="dev", hour=6, minute=0)
    s.add_daily_digest(func=lambda: None, cluster="prd", hour=6, minute=1)
    jobs = {j.id: j for j in s._sched.get_jobs()}
    assert "mode-A-dev" in jobs
    assert "mode-A-prd" in jobs
    # Cron trigger fields are accessible via the trigger object
    dev_trigger = jobs["mode-A-dev"].trigger
    prd_trigger = jobs["mode-A-prd"].trigger
    # APScheduler exposes the field as a list of CronField objects keyed
    # by name — assert hour=6, minute=0 (dev) / 1 (prd)
    assert str(dev_trigger).startswith("cron[")
    assert "hour='6'" in str(dev_trigger)
    assert "minute='0'" in str(dev_trigger)
    assert "minute='1'" in str(prd_trigger)


def test_add_daily_digest_respects_kill_switch(monkeypatch):
    """If DISABLED_MODES includes A, the wrapped digest job no-ops."""
    monkeypatch.setenv("DISABLED_MODES", "A")
    called = []
    s = Scheduler()
    s.add_daily_digest(func=lambda: called.append(1), cluster="dev", hour=6, minute=0)
    # Invoke the wrapped callable directly (skipping the cron trigger)
    job = s._sched.get_job("mode-A-dev")
    job.func()
    assert called == []   # kill switch swallowed it
