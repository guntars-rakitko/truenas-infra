"""Tests for daily_digest runner — orchestration of the 24h pipeline."""
from __future__ import annotations
import datetime as dt
import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _no_prometheus(monkeypatch):
    """run_async reads namespace ages from Prometheus and the known events
    from Prometheus and kubectl: no test reaches a cluster."""
    from cluster_agent.modes import daily_digest
    monkeypatch.setattr(daily_digest, "prometheus_query",
                        lambda cluster, promql: {"data": {"result": [
                            {"metric": {"namespace": "giks"}, "value": [1791676800, "1791504000"]}]}})
    monkeypatch.setattr(daily_digest, "gather_known_events", lambda cluster, **kw: [])


def test_an_empty_namespace_age_answer_raises(monkeypatch):
    """No namespace at all (kube-state-metrics down) is no answer: it must
    not read as "every namespace is old", silently."""
    from cluster_agent.modes import daily_digest
    monkeypatch.setattr(daily_digest, "prometheus_query",
                        lambda cluster, promql: {"data": {"result": []}})
    with pytest.raises(LookupError):
        daily_digest._namespace_created("dev")


def test_namespace_created_reads_kube_state_metrics(monkeypatch):
    from cluster_agent.modes import daily_digest
    seen = {}

    def fake(cluster, promql):
        seen["promql"] = promql
        return {"data": {"result": [
            {"metric": {"namespace": "sms-gateway"}, "value": [1791676800, "1791504000"]},
            {"metric": {}, "value": [1791676800, "1"]},
        ]}}
    monkeypatch.setattr(daily_digest, "prometheus_query", fake)
    assert daily_digest._namespace_created("dev") == {
        "sms-gateway": dt.datetime.fromtimestamp(1791504000, tz=dt.timezone.utc)}
    assert seen["promql"] == "max by (namespace) (kube_namespace_created)"


@pytest.mark.asyncio
async def test_a_failed_namespace_age_read_is_a_coverage_gap(tmp_path, monkeypatch):
    """Without namespace ages the aggregator judges every namespace on the full
    window, and the summary must say the ages were not read."""
    from cluster_agent.modes import daily_digest
    from cluster_agent.schema import Report

    now = dt.datetime.now(dt.timezone.utc)
    samples = [[(now - dt.timedelta(minutes=i)).timestamp(), "1"] for i in range(5, 0, -1)]
    monkeypatch.setattr(daily_digest, "alertmanager_history",
                        lambda cluster, since_hours=24: {"data": {"result": [
                            {"metric": {"alertname": "X"}, "values": samples}]}})
    monkeypatch.setattr(daily_digest, "alertmanager_alerts", lambda cluster, **kw: [])
    monkeypatch.setattr(daily_digest, "_gather_context_for_chronic", lambda groups, cluster: "")

    def no_prometheus(cluster, promql):
        raise RuntimeError("prometheus unreachable")
    monkeypatch.setattr(daily_digest, "prometheus_query", no_prometheus)
    seen = {}

    def fake_aggregate(cluster, **kw):
        seen.update(kw)
        return []
    monkeypatch.setattr(daily_digest, "aggregate_log_patterns", fake_aggregate)

    async def fake_triage(**kw):
        return Report(
            id="01JK3R8Q9M01234567890123XZ", cluster="dev", digest_window_hours=24,
            summary="quiet", quiet_period=True, findings=[],
            total_alerts_24h=1, chronic_count=0, transient_count=1, self_healed_count=0,
        )
    monkeypatch.setattr(daily_digest, "triage_digest", fake_triage)
    monkeypatch.setattr(daily_digest, "dispatch", MagicMock())
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.75")

    result = await daily_digest.run_async(cluster="dev")
    assert result.error is None
    assert seen["namespace_created"] is None
    assert seen["failures"] == ["namespace ages: RuntimeError"]


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

    from prometheus_client import REGISTRY
    before = dt.datetime.now().timestamp()
    result = await daily_digest.run_async(cluster="dev")
    assert result.quiet_period is True
    assert result.findings_emitted == 0
    assert result.alert_groups_seen == 0
    assert "No alerts fired" in result.summary
    # A quiet day is a success, so the liveness gauge moves too.
    stamp = REGISTRY.get_sample_value(
        "cluster_agent_last_success_timestamp", {"mode": "A"})
    assert stamp is not None and stamp >= before


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


def _stub_digest_inputs(daily_digest, monkeypatch, tmp_path):
    """One active alert group, no AM annotations, no context, quiet LLM."""
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

    async def fake_triage(**kw):
        return Report(
            id="01JK3R8Q9M01234567890123XZ", cluster="dev", digest_window_hours=24,
            summary="quiet", quiet_period=True, findings=[],
            total_alerts_24h=1, chronic_count=0, transient_count=0, self_healed_count=0,
        )
    monkeypatch.setattr(daily_digest, "triage_digest", fake_triage)
    monkeypatch.setattr(daily_digest, "dispatch", MagicMock())
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.75")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["per-query", "whole-aggregator"])
async def test_daily_digest_forwards_log_mining_failures_to_summary(tmp_path, monkeypatch, mode):
    """Failed Loki queries must reach the daily summary, whether one
    tripwire failed (reported by the aggregator) or the whole aggregator
    raised (reported by the runner)."""
    from cluster_agent.modes import daily_digest, summary_issue

    _stub_digest_inputs(daily_digest, monkeypatch, tmp_path)

    if mode == "per-query":
        def fake_mining(cluster, *, failures, **kw):
            failures.append("tripwire OOMKilled: ReadTimeout")
            return []
        expected = ["tripwire OOMKilled: ReadTimeout"]
    else:
        def fake_mining(cluster, **kw):
            raise RuntimeError("loki backend exploded")
        expected = ["log-pattern mining: RuntimeError"]
    monkeypatch.setattr(daily_digest, "aggregate_log_patterns", fake_mining)

    captured = {}
    monkeypatch.setattr(summary_issue, "emit_summary",
                        lambda **kw: captured.update(kw) or {})

    result = await daily_digest.run_async(cluster="dev")
    assert result.error is None
    assert captured["log_mining_failures"] == expected


# ── Signals, code-built dedup keys and known events (2026-10-10) ──────

def _history(*alerts):
    """Two hours of firing per (alertname, namespace)."""
    now = dt.datetime.now(dt.timezone.utc)
    samples = [[(now - dt.timedelta(minutes=i)).timestamp(), "1"] for i in range(120, 0, -1)]
    return {"data": {"result": [
        {"metric": {"alertname": a, "namespace": ns}, "values": samples} for a, ns in alerts]}}


def _finding(signals, key):
    from cluster_agent.schema import Evidence, Finding
    return Finding(id="0" * 26, mode="A", cluster="dev", severity="medium", title="t", summary="s",
                   evidence=[Evidence(type="alert", ref="Alertmanager/X@now")], confidence=0.6,
                   dedup_key=key, signals=signals)


def _report(findings):
    from cluster_agent.schema import Report
    return Report(id="1" * 26, cluster="dev", digest_window_hours=24, summary="s",
                  quiet_period=not findings, findings=findings, total_alerts_24h=2,
                  chronic_count=2, transient_count=0, self_healed_count=0)


def _fake_dispatch(calls, numbers):
    """Records the action and writes state.db the way dispatch() does."""
    from cluster_agent.dispatch import DispatchResult
    from cluster_agent.state.dedup import _DedupActionKind, record

    def fake(finding, action, *, db):
        ref = action.gh_issue_ref
        if action.kind == _DedupActionKind.CREATE:
            ref = f"guntars-rakitko/kube-infra#{numbers.pop(0)}"
        calls.append((finding.dedup_key, action.kind.value, ref))
        record(db, finding.dedup_key, gh_issue_ref=ref, cluster=finding.cluster)
        return DispatchResult(finding_id=finding.id, gh_issue_ref=ref, grafana_annotation_id=None)
    return fake


async def _run_day(daily_digest, monkeypatch, alerts, findings, calls, numbers):
    monkeypatch.setattr(daily_digest, "alertmanager_history", lambda cluster, since_hours=24: _history(*alerts))
    monkeypatch.setattr(daily_digest, "alertmanager_alerts", lambda cluster, **kw: [])
    monkeypatch.setattr(daily_digest, "_gather_context_for_chronic", lambda groups, cluster: "")
    monkeypatch.setattr(daily_digest, "aggregate_log_patterns", lambda cluster, **kw: [])
    monkeypatch.setattr(daily_digest, "_reconcile_finding_states", lambda sdb, repo: 0)
    seen = {}

    async def fake_triage(**kw):
        seen.update(kw)
        return _report(findings(kw["alert_groups"]))
    monkeypatch.setattr(daily_digest, "triage_digest", fake_triage)
    monkeypatch.setattr(daily_digest, "dispatch", _fake_dispatch(calls, numbers))
    result = await daily_digest.run_async(cluster="dev")
    assert result.error is None
    return seen


@pytest.mark.asyncio
async def test_a_finding_files_under_its_signals_key_and_tomorrow_lands_on_the_same_issue(tmp_path, monkeypatch):
    """Day 1: one finding covers two alerts; its key is built from them,
    whatever the LLM invented, and the other alert becomes an alias.
    Day 2: only the second alert fires, and its finding comments on the
    day-1 issue instead of filing a new one."""
    from cluster_agent.modes import daily_digest
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("DAILY_DIGEST_BUDGET_USD", "0.75")
    calls: list[tuple[str, str, str]] = []
    numbers = [1601, 1602]

    def day1(groups):
        ids = [g.signal_id for g in groups]
        return [_finding(ids, "alert:Whatever:the-llm-said:prd"), _finding([], "alert:Free:form:dev")]
    await _run_day(daily_digest, monkeypatch,
                   [("KubeJobFailed", "giks-db"), ("CnpgBackupTooOld", "giks-db")], day1, calls, numbers)
    assert calls == [
        ("alert:CnpgBackupTooOld:giks-db:dev", "create", "guntars-rakitko/kube-infra#1601"),
        ("alert:Free:form:dev", "create", "guntars-rakitko/kube-infra#1602"),   # no signals: the LLM's key
    ]

    calls.clear()
    seen = await _run_day(daily_digest, monkeypatch, [("KubeJobFailed", "giks-db")],
                          lambda groups: [_finding([groups[0].signal_id], "alert:Other:name:dev")],
                          calls, numbers)
    assert calls == [("alert:KubeJobFailed:giks-db:dev", "comment", "guntars-rakitko/kube-infra#1601")]
    # ... and the LLM was told, before it answered, that an open issue tracks it.
    assert seen["alert_groups"][0].tracked_by == "guntars-rakitko/kube-infra#1601"


@pytest.mark.asyncio
async def test_known_events_reach_the_llm_and_the_summary_with_their_gaps(tmp_path, monkeypatch):
    from cluster_agent.modes import daily_digest, summary_issue
    from cluster_agent.schema import KnownEvent
    from cluster_agent.tools import audit

    _stub_digest_inputs(daily_digest, monkeypatch, tmp_path)
    event = KnownEvent(kind="node_boot", at=dt.datetime.now(dt.timezone.utc), what="node msa2-dev-01 booted")
    seen_window = {}

    def fake_events(cluster, *, start, end, failures, **kw):
        seen_window.update(start=start, end=end)
        failures.append("known events helm upgrades: ToolError")
        return [event]
    monkeypatch.setattr(daily_digest, "gather_known_events", fake_events)
    captured_llm, captured_summary = {}, {}

    async def fake_triage(**kw):
        captured_llm.update(kw)
        return _report([])
    monkeypatch.setattr(daily_digest, "triage_digest", fake_triage)
    monkeypatch.setattr(summary_issue, "emit_summary", lambda **kw: captured_summary.update(kw) or {})

    result = await daily_digest.run_async(cluster="dev")
    assert result.error is None
    assert seen_window["end"] - seen_window["start"] == dt.timedelta(hours=24)
    assert captured_llm["known_events"] == [event] and event.signal_id == "E1"
    assert captured_llm["known_event_failures"] == ["known events helm upgrades: ToolError"]
    assert captured_summary["known_events"] == [event]
    assert captured_summary["known_event_failures"] == ["known events helm upgrades: ToolError"]
    # Every tool call of the run logged one run id.
    assert audit.get_run_id().startswith("A-dev-")


@pytest.mark.asyncio
async def test_a_known_events_crash_is_a_gap_not_an_abort(tmp_path, monkeypatch):
    from cluster_agent.modes import daily_digest, summary_issue

    _stub_digest_inputs(daily_digest, monkeypatch, tmp_path)

    def boom(cluster, **kw):
        raise RuntimeError("bug")
    monkeypatch.setattr(daily_digest, "gather_known_events", boom)
    captured = {}
    monkeypatch.setattr(summary_issue, "emit_summary", lambda **kw: captured.update(kw) or {})
    result = await daily_digest.run_async(cluster="dev")
    assert result.error is None
    assert captured["known_events"] == []
    assert captured["known_event_failures"] == ["known events: RuntimeError"]
