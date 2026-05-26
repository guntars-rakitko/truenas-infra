"""Mode A runner — full flow with stubbed Alertmanager + LLM + dispatch."""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "mode_a"


@pytest.mark.asyncio
async def test_mode_a_create_path(tmp_path, monkeypatch):
    """End-to-end Mode A run on a single active alert with no prior dedup
    state → calls Alertmanager, gathers context, calls LLM, dispatches
    to all 3 surfaces with action=create."""
    from cluster_agent.modes import alert_triage

    # Stub: Alertmanager returns one alert
    alert = json.loads((FIXTURES / "alert_pod_oom.json").read_text())
    monkeypatch.setattr(
        alert_triage,
        "alertmanager_alerts",
        lambda cluster, **kw: [alert],
    )

    # Stub: context-gather returns canned blob
    monkeypatch.setattr(
        alert_triage,
        "gather_context_for_alert",
        lambda alert, cluster, window_min=30: {
            "loki_excerpt": "stub log",
            "kubectl_describe": "stub describe",
            "prom_values": "stub prom",
            "flux_state": "stub flux",
        },
    )

    # Stub: LLM returns the canned response
    canned_response = (FIXTURES / "llm_response_pod_oom.json").read_text()
    from cluster_agent import llm
    async def fake_sdk_query(prompt, options):
        return canned_response
    monkeypatch.setattr(llm, "_sdk_query", fake_sdk_query)

    # Stub: Grafana + GH (alert_triage delegates to dispatch.dispatch,
    # so patch the names where dispatch resolves them)
    from cluster_agent import dispatch as d_mod
    monkeypatch.setattr(d_mod, "post_annotation", lambda **kw: "1001")
    monkeypatch.setattr(d_mod, "gh_issue_create",
                        lambda repo, title, body, labels=None: {"number": 7})
    monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MODE_A_BUDGET_USD", "0.50")
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))

    result = await alert_triage.run_async(cluster="dev")
    assert result.findings_emitted == 1
    assert result.findings_skipped_dedup == 0
    assert result.alerts_seen == 1


@pytest.mark.asyncio
async def test_mode_a_dedup_skips_recent_open_issue(tmp_path, monkeypatch):
    """Second run on the SAME alert with an open issue in SQLite → action
    is COMMENT, no new issue created."""
    from cluster_agent.modes import alert_triage
    from cluster_agent.state import db as db_mod
    from cluster_agent.state.dedup import record

    # Pre-seed SQLite with an open issue for the dedup_key the LLM will return
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    sdb = db_mod.StateDB(tmp_path / "state.db")
    record(sdb, "alert:KubePodCrashLooping:pocket-id-0:dev",
           gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#5",
           state="open")

    alert = json.loads((FIXTURES / "alert_pod_oom.json").read_text())
    monkeypatch.setattr(alert_triage, "alertmanager_alerts", lambda cluster, **kw: [alert])
    monkeypatch.setattr(alert_triage, "gather_context_for_alert",
                        lambda alert, cluster, window_min=30: {
                            "loki_excerpt": "", "kubectl_describe": "",
                            "prom_values": "", "flux_state": "",
                        })
    from cluster_agent import llm
    async def fake_sdk_query(prompt, options):
        return (FIXTURES / "llm_response_pod_oom.json").read_text()
    monkeypatch.setattr(llm, "_sdk_query", fake_sdk_query)

    from cluster_agent import dispatch as d_mod
    gh_create = MagicMock()
    gh_comment = MagicMock(return_value={"id": 999})
    monkeypatch.setattr(d_mod, "gh_issue_create", gh_create)
    monkeypatch.setattr(d_mod, "gh_issue_comment", gh_comment)
    monkeypatch.setattr(d_mod, "post_annotation", lambda **kw: "1002")
    monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MODE_A_BUDGET_USD", "0.50")

    await alert_triage.run_async(cluster="dev")
    assert gh_create.called is False
    assert gh_comment.called is True


@pytest.mark.asyncio
async def test_mode_a_tick_skip_when_alert_set_unchanged(tmp_path, monkeypatch):
    """Tick-level cost gate (2026-05-26): if the active alert set hash
    matches the previous tick AND every alert already has an open GH
    issue, the runner must NOT call the LLM at all.

    Setup mirrors the first run (creates state.db rows + sets the
    tick-state row), then runs a second time with the same alert and
    asserts the LLM was never called."""
    from cluster_agent.modes import alert_triage
    from cluster_agent.state import db as db_mod
    from cluster_agent.state.dedup import record

    alert = json.loads((FIXTURES / "alert_pod_oom.json").read_text())
    monkeypatch.setattr(alert_triage, "alertmanager_alerts", lambda cluster, **kw: [alert])
    monkeypatch.setattr(alert_triage, "gather_context_for_alert",
                        lambda alert, cluster, window_min=30: {
                            "loki_excerpt": "", "kubectl_describe": "",
                            "prom_values": "", "flux_state": "",
                        })

    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MODE_A_BUDGET_USD", "0.50")

    # Pre-seed: the alert already has an open GH issue under the
    # conservative dedup key (this matches what the previous tick would
    # have written after creating the issue + recording tick state).
    sdb = db_mod.StateDB(tmp_path / "state.db")
    conservative_key = alert_triage._conservative_dedup_key(alert, "dev")
    record(sdb, conservative_key,
           gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#5",
           state="open")
    # Pre-seed: matching tick state row from "previous tick"
    h = alert_triage._hash_alert_set([alert])
    sdb.execute(
        "INSERT INTO mode_a_tick_state VALUES (?, ?, ?, ?)",
        ("dev", h, "2026-05-26T00:00:00+00:00", 1),
    )

    # If the LLM is called, that's the bug we're catching.
    from cluster_agent import llm as llm_mod
    async def boom(prompt, options):
        raise AssertionError("LLM should not be called — alert set unchanged")
    monkeypatch.setattr(llm_mod, "_sdk_query", boom)

    result = await alert_triage.run_async(cluster="dev")
    # The runner reported the alert as seen + skipped without emitting
    # a finding (since no LLM call was made).
    assert result.alerts_seen == 1
    assert result.findings_emitted == 0
    assert result.findings_skipped_dedup == 1


@pytest.mark.asyncio
async def test_mode_a_tick_skip_does_not_skip_when_issue_closed(tmp_path, monkeypatch):
    """If the alert set hash matches but the operator closed the GH
    issue (state='closed' in SQLite), the runner must fall through to
    the normal flow so the alert can be RE-OPENED.

    Without this guard the alert would silently never re-fire."""
    from cluster_agent.modes import alert_triage
    from cluster_agent.state import db as db_mod
    from cluster_agent.state.dedup import record

    alert = json.loads((FIXTURES / "alert_pod_oom.json").read_text())
    monkeypatch.setattr(alert_triage, "alertmanager_alerts", lambda cluster, **kw: [alert])
    monkeypatch.setattr(alert_triage, "gather_context_for_alert",
                        lambda alert, cluster, window_min=30: {
                            "loki_excerpt": "", "kubectl_describe": "",
                            "prom_values": "", "flux_state": "",
                        })
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
    monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")
    monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
    monkeypatch.setenv("MODE_A_BUDGET_USD", "0.50")

    sdb = db_mod.StateDB(tmp_path / "state.db")
    conservative_key = alert_triage._conservative_dedup_key(alert, "dev")
    # Issue exists but was CLOSED by the operator
    record(sdb, conservative_key,
           gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#5",
           state="closed")
    h = alert_triage._hash_alert_set([alert])
    sdb.execute(
        "INSERT INTO mode_a_tick_state VALUES (?, ?, ?, ?)",
        ("dev", h, "2026-05-26T00:00:00+00:00", 1),
    )

    # LLM is expected to be called; stub returns canned response
    from cluster_agent import llm as llm_mod
    async def fake(prompt, options):
        return (FIXTURES / "llm_response_pod_oom.json").read_text()
    monkeypatch.setattr(llm_mod, "_sdk_query", fake)
    from cluster_agent import dispatch as d_mod
    monkeypatch.setattr(d_mod, "post_annotation", lambda **kw: "9")
    monkeypatch.setattr(d_mod, "gh_issue_comment", MagicMock())
    monkeypatch.setattr(d_mod, "gh_issue_create", MagicMock())

    result = await alert_triage.run_async(cluster="dev")
    # Critical: the LLM WAS invoked (not skipped) — operator-closed
    # issues mustn't be silently shadowed by the tick-hash dedup.
    assert result.findings_emitted == 1


def test_run_sync_wraps_run_async(monkeypatch):
    """run() is the sync entrypoint scheduler.add_mode expects; it must
    run the async coroutine to completion in a fresh event loop."""
    from cluster_agent.modes import alert_triage

    called = {}

    async def fake_async(cluster):
        called["cluster"] = cluster
        return alert_triage.ModeResult(alerts_seen=0, findings_emitted=0, findings_skipped_dedup=0)

    monkeypatch.setattr(alert_triage, "run_async", fake_async)
    result = alert_triage.run(cluster="dev")
    assert called["cluster"] == "dev"
    assert result.alerts_seen == 0
