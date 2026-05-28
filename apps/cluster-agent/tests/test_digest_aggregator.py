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


# ── P3: Log-pattern aggregator tests ──────────────────────────────


def _loki_metric_response(*pairs) -> dict:
    """Build a Loki metric-form (`matrix`-shaped) response from
    (namespace, total_count) pairs. The aggregator only cares about
    the sum, not bucket distribution, so we put it all in one [ts, v]."""
    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    return {
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": {"namespace": ns}, "values": [[now_ts, str(count)]]}
                for ns, count in pairs
            ],
        },
    }


def _loki_streams_response(*streams) -> dict:
    """Build a Loki streams-shaped response from (namespace, [lines]) tuples."""
    ts = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1e9))
    return {
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"namespace": ns},
                    "values": [[ts, line] for line in lines],
                }
                for ns, lines in streams
            ],
        },
    }


def test_aggregate_log_patterns_ratio_outlier_surfaces():
    """A namespace with 24h count ≥ 3× baseline AND ≥ 50 lines is surfaced
    as a ratio_outlier LogPattern."""
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns

    def metric_query(cluster, query, *, start, end, step_seconds):
        # Heuristic: shorter window = "recent" response; longer = baseline
        window_seconds = (end - start).total_seconds()
        if window_seconds <= 24 * 3600 + 60:
            # Recent 24h: pocket-id has a big spike, velero is normal
            return _loki_metric_response(
                ("pocket-id", 500),    # 500 errors in last 24h
                ("velero", 30),        # below floor, ignored
                ("flux-system", 60),   # at floor; ratio depends on baseline
            )
        else:
            # Baseline 6 days at 1 bucket per day → 6 datapoints per ns
            now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
            return {
                "data": {
                    "result": [
                        {"metric": {"namespace": "pocket-id"},
                         "values": [[now_ts, "50"]] * 6},   # avg 50/day
                        {"metric": {"namespace": "velero"},
                         "values": [[now_ts, "20"]] * 6},
                        {"metric": {"namespace": "flux-system"},
                         "values": [[now_ts, "55"]] * 6},   # avg 55/day; ratio 60/55 ≈ 1.09
                    ],
                },
            }

    def streams_query(cluster, query, *, start, end, limit):
        return _loki_streams_response()   # no tripwires fire in this test

    patterns = aggregate_log_patterns(
        "dev",
        loki_query_fn=streams_query,
        loki_metric_query_fn=metric_query,
    )
    # pocket-id: 500 / 50 = 10× → ratio_outlier
    # velero: count below 50 floor → skipped
    # flux-system: ratio 1.09 (below 3.0) → skipped
    assert len(patterns) == 1
    assert patterns[0].namespace == "pocket-id"
    assert patterns[0].chronicity == "ratio_outlier"
    assert patterns[0].count_24h == 500
    assert patterns[0].ratio_vs_baseline == 10.0


def test_aggregate_log_patterns_tripwire_surfaces_one_occurrence():
    """A tripwire pattern (e.g. panic) is surfaced even on a single
    occurrence — no ratio/floor gating for tripwires."""
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns

    def metric_query(cluster, query, *, start, end, step_seconds):
        # No statistical outliers
        return _loki_metric_response()

    panic_count = {"count": 0}

    def streams_query(cluster, query, *, start, end, limit):
        # Match only when LogQL includes "panic"
        if "panic" in query.lower():
            panic_count["count"] += 1
            ts = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1e9))
            return {
                "data": {
                    "result": [{
                        "stream": {"namespace": "flux-system"},
                        "values": [[ts, "runtime error: invalid memory address — panic"]],
                    }],
                },
            }
        return _loki_streams_response()

    patterns = aggregate_log_patterns(
        "dev",
        loki_query_fn=streams_query,
        loki_metric_query_fn=metric_query,
    )
    tripwires = [p for p in patterns if p.chronicity == "tripwire"]
    assert any(p.matched_tripwire == "panic" for p in tripwires)
    assert any(p.namespace == "flux-system" for p in tripwires)
    panic_pattern = next(p for p in tripwires if p.matched_tripwire == "panic")
    assert panic_pattern.count_24h == 1
    assert len(panic_pattern.sample_lines) == 1


def test_aggregate_log_patterns_redacts_secrets_in_sample_lines():
    """Sample lines containing API keys / passwords / bearer tokens are
    redacted before being included in a LogPattern."""
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns

    def metric_query(cluster, query, *, start, end, step_seconds):
        return _loki_metric_response()

    def streams_query(cluster, query, *, start, end, limit):
        if "panic" in query.lower():
            ts = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1e9))
            return {
                "data": {
                    "result": [{
                        "stream": {"namespace": "secret-leaker"},
                        "values": [
                            [ts, 'panic: AUTH_TOKEN=sk-ant-api03-supersecretvalue123 failed'],
                            [ts, 'panic: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig'],
                            [ts, 'panic: password=hunter2 rejected'],
                        ],
                    }],
                },
            }
        return _loki_streams_response()

    patterns = aggregate_log_patterns(
        "dev",
        loki_query_fn=streams_query,
        loki_metric_query_fn=metric_query,
    )
    panic = next(p for p in patterns if p.matched_tripwire == "panic")
    joined = " ".join(panic.sample_lines)
    # Original secret values must NOT appear; [REDACTED] should be present
    assert "sk-ant-api03-supersecretvalue123" not in joined
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.payload.sig" not in joined
    assert "hunter2" not in joined
    assert "[REDACTED]" in joined


def test_aggregate_log_patterns_quiet_cluster_returns_empty():
    """When no outliers exceed thresholds and no tripwires match,
    the aggregator returns an empty list (no false positives)."""
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns

    def metric_query(cluster, query, *, start, end, step_seconds):
        return _loki_metric_response(("flux-system", 10))   # below floor

    def streams_query(cluster, query, *, start, end, limit):
        return _loki_streams_response()

    patterns = aggregate_log_patterns(
        "dev",
        loki_query_fn=streams_query,
        loki_metric_query_fn=metric_query,
    )
    assert patterns == []


def test_aggregate_log_patterns_tool_failure_does_not_abort():
    """If the Loki metric query raises, the aggregator returns an empty
    list (or just tripwires) rather than blowing up the whole digest."""
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns

    def metric_query(cluster, query, *, start, end, step_seconds):
        raise RuntimeError("loki backend not reachable")

    def streams_query(cluster, query, *, start, end, limit):
        return _loki_streams_response()

    # Should not raise
    patterns = aggregate_log_patterns(
        "dev",
        loki_query_fn=streams_query,
        loki_metric_query_fn=metric_query,
    )
    assert patterns == []


def test_aggregate_log_patterns_caps_at_max_patterns():
    """When more outliers + tripwires would exist than max_patterns,
    the list is truncated. Tripwires take priority (sorted first)."""
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns

    def metric_query(cluster, query, *, start, end, step_seconds):
        if (end - start).total_seconds() <= 24 * 3600 + 60:
            return _loki_metric_response(
                *[(f"ns-{i}", 1000) for i in range(20)],   # 20 ratio outliers
            )
        else:
            now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
            return {
                "data": {
                    "result": [
                        {"metric": {"namespace": f"ns-{i}"},
                         "values": [[now_ts, "10"]] * 6}    # baseline 10/day → ratio 100x
                        for i in range(20)
                    ],
                },
            }

    def streams_query(cluster, query, *, start, end, limit):
        return _loki_streams_response()

    patterns = aggregate_log_patterns(
        "dev",
        loki_query_fn=streams_query,
        loki_metric_query_fn=metric_query,
        max_patterns=5,
    )
    assert len(patterns) == 5


def test_tripwire_logql_excludes_audit_k8s_io_events():
    """Every tripwire query must contain `!= "audit.k8s.io"` so audit
    log events (which contain crash-keyword strings in requestURI/Event
    message fields but are NOT real component crashes) are filtered at
    the Loki layer before reaching the LLM. Regression guard for
    cluster-agent-sandbox #39 + #44 (2026-05-27 Prometheus Operator
    list/watch reconnection storm generated 15× audit events matching
    every tripwire in lockstep).
    """
    from cluster_agent.modes.digest_aggregator import _TRIPWIRE_PATTERNS

    for label, logql in _TRIPWIRE_PATTERNS:
        assert '!= "audit.k8s.io"' in logql, (
            f"tripwire {label!r} missing audit.k8s.io exclusion — "
            f"see #39/#44 incident postmortem"
        )


def test_connection_refused_tripwire_excludes_longhorn_detached_engine_noise():
    """`connection_refused` tripwire must filter out Longhorn manager
    warnings on detached engines. RWO Job volumes (e.g. renovate-cache
    attached to the Renovate CronJob every 2h) cycle attached→detached
    with their Job lifecycle; manager polling the absent engine pod is
    expected lifecycle noise, not a real failure. Regression guard for
    cluster-agent-sandbox #43 (2026-05-28).
    """
    from cluster_agent.modes.digest_aggregator import _TRIPWIRE_PATTERNS

    cr_query = next(q for (label, q) in _TRIPWIRE_PATTERNS if label == "connection_refused")
    assert '!= "Failed to get clone status"' in cr_query
    assert '!= "Failed to get purge status"' in cr_query
