"""Tests for digest_aggregator — Prom ALERTS time-series → AlertGroups.

The classification logic is what's actually load-bearing here: the LLM
trusts the `chronicity` field to skip noise. If we misclassify a
chronic alert as `transient`, real problems silently disappear from
the digest. If we misclassify a transient as `chronic`, noise gets
elevated to a Finding. Tests exercise the boundaries.
"""
from __future__ import annotations
import datetime as dt

import pytest

from cluster_agent.modes.digest_aggregator import (
    aggregate, _classify_chronicity, _fingerprint_of, enrich_with_annotations,
)


def _prom_response(*series: dict) -> dict:
    """Build a fake `query_range` response from per-series dicts."""
    return {"data": {"result": list(series)}}


def _series(alertname: str, labels: dict, sample_timestamps_utc: list[dt.datetime]) -> dict:
    """One Prom timeseries — like ALERTS{alertname=X, ...} would return."""
    return {
        "metric": {"__name__": "ALERTS", "alertstate": "firing", "alertname": alertname, **labels},
        "values": [[ts.timestamp(), "1"] for ts in sample_timestamps_utc],
    }


def _ts_range(start: dt.datetime, count: int, step_seconds: int = 60) -> list[dt.datetime]:
    return [start + dt.timedelta(seconds=i * step_seconds) for i in range(count)]


def test_aggregate_empty_response_returns_empty_list():
    """No alerts fired in window → empty groups."""
    assert aggregate(_prom_response()) == []


def test_aggregate_contiguous_samples_count_as_one_cycle():
    """A run of contiguous 60s samples = 1 fire cycle, total_firing
    = (end - start) seconds."""
    now = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
    start = now - dt.timedelta(hours=2)
    samples = _ts_range(start, 120, step_seconds=60)   # 2h of contiguous samples
    resp = _prom_response(_series("KubePodCrashLooping",
                                  {"namespace": "pocket-id", "pod": "pocket-id-0"},
                                  samples))
    groups = aggregate(resp, step_seconds=60, now=now)
    assert len(groups) == 1
    g = groups[0]
    assert g.fire_count == 1
    # 119 step intervals between 120 samples = 7140s ≈ 2h
    assert g.total_firing_seconds == 7140
    assert g.chronicity == "chronic"   # 2h firing > 1h threshold
    # Last sample was at `start + 119min` = 1min before `now` → currently firing
    assert g.currently_firing is True


def test_aggregate_gap_splits_into_two_cycles():
    """Samples with a 10-min gap = 2 distinct fire cycles → flapping
    if cycle count >= threshold."""
    now = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
    # 3 short fires separated by big gaps
    fire1 = _ts_range(now - dt.timedelta(hours=10), 3, step_seconds=60)
    fire2 = _ts_range(now - dt.timedelta(hours=6), 3, step_seconds=60)
    fire3 = _ts_range(now - dt.timedelta(hours=2), 3, step_seconds=60)
    samples = fire1 + fire2 + fire3
    resp = _prom_response(_series("KubeJobFailed", {"namespace": "velero"}, samples))
    groups = aggregate(resp, step_seconds=60, now=now)
    assert groups[0].fire_count == 3
    assert groups[0].chronicity == "flapping"


def test_classify_chronicity_chronic_beats_flapping():
    """Long firing time wins regardless of cycle count."""
    assert _classify_chronicity(
        fire_count=5, total_firing_seconds=4000, currently_firing=True,
        chronic_threshold_seconds=3600, transient_threshold_seconds=300,
        flapping_threshold_cycles=3,
    ) == "chronic"


def test_classify_chronicity_active_means_currently_firing_short():
    """Currently firing, < 1h cumulative, < 3 cycles → active."""
    assert _classify_chronicity(
        fire_count=1, total_firing_seconds=300, currently_firing=True,
        chronic_threshold_seconds=3600, transient_threshold_seconds=300,
        flapping_threshold_cycles=3,
    ) == "active"


def test_classify_chronicity_transient_means_short_resolved():
    """Single short fire, already resolved → transient (noise)."""
    assert _classify_chronicity(
        fire_count=1, total_firing_seconds=60, currently_firing=False,
        chronic_threshold_seconds=3600, transient_threshold_seconds=300,
        flapping_threshold_cycles=3,
    ) == "transient"


def test_classify_chronicity_self_healed_means_multiple_short_fires():
    """Multiple resolved cycles, doesn't hit flapping threshold → self_healed."""
    assert _classify_chronicity(
        fire_count=2, total_firing_seconds=200, currently_firing=False,
        chronic_threshold_seconds=3600, transient_threshold_seconds=300,
        flapping_threshold_cycles=3,
    ) == "self_healed"


def test_groups_sorted_chronic_first():
    """Sort order: chronic > flapping > active > self_healed > transient.
    The LLM should see the most actionable alerts at the top of the list."""
    now = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
    # Make a transient (1 short fire, resolved long ago)
    transient_samples = [now - dt.timedelta(hours=20)]
    # Make a chronic (2h continuous)
    chronic_samples = _ts_range(now - dt.timedelta(hours=2), 120, step_seconds=60)

    resp = _prom_response(
        _series("Transient", {"x": "1"}, transient_samples),
        _series("Chronic", {"x": "1"}, chronic_samples),
    )
    groups = aggregate(resp, step_seconds=60, now=now)
    assert groups[0].alertname == "Chronic"
    assert groups[1].alertname == "Transient"


def test_fingerprint_stable_across_label_orderings():
    """Sort-by-key ensures the fingerprint doesn't depend on dict iteration order."""
    a = _fingerprint_of("KubePodCrashLooping", {"namespace": "n", "pod": "p"})
    b = _fingerprint_of("KubePodCrashLooping", {"pod": "p", "namespace": "n"})
    assert a == b


def test_enrich_with_annotations_populates_summary():
    """When a group fingerprint matches an active alert, sample_annotation
    is filled from the alert's annotations.summary."""
    now = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.timezone.utc)
    samples = _ts_range(now - dt.timedelta(minutes=5), 5, step_seconds=60)
    resp = _prom_response(_series("KubePodCrashLooping",
                                  {"namespace": "pocket-id", "pod": "pocket-id-0"},
                                  samples))
    groups = aggregate(resp, step_seconds=60, now=now)

    # Matching active alert as AM would return it
    active = [{
        "labels": {"alertname": "KubePodCrashLooping",
                   "namespace": "pocket-id", "pod": "pocket-id-0"},
        "annotations": {"summary": "Pod pocket-id/pocket-id-0 is crash looping"},
    }]
    enrich_with_annotations(groups, active)
    assert groups[0].sample_annotation == "Pod pocket-id/pocket-id-0 is crash looping"
