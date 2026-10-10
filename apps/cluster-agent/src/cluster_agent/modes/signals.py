"""Signal ids and code-built dedup keys (2026-10-10).

Every alert group, log pattern and known event the digest shows the LLM
carries a `signal_id`: A1, A2, ... for alert groups, L1, ... for log
patterns, E1, ... for known events. A Finding lists the ids it covers in
`Finding.signals`, and its dedup key is built here from those signals:

    alert:<alertname>:<namespace>:<cluster>        (an alert group)
    log:ratio:<namespace>:<cluster>                 (a ratio outlier)
    log:tripwire:<label>:<namespace>:<cluster>      (a tripwire)

Until then the LLM invented the key (`alert:<alertname>:<scope-id>`), and
its scope-id changed from day to day, so a recurring condition filed a
new issue instead of commenting on the open one. The LLM's key is still
the fallback for a finding that cites no alert group or log pattern.

A finding that covers several signals has several candidate keys. The
first one state.db already knows (an open issue, or one closed less than
REOPEN_WINDOW ago) wins, so a condition the LLM groups differently today
still lands on yesterday's issue; with none known, the first in sorted
order (alerts before logs). The others are recorded as aliases of the
chosen key's issue (state.dedup.record_aliases), so tomorrow any one of
them finds it.
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


def _part(value: str) -> str:
    """One `:`-separated key part: no separator inside it, nothing odd."""
    return _UNSAFE.sub("_", value) or "_"


def assign_signal_ids(
    groups: Iterable[AlertGroup],
    patterns: Iterable[LogPattern],
    events: Iterable[KnownEvent],
) -> dict[str, Signal]:
    """Number the signals in their given order and return id → signal."""
    by_id: dict[str, Signal] = {}
    for prefix, items in (("A", groups), ("L", patterns), ("E", events)):
        for n, item in enumerate(items, start=1):
            item.signal_id = f"{prefix}{n}"
            by_id[item.signal_id] = item
    return by_id


def signal_key(signal: Signal, cluster: str) -> str | None:
    """The dedup key a finding on this one signal gets; None for a known
    event (never a finding by itself) and for Watchdog."""
    if isinstance(signal, AlertGroup):
        if signal.alertname in _NO_KEY_ALERTS:
            return None
        labels = signal.labels or {}
        scope = labels.get("namespace") or labels.get("exported_namespace") or "cluster"
        return f"alert:{_part(signal.alertname)}:{_part(scope)}:{cluster}"
    if isinstance(signal, LogPattern):
        if signal.chronicity == "tripwire":
            return (f"log:tripwire:{_part(signal.matched_tripwire or 'unknown')}:"
                    f"{_part(signal.namespace)}:{cluster}")
        return f"log:ratio:{_part(signal.namespace)}:{cluster}"
    return None


def candidate_keys(signal_ids: Iterable[str], by_id: dict[str, Signal], cluster: str) -> list[str]:
    """The keys a finding citing these signals may file under: its alert
    groups' keys sorted, then its log patterns' keys sorted. Ids that name
    no signal (the LLM made one up) and known events are ignored."""
    alert_keys: set[str] = set()
    log_keys: set[str] = set()
    for sid in signal_ids:
        signal = by_id.get(sid)
        if signal is None:
            continue
        key = signal_key(signal, cluster)
        if key is None:
            continue
        (alert_keys if isinstance(signal, AlertGroup) else log_keys).add(key)
    return sorted(alert_keys) + sorted(log_keys)


def choose_dedup_key(db: StateDB, candidates: list[str]) -> str:
    """The first candidate state.db would not CREATE a new issue for (an
    open issue, or a closed one inside the reopen window), else the first."""
    for key in candidates:
        if lookup(db, key).kind != _DedupActionKind.CREATE:
            return key
    return candidates[0]


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
        key = signal_key(signal, cluster)
        if key is None:
            continue
        action = lookup(db, key)
        if action.kind == _DedupActionKind.COMMENT and action.gh_issue_ref:
            signal.tracked_by = action.gh_issue_ref
