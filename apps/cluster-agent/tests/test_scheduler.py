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
