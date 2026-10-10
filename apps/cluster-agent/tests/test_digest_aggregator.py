"""Tests for digest_aggregator — Prom ALERTS time-series → AlertGroups.

The classification logic is what's actually load-bearing here: the LLM
trusts the `chronicity` field to skip noise. If we misclassify a
chronic alert as `transient`, real problems silently disappear from
the digest. If we misclassify a transient as `chronic`, noise gets
elevated to a Finding. Tests exercise the boundaries.
"""
from __future__ import annotations
import datetime as dt
import json
import re

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


# ── P3 log-pattern mining ────────────────────────────────────────────
#
# The aggregator's Loki calls are answered by _SimLoki, which evaluates
# `count_over_time(…[Nh])` over hourly data the way Loki does, INCLUDING
# `align_queries_with_step: true` (kube-infra loki.yaml): start and end are
# rounded down to a multiple of the step before the points are laid out.
# That rounding is what made the old 24h-step query count two days, so a
# regression to it fails here, not only in production.

_NOW = dt.datetime(2026, 10, 11, 3, 1, 30, tzinfo=dt.timezone.utc)


def _hours_back(n: int) -> dt.datetime:
    """The hour that ends n hours before the window's end (0 = the last full
    hour before _NOW: the window is 0..23, its baseline 24..167)."""
    return _NOW.replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=n)


class _SimLoki:
    """Hourly data per (query kind, namespace): {kind: {ns: {hour_end: count}}}.

    A query's kind is "errors" (_ERROR_LINE_FILTER), "postgres"
    (_POSTGRES_ERROR_FILTER) or a tripwire label, found from its filter.
    `samples[(label, ns)]` answers the per-namespace sample queries.
    `raise_on` holds kinds (or "samples") whose queries raise."""

    def __init__(self, hourly=None, samples=None, raise_on=()):
        self.hourly = hourly or {}
        self.samples = samples or {}
        self.raise_on = set(raise_on)
        self.metric_calls: list[tuple[str, dt.datetime, dt.datetime, int]] = []
        self.stream_calls: list[tuple[str, dt.datetime, dt.datetime, int]] = []

    @staticmethod
    def kind_of(query: str) -> str:
        from cluster_agent.modes import digest_aggregator as da
        if da._POSTGRES_ERROR_FILTER in query:
            return "postgres"
        if da._ERROR_LINE_FILTER in query:
            return "errors"
        for label, line_filter in da._TRIPWIRE_PATTERNS:
            if line_filter in query:
                return label
        raise AssertionError(f"unknown query: {query[:120]}")

    def metric(self, cluster, query, *, start, end, step_seconds):
        self.metric_calls.append((query, start, end, step_seconds))
        kind = self.kind_of(query)
        if kind in self.raise_on:
            raise TimeoutError("read timed out")
        rng = int(re.search(r"\[(\d+)h\]\)\)$", query).group(1)) * 3600
        step = dt.timedelta(seconds=step_seconds)
        epoch = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
        t = start - (start - epoch) % step          # align_queries_with_step
        last = end - (end - epoch) % step
        result = []
        for ns, hours in self.hourly.get(kind, {}).items():
            values = []
            while t <= last:
                n = sum(c for h, c in hours.items() if t - dt.timedelta(seconds=rng) < h <= t)
                if n:
                    values.append([t.timestamp(), str(n)])
                t += step
            t = start - (start - epoch) % step
            if values:
                result.append({"metric": {"namespace": ns}, "values": values})
        return {"data": {"resultType": "matrix", "result": result}}

    def streams(self, cluster, query, *, start, end, limit):
        self.stream_calls.append((query, start, end, limit))
        if "samples" in self.raise_on:
            raise TimeoutError("read timed out")
        ns = re.match(r'\{namespace="([^"]+)"\}', query).group(1)
        lines = self.samples.get((self.kind_of(query), ns), [])[:limit]
        ts = str(int(_NOW.timestamp() * 1e9))
        result = [{"stream": {"namespace": ns}, "values": [[ts, l] for l in lines]}] if lines else []
        return {"data": {"resultType": "streams", "result": result}}


def _run(sim, **kw):
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns
    return aggregate_log_patterns(
        "dev", loki_query_fn=sim.streams, loki_metric_query_fn=sim.metric, now=_NOW, **kw,
    )


def _spread(total: int, first: int, last: int) -> dict[dt.datetime, int]:
    """`total` lines evenly over the hours ending `first`..`last` hours back."""
    hours = list(range(first, last + 1))
    out = {_hours_back(h): total // len(hours) for h in hours}
    out[_hours_back(first)] += total - sum(out.values())
    return out


def test_hour_windows_are_aligned_and_exact():
    from cluster_agent.modes.digest_aggregator import _hour_windows
    recent_start, end, baseline_start, baseline_end = _hour_windows(_NOW, window_hours=24, baseline_days=7)
    utc = dt.timezone.utc
    assert end == dt.datetime(2026, 10, 11, 3, 0, tzinfo=utc)
    assert recent_start == dt.datetime(2026, 10, 10, 4, 0, tzinfo=utc)     # 24 points
    assert baseline_end == dt.datetime(2026, 10, 10, 3, 0, tzinfo=utc)
    assert baseline_start == dt.datetime(2026, 10, 4, 4, 0, tzinfo=utc)    # 144 points


def test_ratio_counts_each_window_once():
    """The window is the 24 full hours before the run, counted once: until
    2026-10-10 a 24h-step query, rounded to two UTC midnights by Loki, added
    two days (#1534/#1537: 43,307 for 21,742)."""
    sim = _SimLoki({"errors": {
        "kyverno": {**_spread(2400, 0, 23),      # the window: 100/h
                    **_spread(14400, 24, 167)},  # the six days before: 100/h
    }})
    failures: list[str] = []
    patterns = _run(sim, failures=failures, min_count_floor=1, ratio_threshold=0.5)
    assert failures == []
    (p,) = patterns
    assert p.count_24h == 2400
    assert p.baseline_mean_24h == 2400.0
    assert p.ratio_vs_baseline == 1.0
    assert {(c[3], c[1].minute, c[2].minute) for c in sim.metric_calls} == {(3600, 0, 0)}


def test_ratio_outlier_surfaces_against_a_daily_baseline():
    """A namespace at ≥ 3× its daily baseline and ≥ 50 lines surfaces;
    below the floor or the ratio, it does not."""
    sim = _SimLoki({
        "errors": {
            "pocket-id": {**_spread(500, 0, 23), **_spread(300, 24, 167)},    # 500 vs 50/day
            "flux-system": {**_spread(60, 0, 23), **_spread(330, 24, 167)},   # 60 vs 55/day
            "quiet": _spread(30, 0, 23),                                      # under the floor
        },
        "postgres": {"pocket-id": _spread(100, 0, 23)},                       # summed in
    })
    patterns = _run(sim)
    assert [(p.namespace, p.chronicity, p.count_24h, p.ratio_vs_baseline) for p in patterns] == [
        ("pocket-id", "ratio_outlier", 600, 12.0),
    ]


def test_a_new_namespace_is_no_ratio_outlier_until_it_has_a_baseline():
    """#1486: on the SMS gateway's first day, 459 lines against a baseline
    of 0 read as 459×. A namespace needs `min_history_days` of baseline."""
    sim = _SimLoki({"errors": {
        "sms-gateway": _spread(459, 0, 23),
        "five-days-old": {**_spread(500, 0, 23), **_spread(400, 24, 119)},
        "old-and-quiet": _spread(459, 0, 23),
    }})
    window_start = _hours_back(24)
    patterns = _run(sim, namespace_created={
        "sms-gateway": window_start - dt.timedelta(days=1),
        "five-days-old": window_start - dt.timedelta(days=4),
        "old-and-quiet": window_start - dt.timedelta(days=30),
    })
    got = {p.namespace: (p.baseline_mean_24h, p.ratio_vs_baseline) for p in patterns}
    assert "sms-gateway" not in got
    # Judged on the 4 baseline days it existed (400/4 = 100/day), not 6.
    assert got["five-days-old"] == (100.0, 5.0)
    # An old namespace that never logged errors still surfaces when it does.
    assert got["old-and-quiet"] == (0.0, 459.0)


def test_unknown_namespace_age_uses_the_full_window():
    sim = _SimLoki({"errors": {"x": _spread(459, 0, 23)}})
    assert [p.namespace for p in _run(sim, namespace_created={})] == ["x"]
    assert [p.namespace for p in _run(sim, namespace_created=None)] == ["x"]


def test_tripwire_counts_are_true_counts_with_samples():
    """The count is the namespace's real count (a metric query), not the
    number of lines a 15-line streams query returned (#1311/#1312/#1370's
    "15 fatal"; "evicted" was really 216)."""
    sim = _SimLoki(
        {"evicted": {"giks": _spread(216, 1, 20)}, "panic": {"flux-system": {_hours_back(3): 1}}},
        samples={("evicted", "giks"): ["Evicted pod a", "Evicted pod b", "Evicted pod c", "Evicted pod d"],
                 ("panic", "flux-system"): ["panic: runtime error: invalid memory address"]},
    )
    patterns = _run(sim)
    got = {(p.matched_tripwire, p.namespace): p for p in patterns}
    assert got[("evicted", "giks")].count_24h == 216
    assert got[("evicted", "giks")].sample_lines == ["Evicted pod a", "Evicted pod b", "Evicted pod c"]
    panic = got[("panic", "flux-system")]
    assert (panic.count_24h, panic.level, panic.sample_lines) == (1, "panic", ["panic: runtime error: invalid memory address"])
    assert (panic.first_seen_at, panic.last_seen_at) == (_hours_back(4), _hours_back(3))
    # One narrow query per kept pattern: its namespace, `limit` lines.
    assert sorted((q.split("}")[0], limit) for q, _s, _e, limit in sim.stream_calls) == [
        ('{namespace="flux-system"', 3), ('{namespace="giks"', 3)]


def test_samples_are_fetched_only_for_kept_patterns_and_redacted():
    sim = _SimLoki(
        {"connection_refused": {f"ns-{i:02d}": {_hours_back(2): 20 - i} for i in range(15)}},
        samples={("connection_refused", "ns-00"): ["connection refused, password=hunter2"]},
    )
    patterns = _run(sim, max_patterns=10)
    assert len(patterns) == 10 and len(sim.stream_calls) == 10
    assert patterns[0].namespace == "ns-00"                        # highest count first
    assert "hunter2" not in patterns[0].sample_lines[0]


def test_ratio_outliers_keep_their_slots_on_a_noisy_tripwire_day():
    """On a restart day the probe-failure tripwires alone filled all ten
    places (msa2-dev, 2026-10-10), so no ratio outlier reached the LLM."""
    sim = _SimLoki({
        "connection_refused": {f"ns-{i:02d}": {_hours_back(2): 5} for i in range(12)},
        "errors": {"cnpg-system": _spread(900, 0, 23), "monitoring": _spread(600, 0, 23)},
    })
    patterns = _run(sim, max_patterns=10, ratio_slots=3)
    assert [p.chronicity for p in patterns].count("tripwire") == 8
    assert [p.namespace for p in patterns if p.chronicity == "ratio_outlier"] == ["cnpg-system", "monitoring"]


def test_a_quiet_cluster_returns_nothing_and_records_nothing():
    sim = _SimLoki({"errors": {"flux-system": _spread(10, 0, 23)}})
    failures: list[str] = []
    assert _run(sim, failures=failures) == []
    assert failures == []


def test_every_failed_query_is_recorded_and_the_rest_still_run():
    """A failed query is NOT a clean result (2026-09-26: 8 of dev's 10
    tripwires timed out and nothing outside the container log said so)."""
    sim = _SimLoki(
        {"panic": {"giks": {_hours_back(1): 2}}},
        raise_on={"errors", "OOMKilled", "x509_expired", "samples"},
    )
    failures: list[str] = []
    patterns = _run(sim, failures=failures)
    assert [(p.matched_tripwire, p.namespace, p.count_24h, p.sample_lines) for p in patterns] == [
        ("panic", "giks", 2, [])]
    assert failures == [
        "ratio_outlier: TimeoutError",
        "tripwire OOMKilled: TimeoutError",
        "tripwire x509_expired: TimeoutError",
        "tripwire panic samples giks: TimeoutError",
    ]


def test_a_malformed_tripwire_answer_loses_that_tripwire_only():
    """Parsing happens inside each query's try: one bad answer is a recorded
    gap, and every other pattern of the run survives."""
    sim = _SimLoki({"panic": {"giks": {_hours_back(1): 2}}})

    def metric(cluster, query, **kw):
        if sim.kind_of(query) == "fatal":
            return {"data": {"result": [{"metric": {"namespace": "x"}, "values": [["bad"]]}]}}
        return sim.metric(cluster, query, **kw)
    failures: list[str] = []
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns
    patterns = aggregate_log_patterns("dev", loki_query_fn=sim.streams,
                                      loki_metric_query_fn=metric, now=_NOW, failures=failures)
    assert [(p.matched_tripwire, p.namespace) for p in patterns] == [("panic", "giks")]
    assert failures == ["tripwire fatal: ValueError"]


def test_a_loki_400_names_its_reason_in_the_gap():
    """A LogQL parse error is a 400 whose body says why; httpx's exception
    text does not. The gap line carries status and body, on one line."""
    import httpx
    sim = _SimLoki()

    def metric(cluster, query, **kw):
        if sim.kind_of(query) == "panic":
            request = httpx.Request("GET", "https://apiserver/loki")
            response = httpx.Response(400, request=request,
                                      text="parse error at line 1,\n col 52: `syntax error`")
            raise httpx.HTTPStatusError("400 Bad Request", request=request, response=response)
        return sim.metric(cluster, query, **kw)
    failures: list[str] = []
    from cluster_agent.modes.digest_aggregator import aggregate_log_patterns
    aggregate_log_patterns("dev", loki_query_fn=sim.streams, loki_metric_query_fn=metric,
                           now=_NOW, failures=failures)
    assert failures == ["tripwire panic: HTTPStatusError 400: parse error at line 1, col 52: 'syntax error'"]


def test_every_backtick_regex_is_re2():
    """Loki runs RE2; the tests evaluate with Python's `re`, which also
    accepts lookarounds and backreferences. One such edit to a shared
    filter would 400 every query, so it is refused here."""
    from cluster_agent.modes import digest_aggregator as da
    filters = [da._ERROR_LINE_FILTER, da._POSTGRES_ERROR_FILTER] + [f for _l, f in da._TRIPWIRE_PATTERNS]
    regexes = [r for f in filters for r in re.findall(r"`([^`]*)`", f)]
    assert regexes, "no backtick regex found: the guard is checking nothing"
    for rx in regexes:
        assert not re.search(r"\(\?(?:=|!|<=|<!|>)", rx), f"lookaround/atomic group, not RE2: {rx}"
        assert not re.search(r"\\[1-9]", rx), f"backreference, not RE2: {rx}"
        re.compile(rx)


def test_a_namespace_that_is_not_a_dns_label_never_reaches_a_selector():
    sim = _SimLoki({"fatal": {'x"} or {namespace=~".+': {_hours_back(1): 1}}})
    (p,) = _run(sim)
    assert p.sample_lines == [] and sim.stream_calls == []


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


# ── Record-not-event exclusions (kube-infra #1263/#1268/#1286/#1287) ──
#
# The tripwires are LogQL line filters executed by Loki, so "is the
# filter string present" proves little. The tests below EVALUATE each
# tripwire's filter chain against real log-line shapes captured from
# dev/prd Loki on 2026-09-26, with a minimal evaluator for exactly the
# LogQL subset `_TRIPWIRE_PATTERNS` uses. The evaluator raises on
# anything else, so a future filter it cannot read fails loudly instead
# of being silently skipped.

_LOGQL_TOKEN = re.compile(r'\s*(\|=|!=|\|~|!~|or\b|"(?:[^"\\]|\\.)*"|`[^`]*`)')


def _logql_line_filter_matches(expr: str, line: str) -> bool:
    """True if `line` passes every stage of a LogQL line-filter chain.

    Supports only: `|=` / `!=` / `|~` / `!~`, each followed by one or
    more `or`-joined double-quoted or backtick strings. Regex stages use
    unanchored search, like Loki's RE2 MatchString.
    """
    expr = expr.strip()
    tokens: list[str] = []
    pos = 0
    while pos < len(expr):
        m = _LOGQL_TOKEN.match(expr, pos)
        if not m:
            raise ValueError(f"unsupported LogQL at {expr[pos:]!r}")
        tokens.append(m.group(1))
        pos = m.end()

    def literal(idx: int) -> str:
        if idx >= len(tokens):
            raise ValueError(f"filter stage without a value in {expr!r}")
        tok = tokens[idx]
        if tok.startswith("`"):
            return tok[1:-1]
        if tok.startswith('"'):
            return json.loads(tok)
        raise ValueError(f"expected a string literal, got {tok!r}")

    i = 0
    while i < len(tokens):
        op = tokens[i]
        if op not in ("|=", "!=", "|~", "!~"):
            raise ValueError(f"expected a filter operator, got {op!r}")
        values = [literal(i + 1)]
        i += 2
        while i < len(tokens) and tokens[i] == "or":
            values.append(literal(i + 1))
            i += 2
        if op in ("|~", "!~"):
            hit = any(re.search(v, line) for v in values)
        else:
            hit = any(v in line for v in values)
        if hit != (op in ("|=", "|~")):
            return False
    return True


def test_logql_evaluator_rejects_syntax_it_does_not_understand():
    """Guard for the guard: an unparseable stage must raise, not pass."""
    with pytest.raises(ValueError):
        _logql_line_filter_matches('|= "x" | json', "x")
    with pytest.raises(ValueError):
        _logql_line_filter_matches('|= "x" |= ', "x")


def _tripwire(label: str) -> str:
    from cluster_agent.modes.digest_aggregator import _TRIPWIRE_PATTERNS
    return next(q for (lbl, q) in _TRIPWIRE_PATTERNS if lbl == label)


def test_every_tripwire_carries_every_record_exclusion():
    from cluster_agent.modes.digest_aggregator import (
        _AUDIT_EXCLUDE,
        _JSON_DOCUMENT_EXCLUDE,
        _LOKI_QUERY_LOG_EXCLUDE,
        _RUNNER_JOB_MESSAGE_EXCLUDE,
        _TRIPWIRE_PATTERNS,
    )
    assert len(_TRIPWIRE_PATTERNS) == 10
    for label, logql in _TRIPWIRE_PATTERNS:
        for part in (_AUDIT_EXCLUDE, _RUNNER_JOB_MESSAGE_EXCLUDE,
                     _JSON_DOCUMENT_EXCLUDE, _LOKI_QUERY_LOG_EXCLUDE):
            assert part in logql, f"tripwire {label!r} is missing {part!r}"



# ── The ratio outlier's filters, on real lines (2026-10-10) ────────────
# Each line was read from msa2-dev / msa2-prd Loki on 2026-10-10. Where a
# format had no below-error line naming an error in 72h, the line is that
# component's real line with only its level token swapped (marked "swap").

_KYVERNO_ERR = (
    '\x1b[90m2026-10-10T15:45:06Z\x1b[0m \x1b[31mERR\x1b[0m \x1b[1mgithub.com/kyverno/kyverno/pkg/'
    'controllers/report/aggregate/controller.go:844\x1b[0m\x1b[36m >\x1b[0m \x1b[1mfailed to delete '
    'ephemeral report\x1b[0m \x1b[36merror=\x1b[0m\x1b[31m\x1b[1m"ephemeralreports.reports.kyverno.io '
    '\\"f4ff3119\\" not found"\x1b[0m\x1b[0m'
)
_DOPPLER_ERROR = (
    '2026-10-10T10:39:27Z\tERROR\tcontrollers.DopplerSecret\tUnable to set reconcile deployments '
    'condition\t{"dopplersecret": "doppler-operator-system/healthchecks-watchdog", "error": '
    '"client rate limiter Wait returned an error: context canceled"}'
)
_PG_LOG_RECORD = (
    '{"level":"info","ts":"2026-10-10T18:35:39.768483209Z","logger":"postgres","msg":"record",'
    '"logging_pod":"giks-1","record":{"log_time":"2026-10-10 18:35:39.768 UTC","process_id":"38",'
    '"error_severity":"LOG","sql_state_code":"00000","message":"checkpoint complete: wrote 529 '
    'buffers (0.1%)"}}'
)
_CNPG_EXPORTER_DURING_SHUTDOWN = (
    '{"level":"error","ts":"2026-10-10T14:46:12Z","msg":"Error collecting user query","query":'
    '"pg_settings","logging_pod":"w1-1","targetDatabase":"webtracker","error":"failed to connect to '
    '`user=cnpg_metrics_exporter database=webtracker`: /controller/run/.s.PGSQL.5432 (/controller/run): '
    'server error: FATAL: the database system is shutting down (SQLSTATE 57P03)"}'
)


def _pg_record(severity: str, code: str, message: str) -> str:
    return (_PG_LOG_RECORD
            .replace('"error_severity":"LOG","sql_state_code":"00000"',
                     f'"error_severity":"{severity}","sql_state_code":"{code}"')
            .replace("checkpoint complete: wrote 529 buffers (0.1%)", message))


_BELOW_ERROR_LINES = [
    pytest.param(
        '\x1b[90m2026-10-09T12:04:58Z\x1b[0m \x1b[32mINF\x1b[0m \x1b[1mk8s.io/client-go@v0.36.3/rest/'
        'warnings.go:107\x1b[0m\x1b[36m >\x1b[0m \x1b[1mWarning: kyverno.io/v1 ClusterPolicy is '
        'deprecated and will be removed in a future release\x1b[0m',
        id="kyverno-deprecation-warning-1534"),
    pytest.param(_KYVERNO_ERR.replace("\x1b[31mERR", "\x1b[32mINF"), id="kyverno-INF-swap"),
    pytest.param(
        'W1010 18:46:44.683250       1 logging.go:55] [core] [Channel #5263 SubChannel #5264] grpc: '
        'addrConn.createTransport failed to connect to {Addr: "127.0.0.1:2379", }. Err: connection '
        'error: desc = "transport: Error while dialing: dial tcp 127.0.0.1:2379: operation was canceled"',
        id="klog-W-apiserver"),
    pytest.param(
        'I1010 18:46:44.683250       1 logging.go:55] [core] grpc: addrConn.createTransport failed to '
        'connect. Err: connection error: desc = "transport: Error while dialing"',
        id="klog-I-swap"),
    pytest.param(
        'level=info ts=2026-10-10T14:49:41.863869326Z caller=ingester.go:603 component=ingester '
        'msg="WAL segment recovery finished" elapsed=61.934734ms errors=true',
        id="logfmt-info-loki"),
    pytest.param(
        '{"level":"info","ts":"2026-10-10T14:49:45.966251313Z","msg":"DB not available, will retry",'
        '"logger":"instance-manager","logging_pod":"w1-1","err":"failed to connect to `user=postgres '
        'database=postgres`: dial error: dial unix /controller/run/.s.PGSQL.5432: connect: no such file"}',
        id="json-info-cnpg-instance-manager"),
    pytest.param(_DOPPLER_ERROR.replace("\tERROR\t", "\tINFO\t"), id="zap-console-INFO-swap"),
    pytest.param('[WORKER 2026-10-10 12:00:00Z INFO ExecutionContext]   "errorMessages": [],',
                 id="runner-INFO-diagnostics"),
    pytest.param("[18:00:01 INF] Executed DbCommand (3ms) [Parameters=[@errorCutoff='?' (DbType = "
                 "DateTime)], CommandType='Text', CommandTimeout='30']", id="serilog-INF-giks"),
    pytest.param('{"kind":"Event","apiVersion":"audit.k8s.io/v1","level":"Request","verb":"update",'
                 '"responseStatus":{"code":409,"message":"error"}}', id="apiserver-audit-record"),
    pytest.param(_pg_record("ERROR", "42P01", 'relation "x" does not exist'),
                 id="postgres-record-is-counted-by-its-own-query"),
    pytest.param(_CNPG_EXPORTER_DURING_SHUTDOWN, id="cnpg-exporter-during-a-planned-shutdown"),
]

_ERROR_LINES = [
    pytest.param(_KYVERNO_ERR, id="kyverno-ERR"),
    pytest.param('E1010 14:23:24.069551       1 dispatcher.go:214] "Unhandled Error" err="failed calling '
                 'webhook \\"prometheusrulevalidate.monitoring.coreos.com\\": failed to call webhook"',
                 id="klog-E-apiserver"),
    pytest.param('level=error ts=2026-09-26T03:00:00Z caller=flush.go:143 org_id=fake msg="failed to '
                 'flush" err="dial tcp 10.10.10.10:9000: connect: connection refused"', id="logfmt-error"),
    pytest.param(_DOPPLER_ERROR, id="zap-console-ERROR-doppler"),
    pytest.param('{"level":"error","ts":"2026-09-26T08:50:29Z","logger":"reconciler.scan job","msg":'
                 '"Scan job container","status.message":"FATAL\\tFatal error\\trun error"}', id="json-error"),
    pytest.param('[RUNNER 2026-10-10 14:21:03Z ERR  BrokerServer]    at System.Net.Sockets.Socket.'
                 'AwaitableSocketAsyncEventArgs.ThrowException(SocketError error, CancellationToken token)',
                 id="runner-ERR"),
    pytest.param('{"action":"Resource Passed","eventRV":"8424708","kind":"ImageValidatingPolicy","msg":'
                 '"Deployment sms-gateway/app: [evaluation] error; failed to evaluate policy: Get '
                 '\\"https://ghcr.io/v2/\\": context canceled","reason":"PolicyViolation","type":"Warning"}',
                 id="kubernetes-warning-event"),
    pytest.param("panic: runtime error: invalid memory address or nil pointer dereference", id="go-panic"),
    pytest.param("fatal error: concurrent map writes", id="go-fatal-error"),
    # Serilog's abbreviated error level, without the word (GIKS, msa2-dev 2026-10-10).
    pytest.param("[14:20:02 ERR] 10 state change attempt(s) failed due to an exception, "
                 "moving job to the FailedState", id="serilog-ERR-without-the-word"),
    pytest.param("[14:20:02 FTL] Host terminated unexpectedly", id="serilog-FTL"),
]


@pytest.mark.parametrize("line", _BELOW_ERROR_LINES)
def test_error_line_filter_drops_lines_below_error(line):
    from cluster_agent.modes.digest_aggregator import _ERROR_LINE_FILTER
    assert not _logql_line_filter_matches(_ERROR_LINE_FILTER, line), line[:90]


_OLD_RATIO_KEYWORDS = (
    '|= "error" or "Error" or "ERROR" or "warn" or "Warn" or "WARN" or "panic" or "Panic" '
    'or "PANIC" or "fatal" or "Fatal" or "FATAL"'
)


@pytest.mark.parametrize("line", _BELOW_ERROR_LINES)
def test_the_old_ratio_filter_counted_each_below_error_line(line):
    """Positive control for the drop test: until 2026-10-10 every one of these
    counted as an error, so a pass above means the new filter dropped it."""
    assert _logql_line_filter_matches(_OLD_RATIO_KEYWORDS, line), line[:90]


@pytest.mark.parametrize("line", _ERROR_LINES)
def test_error_line_filter_keeps_error_lines(line):
    from cluster_agent.modes.digest_aggregator import _ERROR_LINE_FILTER
    assert _logql_line_filter_matches(_ERROR_LINE_FILTER, line), line[:90]


def test_postgres_filter_judges_a_record_by_its_own_severity():
    """Every Postgres record's wrapper says `"level":"info"` and carries the key
    "error_severity" (#1487: every checkpoint counted as an error)."""
    from cluster_agent.modes.digest_aggregator import _POSTGRES_ERROR_FILTER as f
    assert _logql_line_filter_matches(f, _pg_record("FATAL", "28P01", 'password authentication failed for user "giks"'))
    assert _logql_line_filter_matches(f, _pg_record("ERROR", "42P01", 'relation "x" does not exist'))
    assert not _logql_line_filter_matches(f, _PG_LOG_RECORD)
    assert not _logql_line_filter_matches(f, _pg_record("FATAL", "57P01", "terminating connection due to administrator command"))
    assert not _logql_line_filter_matches(f, _pg_record("FATAL", "57P03", "the database system is starting up"))
    assert not _logql_line_filter_matches(f, _KYVERNO_ERR)


# (tripwire label, log line) pairs that are RECORDS of a keyword and must
# NOT surface. Shapes copied from Loki, 2026-09-26.
_RECORD_LINES = [
    # Trivy scan report (compressLogs=false), trivy-system — #1263/#1268
    pytest.param("panic", (
        '          "Description": "Parsing an invalid SVCB or HTTPS RR can panic when '
        'the size of a parameter value overflows the message buffer.",'
    ), id="trivy-description"),
    pytest.param("panic", (
        '          "Title": "golang.org/x/crypto/ssh: CertChecker callback can panic",'
    ), id="trivy-title"),
    pytest.param("panic", (
        '            "https://securityinfinity.com/research/'
        'buger-jsonparser-negative-slice-panic-dos-2026",\n'
    ), id="trivy-reference-url"),
    pytest.param("fatal", (
        '          "created_by": "RUN /bin/sh -c set -eux; apk add --no-cache gcc make; '
        './configure --enable-fatal-warnings",'
    ), id="trivy-created-by"),
    pytest.param("fatal", (
        '          "Description": "brace-expansion through 5.0.7 hits a fatal '
        'out-of-memory error on crafted input",'
    ), id="trivy-description-fatal"),
    pytest.param("connection_refused", (
        '          "Description": "a peer closing early surfaces as connection refused",'
    ), id="trivy-description-conn-refused"),
    # GitHub Actions runner job-message dump, actions-runner-system — #1287
    pytest.param("OOMKilled", (
        '[WORKER 2026-09-24 23:30:30Z INFO Worker]                       '
        '"v": "## Summary\\n\\nrunner pod was OOMKilled during Task D1b"'
    ), id="runner-job-message-oom"),
    pytest.param("fatal", (
        '[WORKER 2026-09-26 10:14:26Z INFO Worker]                           '
        '"v": "fix(bootstrap): talosctl config new refuses: fatal: path exists"'
    ), id="runner-job-message-fatal"),
    pytest.param("evicted", (
        '[WORKER 2026-09-25 19:23:30Z INFO Worker]   "v": "pods Evicted during drain"'
    ), id="runner-job-message-evicted"),
    # Loki's own query log, monitoring/loki-0 (the digest's ratio query)
    pytest.param("panic", (
        'level=info ts=2026-09-26T03:01:30.665988516Z caller=engine.go:274 '
        'component=querier org_id=fake msg="executing query" '
        'query="sum by (namespace)(count_over_time({namespace=~\\".+\\"} '
        '|= \\"panic\\" or \\"fatal\\"[1d]))" query_hash=3073272766 type=range'
    ), id="loki-query-log-engine"),
    pytest.param("fatal", (
        'level=info ts=2026-09-26T03:02:00.319641969Z caller=metrics.go:237 '
        'component=querier org_id=fake latency=slow query="sum by (namespace)'
        '(count_over_time({namespace=~\\".+\\"} |= \\"fatal\\"[1d]))" '
        'query_hash=3073272766 query_type=metric'
    ), id="loki-query-log-metrics"),
    pytest.param("OOMKilled", (
        'ts=2026-09-26T11:38:37.527561108Z caller=spanlogger.go:152 '
        'middleware=QueryShard.astMapperware org_id=fake user=fake level=warn '
        'msg="failed mapping AST" err="context canceled" '
        'query="{namespace=~\\".+\\"} |= \\"OOMKilled\\""'
    ), id="loki-query-log-spanlogger"),
    # kube-apiserver audit record (pre-existing exclusion, kept)
    pytest.param("OOMKilled", (
        '{"kind":"Event","apiVersion":"audit.k8s.io/v1","level":"Metadata",'
        '"requestURI":"/api/v1/pods","reason":"OOMKilled"}'
    ), id="apiserver-audit-record"),
    # kube-apiserver's post-timeout notice: nothing panicked (#1535, #1536).
    pytest.param("panic", (
        'E1010 14:23:24.069551       1 finisher.go:175] "Unhandled Error" err="FinishRequest: '
        'post-timeout activity - time-elapsed: 1.246373ms, panicked: false, err: <nil>"'
    ), id="apiserver-post-timeout-panicked-false"),
    # Grafana's search-index cache, 216 a day per cluster (2026-10-10).
    pytest.param("evicted", (
        'logger=bleve-backend t=2026-10-10T16:59:35.40208982Z level=info msg="index evicted from '
        'cache" reason=expired key="{Namespace:default Group:folder.grafana.app Resource:folders}"'
    ), id="grafana-index-cache-eviction"),
    # What every planned Postgres restart writes (#1438, #1439, #1447, #1509).
    pytest.param("fatal", _pg_record("FATAL", "57P03", "the database system is starting up"),
                 id="postgres-starting-up-57P03"),
    pytest.param("fatal", _pg_record("FATAL", "57P01", "terminating connection due to administrator command"),
                 id="postgres-admin-shutdown-57P01"),
    pytest.param("fatal", _CNPG_EXPORTER_DURING_SHUTDOWN, id="cnpg-exporter-quoting-57P03"),
]

# (tripwire label, log line) pairs that are real EVENTS and must still
# surface. The first two are the kube-infra #1312 image-scan failure: the
# positive control the triage asked for.
_EVENT_LINES = [
    pytest.param("fatal", (
        '2026-09-26T05:10:24Z\tFATAL\tFatal error\trun error: image scan error: '
        'scan error: unable to initialize a scan service: unable to initialize '
        'container image: unable to find the specified image "quay.io/minio/mc"'
    ), id="trivy-image-scan-FATAL-1312"),
    pytest.param("fatal", (
        '{"level":"error","ts":"2026-09-26T08:50:29Z","logger":"reconciler.scan job",'
        '"msg":"Scan job container","job":"trivy-system/scan-vulnerabilityreport-x",'
        '"container":"uploader","status.message":"FATAL\\tFatal error\\trun error"}'
    ), id="trivy-operator-scan-job-error"),
    pytest.param("panic", "panic: runtime error: invalid memory address or nil pointer dereference",
                 id="go-panic"),
    pytest.param("panic", "\t/usr/local/go/src/runtime/panic.go:262 +0x1d",
                 id="go-stack-frame"),
    pytest.param("fatal", "fatal error: concurrent map writes", id="go-fatal-error"),
    pytest.param("fatal", (
        'level=fatal ts=2026-09-26T03:00:00Z msg="failed to start" '
        'err="open /data/wal: permission denied"'
    ), id="logfmt-fatal"),
    pytest.param("permission_denied", (
        'level=fatal ts=2026-09-26T03:00:00Z msg="failed to start" '
        'err="open /data/wal: permission denied"'
    ), id="logfmt-permission-denied"),
    pytest.param("OOMKilled", "Container runner in pod arc-runner-x was OOMKilled (exit code 137)",
                 id="oomkilled-event"),
    # Loki's REAL errors carry org_id= but no query= field: must still match.
    pytest.param("connection_refused", (
        'level=error ts=2026-09-26T03:00:00Z caller=flush.go:143 org_id=fake '
        'msg="failed to flush" err="dial tcp 10.10.10.10:9000: connect: connection refused"'
    ), id="loki-real-flush-error"),
    # The runner's own errors are ERR, not INFO Worker: must still match.
    pytest.param("connection_refused",
                 "[RUNNER 2026-09-24 23:30:30Z ERR  JobDispatcher] dial tcp: connection refused",
                 id="runner-ERR-line"),
    # A Postgres FATAL that is no planned restart must still match.
    pytest.param("fatal", _pg_record("FATAL", "28P01", 'password authentication failed for user "giks"'),
                 id="postgres-auth-failure-28P01"),
    pytest.param("evicted", (
        '{"count":1,"eventRV":"8424999","kind":"Pod","msg":"The node was low on resource: memory.",'
        '"name":"jobserver-6d9f","reason":"Evicted","type":"Warning"}'
    ), id="kubernetes-evicted-event"),
    pytest.param("panic", (
        'E1010 14:23:24.069551       1 runtime.go:79] Observed a panic: "invalid memory address or '
        'nil pointer dereference" (runtime error: invalid memory address)'
    ), id="klog-observed-panic"),
]


@pytest.mark.parametrize("label,line", _RECORD_LINES)
def test_tripwire_drops_record_lines(label, line):
    assert not _logql_line_filter_matches(_tripwire(label), line), (
        f"tripwire {label!r} still matches a record line: {line[:90]!r}"
    )


@pytest.mark.parametrize("label,line", _EVENT_LINES)
def test_tripwire_keeps_real_event_lines(label, line):
    assert _logql_line_filter_matches(_tripwire(label), line), (
        f"tripwire {label!r} no longer matches a real event: {line[:90]!r}"
    )


@pytest.mark.parametrize("label,line", _RECORD_LINES)
def test_record_lines_do_contain_the_keyword(label, line):
    """Positive control for the drop test: each record line WOULD match
    its tripwire's keyword stage without the exclusions, so a pass above
    means the exclusions removed it, not a keyword typo."""
    keyword_stage = _tripwire(label).split(" != ")[0].split(" !~ ")[0]
    assert _logql_line_filter_matches(keyword_stage, line), (label, line[:90])
