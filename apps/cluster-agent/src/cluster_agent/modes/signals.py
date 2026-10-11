"""Signal ids and code-built dedup keys (2026-10-10).

Every alert group, log pattern and known event the digest shows the LLM
carries a `signal_id`: A1, A2, ... for alert groups, L1, ... for log
patterns, E1, ... for known events. A Finding lists the ids it covers in
`Finding.signals`, and its dedup key is built here from those signals:

    alert:<alertname>:<namespace>[:<workload>]:<cluster>   (an alert group)
    log:ratio:<namespace>:<cluster>                         (a ratio outlier)
    log:tripwire:<label>:<namespace>:<cluster>              (a tripwire)
    event:<kind>:<namespace>:<cluster>                      (a known event alone)

Until then the LLM invented the key (`alert:<alertname>:<scope-id>`), and
its scope-id changed from day to day (`prd-node-17h04`, a Job name with its
run's timestamp), so a recurring condition filed a new issue instead of
commenting on the open one.

The workload part keeps two problems in one namespace apart (the off-site
dump's and the restore drill's KubeJobFailed in `giks-db` are two issues,
and an open issue for one never marks the other "already tracked"). It
comes from the first label that names the alert's OBJECT, in
`_WORKLOAD_LABELS` order. Not `job`, `service` or `instance`: those name
the scrape target (`job="kube-state-metrics"`). `pod` comes after the
object labels because on an alert built from kube-state-metrics or a node
exporter it can be the exporter's own pod. A generated name is reduced to
the object behind it, with Kubernetes' own suffix alphabet, so it is the
same every day: `adminapp-6d9f7c8b5-x2kq4` → `adminapp`,
`giks-db-offsite-dump-29330820-x7k2q` and the Job `…-29330820` →
`giks-db-offsite-dump`. A StatefulSet's ordinal (`pocket-id-0`) stays.

A finding that covers several signals has several candidate keys. The
first one with an OPEN issue wins, then the first with one closed less than
REOPEN_WINDOW ago, then the first in sorted order (alerts before logs), so
a condition the LLM groups differently today still lands on yesterday's
issue. The others are recorded as aliases of the chosen key's issue
(state.dedup.record_aliases) when that issue is new or open, so tomorrow
any one of them finds it. A finding that cites no alert group or log
pattern is matched by its alert evidence, then keyed by the known events
it cites, and only then keeps the LLM's key.
"""
from __future__ import annotations
import re
from typing import Iterable, Union

from ..schema import AlertGroup, Finding, KnownEvent, LogPattern
from ..state.db import StateDB
from ..state.dedup import _DedupActionKind, lookup


Signal = Union[AlertGroup, LogPattern, KnownEvent]

# Never a finding of its own (prompt: "skip silently"), so never a key.
_NO_KEY_ALERTS = frozenset({"Watchdog"})

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

# The labels that name an alert's object, most specific first (module
# docstring). A Flux object's `name` (with `exported_namespace`) is checked
# between the workload kinds and `pod`.
_OBJECT_LABELS = ("cronjob", "job_name", "deployment", "statefulset", "daemonset",
                  "persistentvolumeclaim", "mountpoint")
_AFTER_POD_LABELS = ("route", "reason")

# Kubernetes' generateName and ReplicaSet-hash alphabet (no vowels, no 0/1/3:
# k8s.io/apimachinery/pkg/util/rand), so a real word is never cut.
_K8S_SAFE = "[bcdfghjklmnpqrstvwxz2456789]"
_GENERATED_SUFFIX = re.compile(rf"-{_K8S_SAFE}{{5}}$")       # a pod of a ReplicaSet/DaemonSet/Job
_REPLICASET_HASH = re.compile(rf"-{_K8S_SAFE}{{8,10}}$")     # a ReplicaSet's pod-template hash
_CRONJOB_RUN = re.compile(r"-\d{8,}$")                       # a CronJob's Job: its scheduled minute


def _part(value: str) -> str:
    """One `:`-separated key part: no separator inside it, nothing odd."""
    return _UNSAFE.sub("_", value) or "_"


def workload_of(name: str) -> str:
    """The object behind a generated pod or Job name (module docstring)."""
    name = _GENERATED_SUFFIX.sub("", name)
    name = _REPLICASET_HASH.sub("", name)
    return _CRONJOB_RUN.sub("", name)


def _workload(labels: dict[str, str]) -> str | None:
    for key in _OBJECT_LABELS:
        if labels.get(key):
            return workload_of(labels[key])
    if labels.get("exported_namespace") and labels.get("name"):
        return labels["name"]                       # a Flux object
    if labels.get("pod"):
        return workload_of(labels["pod"])
    for key in _AFTER_POD_LABELS:
        if labels.get(key):
            return labels[key]
    return None


def number_events(events: Iterable[KnownEvent]) -> None:
    """E1, E2, ... in the given order (oldest first). The digest numbers
    them before mining logs: a log pattern names the events it is the
    noise of (digest_aggregator._mark_event_noise)."""
    for n, event in enumerate(events, start=1):
        event.signal_id = f"E{n}"


def assign_signal_ids(
    groups: Iterable[AlertGroup],
    patterns: Iterable[LogPattern],
    events: Iterable[KnownEvent],
) -> dict[str, Signal]:
    """Number the alert groups and log patterns in their given order, and
    the known events unless number_events did; return id → signal."""
    events = list(events)
    if any(e.signal_id is None for e in events):
        number_events(events)
    by_id: dict[str, Signal] = {}
    for prefix, items in (("A", groups), ("L", patterns)):
        for n, item in enumerate(items, start=1):
            item.signal_id = f"{prefix}{n}"
            by_id[item.signal_id] = item
    for event in events:
        if event.signal_id:
            by_id[event.signal_id] = event
    return by_id


def signal_key(signal: Signal, cluster: str) -> str | None:
    """The dedup key a finding on this one signal gets; None for Watchdog.
    A known event's key is used only by a finding that cites nothing else."""
    if isinstance(signal, AlertGroup):
        if signal.alertname in _NO_KEY_ALERTS:
            return None
        labels = signal.labels or {}
        scope = labels.get("namespace") or labels.get("exported_namespace") or "cluster"
        workload = _workload(labels)
        middle = f"{_part(scope)}:{_part(workload)}" if workload else _part(scope)
        return f"alert:{_part(signal.alertname)}:{middle}:{cluster}"
    if isinstance(signal, LogPattern):
        if signal.chronicity == "tripwire":
            return (f"log:tripwire:{_part(signal.matched_tripwire or 'unknown')}:"
                    f"{_part(signal.namespace)}:{cluster}")
        return f"log:ratio:{_part(signal.namespace)}:{cluster}"
    return f"event:{_part(signal.kind)}:{_part(signal.namespace or 'cluster')}:{cluster}"


def candidate_keys(signal_ids: Iterable[str], by_id: dict[str, Signal], cluster: str) -> list[str]:
    """The keys a finding citing these signals may file under: its alert
    groups' keys sorted, then its log patterns' keys sorted. Ids that name
    no signal (the LLM made one up) and known events are ignored here."""
    alert_keys: set[str] = set()
    log_keys: set[str] = set()
    for sid in signal_ids:
        signal = by_id.get(sid)
        if signal is None or isinstance(signal, KnownEvent):
            continue
        key = signal_key(signal, cluster)
        if key is None:
            continue
        (alert_keys if isinstance(signal, AlertGroup) else log_keys).add(key)
    return sorted(alert_keys) + sorted(log_keys)


def event_keys(signal_ids: Iterable[str], by_id: dict[str, Signal], cluster: str) -> list[str]:
    """For a finding that cites only known events (the prompt says an event
    is never a finding by itself, but the LLM may): a stable key per event
    kind and namespace instead of the LLM's invented one."""
    return sorted({signal_key(by_id[sid], cluster) for sid in signal_ids
                   if isinstance(by_id.get(sid), KnownEvent)})


def infer_alert_signals(finding: Finding, groups: Iterable[AlertGroup]) -> list[str]:
    """The alert groups a finding's alert evidence names, when it cites no
    usable id: `Alertmanager/<alertname>@…` matching exactly one group."""
    by_name: dict[str, list[AlertGroup]] = {}
    for g in groups:
        if g.signal_id and g.alertname not in _NO_KEY_ALERTS:
            by_name.setdefault(g.alertname, []).append(g)
    inferred = []
    for ev in finding.evidence:
        if ev.type != "alert":
            continue
        name = ev.ref.split("/", 1)[-1].split("@", 1)[0]
        matches = by_name.get(name, [])
        if len(matches) == 1 and matches[0].signal_id not in inferred:
            inferred.append(matches[0].signal_id)
    return inferred


_ACTION_RANK = {_DedupActionKind.COMMENT: 0, _DedupActionKind.REOPEN: 1, _DedupActionKind.CREATE: 2}


def choose_dedup_key(db: StateDB, candidates: list[str]) -> str:
    """The first candidate with an OPEN issue (a comment), else the first
    with one closed inside the reopen window (a re-fired comment), else the
    first. Open ranks first: a recurrence belongs on the issue still open,
    not on a closed one beside it."""
    return min(candidates, key=lambda k: (_ACTION_RANK[lookup(db, k).kind], candidates.index(k)))


def one_finding_per_signal(findings: list[Finding]) -> None:
    """A signal belongs to the FIRST finding that cites it (the prompt asks
    the LLM for exactly that). Without this, two findings sharing a signal
    would share a candidate key, and the second would become a comment on
    the first's issue."""
    seen: set[str] = set()
    for finding in findings:
        kept = [s for s in dict.fromkeys(finding.signals) if s not in seen]
        seen.update(kept)
        finding.signals = kept


def mark_tracked(db: StateDB, signals: Iterable[Signal], cluster: str) -> None:
    """Set `tracked_by` on each alert group and log pattern whose key has an
    OPEN finding issue, so the LLM and the summary can say so exactly."""
    for signal in signals:
        if isinstance(signal, KnownEvent):
            continue
        key = signal_key(signal, cluster)
        if key is None:
            continue
        action = lookup(db, key)
        if action.kind == _DedupActionKind.COMMENT and action.gh_issue_ref:
            signal.tracked_by = action.gh_issue_ref
