"""Tests for daily_digest runner — orchestration of the 24h pipeline."""
from __future__ import annotations
import datetime as dt
import json
from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_daily_digest_quiet_day_no_findings(tmp_path, monkeypatch):
    """If Prom returns no ALERTS series, the runner short-circuits to a
    quiet-period DigestResult and never calls the LLM."""
    from cluster_agent.modes import daily_digest

    monkeypatch.setattr(daily_digest, "alertmanager_history",
                        lambda cluster, since_hours=24: {"data": {"result": []}})

    async def boom(**kw):
        raise AssertionError("LLM must not be called on a quiet day")
    monkeypatch.setattr(daily_digest, "triage_digest", boom)

    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.50")

    result = await daily_digest.run_async(cluster="dev")
    assert result.quiet_period is True
    assert result.findings_emitted == 0
    assert result.alert_groups_seen == 0
    assert "No alerts fired" in result.summary


@pytest.mark.asyncio
async def test_daily_digest_emits_findings_via_dispatch(tmp_path, monkeypatch):
    """When the LLM returns a Report with 1 Finding, dispatch is called
    once with that Finding and the result counts it."""
    from cluster_agent.modes import daily_digest
    from cluster_agent.schema import Report, Finding, Evidence, AlertGroup

    # Prom returns one chronic alert series so aggregate produces a group
    now = dt.datetime.now(dt.timezone.utc)
    samples = [[(now - dt.timedelta(minutes=i)).timestamp(), "1"] for i in range(120, 0, -1)]
    monkeypatch.setattr(daily_digest, "alertmanager_history",
                        lambda cluster, since_hours=24: {
                            "data": {"result": [{
                                "metric": {"alertname": "KubePodCrashLooping",
                                           "namespace": "pocket-id", "pod": "pocket-id-0"},
                                "values": samples,
                            }]},
                        })
    monkeypatch.setattr(daily_digest, "alertmanager_alerts",
                        lambda cluster, **kw: [])
    # Skip context gathering (kubectl + loki) so tests don't need to stub them
    monkeypatch.setattr(daily_digest, "_gather_context_for_chronic",
                        lambda groups, cluster: "(stubbed)")

    # LLM returns a Report with one Finding
    fake_finding = Finding(
        id="01JK3R8Q9M01234567890123XY",
        mode="A", cluster="dev", severity="medium",
        title="Pocket-ID pod crash-looping 2h", summary="Pod pocket-id-0 restarting.",
        evidence=[Evidence(type="alert", ref="Alertmanager/KubePodCrashLooping@now")],
        confidence=0.7, dedup_key="alert:KubePodCrashLooping:pocket-id-0:dev",
    )
    fake_report = Report(
        id="01JK3R8Q9M01234567890123XZ", cluster="dev", digest_window_hours=24,
        summary="One issue: pocket-id crash-looping for 2h.",
        quiet_period=False, findings=[fake_finding],
        total_alerts_24h=1, chronic_count=1, transient_count=0, self_healed_count=0,
    )
    async def fake_triage(**kw):
        return fake_report
    monkeypatch.setattr(daily_digest, "triage_digest", fake_triage)

    # Capture dispatch calls
    dispatch_calls = []
    def fake_dispatch(finding, action, *, db):
        dispatch_calls.append(finding)
    monkeypatch.setattr(daily_digest, "dispatch", fake_dispatch)

    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.50")

    result = await daily_digest.run_async(cluster="dev")
    assert result.findings_emitted == 1
    assert result.quiet_period is False
    assert len(dispatch_calls) == 1
    assert dispatch_calls[0].dedup_key == "alert:KubePodCrashLooping:pocket-id-0:dev"


@pytest.mark.asyncio
async def test_daily_digest_budget_exceeded_returns_error_no_dispatch(tmp_path, monkeypatch):
    """If triage_digest raises LLMBudgetExceeded, the runner returns an
    error DigestResult and does NOT call dispatch."""
    from cluster_agent.modes import daily_digest
    from cluster_agent.llm import LLMBudgetExceeded

    now = dt.datetime.now(dt.timezone.utc)
    samples = [[(now - dt.timedelta(minutes=i)).timestamp(), "1"] for i in range(5, 0, -1)]
    monkeypatch.setattr(daily_digest, "alertmanager_history",
                        lambda cluster, since_hours=24: {
                            "data": {"result": [{
                                "metric": {"alertname": "X"}, "values": samples,
                            }]},
                        })
    monkeypatch.setattr(daily_digest, "alertmanager_alerts", lambda cluster, **kw: [])
    monkeypatch.setattr(daily_digest, "_gather_context_for_chronic",
                        lambda groups, cluster: "")

    async def boom(**kw):
        raise LLMBudgetExceeded("estimated cost $0.80 > budget $0.50")
    monkeypatch.setattr(daily_digest, "triage_digest", boom)

    dispatch_calls = []
    monkeypatch.setattr(daily_digest, "dispatch",
                        lambda f, a, *, db: dispatch_calls.append(f))

    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.50")

    result = await daily_digest.run_async(cluster="dev")
    assert result.findings_emitted == 0
    assert result.error is not None
    assert "budget" in result.error.lower()
    assert dispatch_calls == []


@pytest.mark.asyncio
async def test_daily_digest_passes_log_patterns_to_llm(tmp_path, monkeypatch):
    """P3: log-pattern aggregator output reaches the LLM via triage_digest's
    `log_patterns` kwarg. Runner must call aggregate_log_patterns AND
    forward the result to triage_digest."""
    from cluster_agent.modes import daily_digest
    from cluster_agent.schema import LogPattern, Report

    # Stub Prom history → one chronic group so we don't short-circuit on no alerts
    now = dt.datetime.now(dt.timezone.utc)
    samples = [[(now - dt.timedelta(minutes=i)).timestamp(), "1"] for i in range(120, 0, -1)]
    monkeypatch.setattr(daily_digest, "alertmanager_history",
                        lambda cluster, since_hours=24: {
                            "data": {"result": [{
                                "metric": {"alertname": "KubePodCrashLooping",
                                           "namespace": "pocket-id", "pod": "pocket-id-0"},
                                "values": samples,
                            }]},
                        })
    monkeypatch.setattr(daily_digest, "alertmanager_alerts", lambda cluster, **kw: [])
    monkeypatch.setattr(daily_digest, "_gather_context_for_chronic",
                        lambda groups, cluster: "(stubbed)")

    # Stub the log-pattern aggregator to return one tripwire pattern
    fake_pattern = LogPattern(
        namespace="velero",
        level="panic",
        chronicity="tripwire",
        count_24h=2,
        baseline_mean_24h=None,
        ratio_vs_baseline=None,
        matched_tripwire="panic",
        sample_lines=["panic: runtime error somewhere"],
        first_seen_at=now - dt.timedelta(hours=2),
        last_seen_at=now - dt.timedelta(minutes=5),
    )
    monkeypatch.setattr(daily_digest, "aggregate_log_patterns",
                        lambda cluster, **kw: [fake_pattern])

    # Capture triage_digest kwargs
    captured = {}
    async def fake_triage(**kw):
        captured.update(kw)
        return Report(
            id="01JK3R8Q9M01234567890123XZ", cluster="dev", digest_window_hours=24,
            summary="quiet", quiet_period=True, findings=[],
            total_alerts_24h=1, chronic_count=1, transient_count=0, self_healed_count=0,
        )
    monkeypatch.setattr(daily_digest, "triage_digest", fake_triage)
    monkeypatch.setattr(daily_digest, "dispatch", MagicMock())

    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.75")

    await daily_digest.run_async(cluster="dev")
    # Critical assertion: log_patterns reached triage_digest
    assert "log_patterns" in captured
    assert len(captured["log_patterns"]) == 1
    assert captured["log_patterns"][0].matched_tripwire == "panic"
    assert captured["log_patterns"][0].namespace == "velero"


@pytest.mark.asyncio
async def test_daily_digest_log_mining_failure_does_not_abort(tmp_path, monkeypatch):
    """If aggregate_log_patterns raises, the digest continues with an
    empty log_patterns list — log mining is supplementary, not required."""
    from cluster_agent.modes import daily_digest
    from cluster_agent.schema import Report

    now = dt.datetime.now(dt.timezone.utc)
    samples = [[(now - dt.timedelta(minutes=i)).timestamp(), "1"] for i in range(5, 0, -1)]
    monkeypatch.setattr(daily_digest, "alertmanager_history",
                        lambda cluster, since_hours=24: {
                            "data": {"result": [{
                                "metric": {"alertname": "X"}, "values": samples,
                            }]},
                        })
    monkeypatch.setattr(daily_digest, "alertmanager_alerts", lambda cluster, **kw: [])
    monkeypatch.setattr(daily_digest, "_gather_context_for_chronic",
                        lambda groups, cluster: "")

    # The aggregator BLOWS UP
    def boom(cluster, **kw):
        raise RuntimeError("loki backend exploded")
    monkeypatch.setattr(daily_digest, "aggregate_log_patterns", boom)

    captured = {}
    async def fake_triage(**kw):
        captured.update(kw)
        return Report(
            id="01JK3R8Q9M01234567890123XZ", cluster="dev", digest_window_hours=24,
            summary="quiet", quiet_period=True, findings=[],
            total_alerts_24h=0, chronic_count=0, transient_count=0, self_healed_count=0,
        )
    monkeypatch.setattr(daily_digest, "triage_digest", fake_triage)
    monkeypatch.setattr(daily_digest, "dispatch", MagicMock())

    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.75")

    # Should NOT raise — partial digest is more useful than no digest
    result = await daily_digest.run_async(cluster="dev")
    assert result.error is None
    # And log_patterns reached triage_digest as an empty list (not missing)
    assert captured["log_patterns"] == []


@pytest.mark.asyncio
async def test_daily_digest_passes_open_dedup_keys_to_llm(tmp_path, monkeypatch):
    """Existing open findings in state.db are passed to the LLM as
    `open_issue_keys` so it can avoid creating semantically-duplicate
    issues."""
    from cluster_agent.modes import daily_digest
    from cluster_agent.state import db as db_mod
    from cluster_agent.state.dedup import record

    sdb = db_mod.StateDB(tmp_path / "state.db")
    record(sdb, "alert:KubePodCrashLooping:pocket-id-0:dev",
           gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#7",
           state="open", cluster="dev")
    record(sdb, "alert:KubeJobFailed:velero-1234:dev",
           gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#8",
           state="open", cluster="dev")

    # Stub the GH reconcile (tested separately) so this test stays offline
    # and its two seeded findings stay open → reach the LLM as intended.
    monkeypatch.setattr(daily_digest, "_reconcile_finding_states",
                        lambda sdb, repo: 0)

    now = dt.datetime.now(dt.timezone.utc)
    samples = [[(now - dt.timedelta(minutes=i)).timestamp(), "1"] for i in range(5, 0, -1)]
    monkeypatch.setattr(daily_digest, "alertmanager_history",
                        lambda cluster, since_hours=24: {
                            "data": {"result": [{
                                "metric": {"alertname": "X"}, "values": samples,
                            }]},
                        })
    monkeypatch.setattr(daily_digest, "alertmanager_alerts", lambda cluster, **kw: [])
    monkeypatch.setattr(daily_digest, "_gather_context_for_chronic",
                        lambda groups, cluster: "")

    captured = {}
    async def fake_triage(**kw):
        captured.update(kw)
        from cluster_agent.schema import Report
        return Report(
            id="01JK3R8Q9M01234567890123XZ", cluster="dev", digest_window_hours=24,
            summary="quiet", quiet_period=True, findings=[],
            total_alerts_24h=0, chronic_count=0, transient_count=0, self_healed_count=0,
        )
    monkeypatch.setattr(daily_digest, "triage_digest", fake_triage)
    monkeypatch.setattr(daily_digest, "dispatch", MagicMock())

    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.50")

    await daily_digest.run_async(cluster="dev")
    assert "alert:KubePodCrashLooping:pocket-id-0:dev" in captured["open_issue_keys"]
    assert "alert:KubeJobFailed:velero-1234:dev" in captured["open_issue_keys"]
