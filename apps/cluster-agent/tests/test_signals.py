"""Signal ids and code-built dedup keys (modes/signals.py, state/dedup.py)."""
from __future__ import annotations
import datetime as dt

import pytest

from cluster_agent.modes.signals import (
    assign_signal_ids,
    candidate_keys,
    choose_dedup_key,
    mark_tracked,
    one_finding_per_signal,
    signal_key,
)
from cluster_agent.schema import AlertGroup, Evidence, Finding, KnownEvent, LogPattern
from cluster_agent.state.db import StateDB
from cluster_agent.state.dedup import lookup, record, record_aliases, _DedupActionKind

_T = dt.datetime(2026, 10, 10, 14, 0, tzinfo=dt.timezone.utc)


def _group(alertname="KubeJobFailed", **labels) -> AlertGroup:
    return AlertGroup(alertname=alertname, fingerprint=f"fp-{alertname}", labels=labels,
                      fire_count=1, total_firing_seconds=60, chronicity="transient",
                      first_seen_at=_T, last_seen_at=_T, currently_firing=False)


def _pattern(ns="giks", tripwire=None) -> LogPattern:
    return LogPattern(namespace=ns, level="error", count_24h=99, first_seen_at=_T, last_seen_at=_T,
                      chronicity="tripwire" if tripwire else "ratio_outlier",
                      matched_tripwire=tripwire)


def _event() -> KnownEvent:
    return KnownEvent(kind="node_boot", at=_T, what="node msa2-dev-01 booted")


def _finding(signals, key="alert:LLM:invented:dev") -> Finding:
    return Finding(id="0" * 26, mode="A", cluster="dev", severity="low", title="t", summary="s",
                   evidence=[Evidence(type="alert", ref="Alertmanager/X@now")], confidence=0.5,
                   dedup_key=key, signals=signals)


def test_ids_number_each_kind_in_order():
    groups, patterns, events = [_group("A"), _group("B")], [_pattern()], [_event(), _event()]
    by_id = assign_signal_ids(groups, patterns, events)
    assert [g.signal_id for g in groups] == ["A1", "A2"]
    assert patterns[0].signal_id == "L1"
    assert [e.signal_id for e in events] == ["E1", "E2"]
    assert by_id["A2"] is groups[1] and by_id["E2"] is events[1] and len(by_id) == 5


@pytest.mark.parametrize("signal,key", [
    (_group("KubeJobFailed", namespace="giks-db", job_name="giks-db-offsite-dump-123"),
     "alert:KubeJobFailed:giks-db:dev"),
    (_group("FluxReconcileFailed", exported_namespace="flux-system", name="infra-configs"),
     "alert:FluxReconcileFailed:flux-system:dev"),
    (_group("NodeHardwareSensorsMissing", instance="10.10.5.12:9100"),
     "alert:NodeHardwareSensorsMissing:cluster:dev"),
    (_group("Weird", namespace="a:b c"), "alert:Weird:a_b_c:dev"),
    (_pattern("cnpg-system"), "log:ratio:cnpg-system:dev"),
    (_pattern("giks", tripwire="fatal"), "log:tripwire:fatal:giks:dev"),
])
def test_signal_keys_are_built_from_stable_labels(signal, key):
    """The same alert in the same namespace gets the same key every day:
    no pod name, job name or LLM-chosen scope in it."""
    assert signal_key(signal, "dev") == key


def test_watchdog_and_known_events_have_no_key():
    assert signal_key(_group("Watchdog", namespace="monitoring"), "dev") is None
    assert signal_key(_event(), "dev") is None


def test_candidates_are_alert_keys_then_log_keys_each_sorted_and_unknown_ids_dropped():
    groups = [_group("KubeJobFailed", namespace="giks-db"), _group("CnpgBackupTooOld", namespace="giks-db"),
              _group("Watchdog", namespace="monitoring")]
    patterns = [_pattern("giks-db", tripwire="fatal")]
    by_id = assign_signal_ids(groups, patterns, [_event()])
    assert candidate_keys(["L1", "A1", "E1", "A3", "A9", "A2", "A1"], by_id, "dev") == [
        "alert:CnpgBackupTooOld:giks-db:dev",
        "alert:KubeJobFailed:giks-db:dev",
        "log:tripwire:fatal:giks-db:dev",
    ]
    assert candidate_keys(["E1"], by_id, "dev") == []


def test_choose_prefers_a_key_state_db_knows(tmp_path):
    db = StateDB(tmp_path / "state.db")
    cands = ["alert:A:ns:dev", "alert:B:ns:dev"]
    assert choose_dedup_key(db, cands) == "alert:A:ns:dev"          # none known: the first
    record(db, "alert:B:ns:dev", gh_issue_ref="o/r#5")
    assert choose_dedup_key(db, cands) == "alert:B:ns:dev"          # open issue
    record(db, "alert:B:ns:dev", gh_issue_ref="o/r#5", state="closed",
           closed_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
    assert choose_dedup_key(db, cands) == "alert:B:ns:dev"          # closed inside the reopen window
    record(db, "alert:B:ns:dev", gh_issue_ref="o/r#5", state="closed",
           closed_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30))
    assert choose_dedup_key(db, cands) == "alert:A:ns:dev"          # too old: a new issue


def test_a_signal_belongs_to_the_first_finding_that_cites_it():
    f1, f2 = _finding(["A1", "L1", "A1"]), _finding(["A1", "A2"])
    one_finding_per_signal([f1, f2])
    assert f1.signals == ["A1", "L1"]
    assert f2.signals == ["A2"]


def test_mark_tracked_names_the_open_issue_only(tmp_path):
    db = StateDB(tmp_path / "state.db")
    groups = [_group("KubeJobFailed", namespace="giks-db"), _group("Other", namespace="x")]
    patterns = [_pattern("giks-db", tripwire="fatal")]
    record(db, "alert:KubeJobFailed:giks-db:dev", gh_issue_ref="guntars-rakitko/kube-infra#1600")
    record(db, "log:tripwire:fatal:giks-db:dev", gh_issue_ref="guntars-rakitko/kube-infra#1601",
           state="closed", closed_at=dt.datetime.now(dt.timezone.utc))
    mark_tracked(db, [*groups, *patterns, _event()], "dev")
    assert groups[0].tracked_by == "guntars-rakitko/kube-infra#1600"
    assert groups[1].tracked_by is None
    assert patterns[0].tracked_by is None      # closed is not tracked


def test_aliases_point_at_the_issue_but_never_steal_another_open_one(tmp_path):
    db = StateDB(tmp_path / "state.db")
    record(db, "alert:Busy:ns:dev", gh_issue_ref="o/r#7")                     # open elsewhere
    record(db, "alert:Old:ns:dev", gh_issue_ref="o/r#3", state="closed",
           closed_at=dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc))         # closed long ago
    record(db, "alert:Orphan:ns:dev", gh_issue_ref=None)                      # open, no issue
    record(db, "alert:Same:ns:dev", gh_issue_ref="o/r#9")                     # already on #9
    recorded = record_aliases(
        db, ["alert:New:ns:dev", "alert:Busy:ns:dev", "alert:Old:ns:dev", "alert:Orphan:ns:dev",
             "alert:Same:ns:dev"],
        gh_issue_ref="o/r#9", mode="A", cluster="dev", severity="low", payload_json="{}")
    assert recorded == ["alert:New:ns:dev", "alert:Old:ns:dev", "alert:Orphan:ns:dev", "alert:Same:ns:dev"]
    for key in recorded:
        action = lookup(db, key)
        assert action.kind == _DedupActionKind.COMMENT and action.gh_issue_ref == "o/r#9"
    assert lookup(db, "alert:Busy:ns:dev").gh_issue_ref == "o/r#7"
