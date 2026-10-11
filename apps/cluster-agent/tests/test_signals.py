"""Signal ids and code-built dedup keys (modes/signals.py, state/dedup.py)."""
from __future__ import annotations
import datetime as dt

import pytest

from cluster_agent.modes.signals import (
    assign_signal_ids,
    candidate_keys,
    choose_dedup_key,
    event_keys,
    infer_alert_signals,
    mark_tracked,
    number_events,
    one_finding_per_signal,
    signal_key,
    workload_of,
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


def test_events_numbered_before_log_mining_keep_their_ids():
    """The digest numbers the events before mining logs, whose patterns
    name them in `explained_by`: assign_signal_ids must not renumber them,
    whatever order it gets them in."""
    events = [_event(), _event(), _event()]
    number_events(events)
    assert [e.signal_id for e in events] == ["E1", "E2", "E3"]
    by_id = assign_signal_ids([_group()], [_pattern()], [events[2], events[0], events[1]])
    assert [e.signal_id for e in events] == ["E1", "E2", "E3"]
    assert by_id["E1"] is events[0] and by_id["E3"] is events[2]


# Label sets as the clusters' ALERTS carried them (last 30 days, 2026-10-10):
# kube-state-metrics' scrape labels (`job`, `service`, `instance`, its own
# `pod` and `container`) sit beside the object's.
_KSM = {"job": "kube-state-metrics", "service": "kube-prometheus-stack-kube-state-metrics",
        "instance": "10.244.0.7:8080", "endpoint": "http",
        "pod": "kube-prometheus-stack-kube-state-metrics-7d9f6b8c4-x2kq4", "container": "kube-state-metrics"}


@pytest.mark.parametrize("signal,key", [
    (_group("KubeJobFailed", namespace="giks-db", job_name="giks-db-offsite-dump-29330820", **_KSM),
     "alert:KubeJobFailed:giks-db:giks-db-offsite-dump:dev"),
    (_group("KubeJobFailed", namespace="giks-db", job_name="giks-restore-drill-29330880", **_KSM),
     "alert:KubeJobFailed:giks-db:giks-restore-drill:dev"),
    (_group("CnpgOffsiteDumpNeverSucceeded", namespace="giks-db", cronjob="giks-db-offsite-dump"),
     "alert:CnpgOffsiteDumpNeverSucceeded:giks-db:giks-db-offsite-dump:dev"),
    (_group("GiksDeploymentScaledToZero", **{**_KSM, "namespace": "giks", "deployment": "jobserver"}),
     "alert:GiksDeploymentScaledToZero:giks:jobserver:dev"),
    (_group("KubePodCrashLooping", **{**_KSM, "namespace": "giks", "pod": "adminapp-6d9f7c8b5-x2kq4",
                                       "container": "adminapp", "reason": "CrashLoopBackOff"}),
     "alert:KubePodCrashLooping:giks:adminapp:dev"),
    (_group("CnpgInstanceDown", namespace="giks-db", pod="giks-1", job="giks-db/giks"),
     "alert:CnpgInstanceDown:giks-db:giks-1:dev"),
    (_group("FluxReconcileVerySlow", namespace="flux-system", exported_namespace="flux-system",
            name="infra-configs", kind="Kustomization", pod="kustomize-controller-5f8b9c7d6d-kq2x4"),
     "alert:FluxReconcileVerySlow:flux-system:infra-configs:dev"),
    (_group("NodeFilesystemSpaceFillingUp", namespace="monitoring", mountpoint="/var/mnt/loki",
            pod="kube-prometheus-stack-prometheus-node-exporter-bfwbq"),
     "alert:NodeFilesystemSpaceFillingUp:monitoring:_var_mnt_loki:dev"),
    (_group("LokiRequestErrors", route="loki_api_v1_query_range", job="monitoring/loki"),
     "alert:LokiRequestErrors:cluster:loki_api_v1_query_range:dev"),
    (_group("CiliumDropRateHigh", reason="Policy denied"), "alert:CiliumDropRateHigh:cluster:Policy_denied:dev"),
    (_group("TraefikHigh5xxRate", namespace="traefik-admin", service="traefik-admin"),
     "alert:TraefikHigh5xxRate:traefik-admin:dev"),
    (_pattern("cnpg-system"), "log:ratio:cnpg-system:dev"),
    (_pattern("giks", tripwire="fatal"), "log:tripwire:fatal:giks:dev"),
])
def test_signal_keys_are_built_from_stable_labels(signal, key):
    """The same problem gets the same key every day, and two problems in one
    namespace get two keys: the object's name, reduced to its workload; never
    a scrape label (`job="kube-state-metrics"`), never a run's timestamp."""
    assert signal_key(signal, "dev") == key


@pytest.mark.parametrize("name,workload", [
    ("adminapp-6d9f7c8b5-x2kq4", "adminapp"),                       # Deployment pod
    ("cilium-envoy-x7k2q", "cilium-envoy"),                         # DaemonSet pod
    ("giks-db-offsite-dump-29330820-x7k2q", "giks-db-offsite-dump"), # a CronJob's Job's pod
    ("giks-restore-drill-29330880", "giks-restore-drill"),          # a CronJob's Job
    ("pocket-id-0", "pocket-id-0"),                                 # StatefulSet ordinal stays
    ("giks-1", "giks-1"),                                           # CNPG instance stays
    ("kube-apiserver-msa2-dev-01", "kube-apiserver-msa2-dev-01"),   # static pod stays
    ("smtp-proxy", "smtp-proxy"),                                   # a real word is never cut
    ("traefik-tools", "traefik-tools"),
])
def test_workload_of_reduces_generated_names_only(name, workload):
    assert workload_of(name) == workload


def test_watchdog_has_no_key_and_an_event_has_its_own():
    assert signal_key(_group("Watchdog", namespace="monitoring"), "dev") is None
    assert signal_key(_event(), "dev") == "event:node_boot:cluster:dev"


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


def test_choose_prefers_an_open_issue_over_a_closed_one(tmp_path):
    """Review finding: with X closed two days ago and Y open, the sorted
    order picked X, so the recurrence landed on the closed issue."""
    db = StateDB(tmp_path / "state.db")
    record(db, "alert:X:ns:dev", gh_issue_ref="o/r#100", state="closed",
           closed_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
    record(db, "alert:Y:ns:dev", gh_issue_ref="o/r#101")
    assert choose_dedup_key(db, ["alert:X:ns:dev", "alert:Y:ns:dev"]) == "alert:Y:ns:dev"
    assert choose_dedup_key(db, ["alert:X:ns:dev", "alert:Z:ns:dev"]) == "alert:X:ns:dev"


def test_alert_evidence_names_the_group_when_no_id_is_cited():
    groups = [_group("KubeJobFailed", namespace="giks-db"), _group("PodRestartingFrequently", namespace="a"),
              _group("PodRestartingFrequently", namespace="b"), _group("Watchdog")]
    assign_signal_ids(groups, [], [])
    f = Finding(id="0" * 26, mode="A", cluster="dev", severity="low", title="t", summary="s",
                evidence=[Evidence(type="alert", ref="Alertmanager/KubeJobFailed@2026-10-10T04:00:00Z"),
                          Evidence(type="alert", ref="Alertmanager/PodRestartingFrequently@x"),  # ambiguous
                          Evidence(type="alert", ref="Alertmanager/Watchdog@x"),
                          Evidence(type="log", ref="loki:{namespace='giks-db'}")],
                confidence=0.5, dedup_key="k")
    assert infer_alert_signals(f, groups) == ["A1"]


def test_a_finding_on_events_alone_gets_an_event_key():
    events = [_event(), KnownEvent(kind="helm_upgrade", at=_T, what="x", namespace="kube-system")]
    by_id = assign_signal_ids([], [], events)
    assert event_keys(["E2", "E1", "A9"], by_id, "dev") == [
        "event:helm_upgrade:kube-system:dev", "event:node_boot:cluster:dev"]


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
