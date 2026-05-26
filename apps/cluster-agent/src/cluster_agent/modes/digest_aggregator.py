"""Aggregate raw Prometheus ALERTS history into per-(alertname, fingerprint)
groups with chronicity classification, AND surface notable log patterns
from Loki history (P3, 2026-05-26).

The daily digest consumes ~10-30 of these summary lines instead of ~200
raw fire events, which keeps the LLM prompt input under ~20K tokens
even on a noisy cluster day. Pre-classification (chronic / transient /
flapping / self_healed) lets the LLM focus its tokens on judgment, not
counting.

Algorithm:
  1. Each Prom timeseries = one (alertname, labels) combination
  2. Walk its `values=[[ts, "1"], ...]` samples in order
  3. Each contiguous run of samples at step-interval = one fire→resolve
     cycle. A gap > step + jitter ends a cycle.
  4. Compute per-group: fire_count, total_firing_seconds, currently_firing
  5. Classify chronicity from thresholds (see _classify_chronicity)
"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import re
from typing import Any

from ..schema import AlertGroup, Chronicity, LogPattern


def aggregate(
    prom_response: dict[str, Any],
    *,
    step_seconds: int = 60,
    now: dt.datetime | None = None,
    chronic_threshold_seconds: int = 3600,
    transient_threshold_seconds: int = 300,
    flapping_threshold_cycles: int = 3,
) -> list[AlertGroup]:
    """Convert a `prometheus_query_range` response into AlertGroups.

    Args:
        prom_response: the response dict from `prometheus_query_range`
                       (i.e. {data: {result: [{metric: {...}, values: [...]}, ...]}})
        step_seconds: the step used when querying Prom (samples that are
                      farther apart than 1.5x step are treated as
                      separate fire cycles)
        now: clock injection for tests; defaults to UTC now
        chronic_threshold_seconds: groups with cumulative firing time
                                   above this are classified `chronic`
        transient_threshold_seconds: single-fire groups with duration
                                     below this are `transient` if not
                                     currently firing
        flapping_threshold_cycles: groups with this many fire→resolve
                                   cycles or more are `flapping`

    Returns:
        List of AlertGroup, one per unique (alertname, fingerprint).
        Empty list if Prom returned no series (no alerts in window).
    """
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    # Prom samples are at most this far apart and still considered one
    # contiguous firing cycle. 1.5x step covers normal scrape jitter.
    cycle_gap_threshold = step_seconds * 1.5

    series = prom_response.get("data", {}).get("result", [])
    groups: list[AlertGroup] = []
    for ts_block in series:
        metric = ts_block.get("metric", {})
        alertname = metric.get("alertname", "unknown")
        # Build a stable fingerprint from the label set (excluding the
        # synthetic alertstate label Prom adds, which we already filter
        # in the query). Mirrors AM's own fingerprint behaviour.
        fingerprint_labels = {
            k: v for k, v in metric.items()
            if k not in ("__name__", "alertstate")
        }
        fingerprint = _fingerprint_of(alertname, fingerprint_labels)

        # Parse samples → list of (datetime, value) tuples
        samples = []
        for ts, _v in ts_block.get("values", []):
            samples.append(dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc))
        if not samples:
            continue

        # Walk samples to find contiguous cycles
        cycles: list[tuple[dt.datetime, dt.datetime]] = []
        cycle_start = samples[0]
        cycle_end = samples[0]
        for s in samples[1:]:
            gap = (s - cycle_end).total_seconds()
            if gap > cycle_gap_threshold:
                # End of a cycle
                cycles.append((cycle_start, cycle_end))
                cycle_start = s
            cycle_end = s
        cycles.append((cycle_start, cycle_end))

        # The group is "currently firing" if the LAST sample is within
        # one step of `now` (Prom's most recent sample is from the last
        # scrape; if alert resolved before this scrape, last sample is
        # older than `step`).
        currently_firing = (now - samples[-1]).total_seconds() <= step_seconds + 30

        total_firing = sum(int((e - s).total_seconds()) for s, e in cycles)
        # Single-sample cycles contribute 0 to the duration above —
        # bump them to step_seconds so a single-scrape fire registers
        # as "fired for ~1 minute" instead of "fired for 0 seconds".
        zero_dur_cycles = sum(1 for s, e in cycles if (e - s).total_seconds() == 0)
        total_firing += zero_dur_cycles * step_seconds

        chronicity = _classify_chronicity(
            fire_count=len(cycles),
            total_firing_seconds=total_firing,
            currently_firing=currently_firing,
            chronic_threshold_seconds=chronic_threshold_seconds,
            transient_threshold_seconds=transient_threshold_seconds,
            flapping_threshold_cycles=flapping_threshold_cycles,
        )

        groups.append(AlertGroup(
            alertname=alertname,
            fingerprint=fingerprint,
            labels=fingerprint_labels,
            fire_count=len(cycles),
            total_firing_seconds=total_firing,
            chronicity=chronicity,
            first_seen_at=samples[0],
            last_seen_at=samples[-1],
            currently_firing=currently_firing,
            sample_annotation=None,   # populated later by enrich_with_annotations
        ))

    # Sort by chronicity (most-actionable first), then by total firing time
    chronicity_order = {"chronic": 0, "flapping": 1, "active": 2,
                        "self_healed": 3, "transient": 4}
    groups.sort(
        key=lambda g: (chronicity_order.get(g.chronicity, 99),
                       -g.total_firing_seconds),
    )
    return groups


def _classify_chronicity(
    *,
    fire_count: int,
    total_firing_seconds: int,
    currently_firing: bool,
    chronic_threshold_seconds: int,
    transient_threshold_seconds: int,
    flapping_threshold_cycles: int,
) -> Chronicity:
    """Decide chronicity from the cumulative pattern.

    Priority order (matters: an alert can be both flapping AND chronic;
    we surface the most actionable label):
      1. chronic if total firing > 1h cumulative
      2. flapping if >= 3 cycles (regardless of duration)
      3. active if currently firing but doesn't meet 1 or 2 yet
      4. self_healed if not currently firing and fire count >= 2
      5. transient if single short fire, resolved
    """
    if total_firing_seconds >= chronic_threshold_seconds:
        return "chronic"
    if fire_count >= flapping_threshold_cycles:
        return "flapping"
    if currently_firing:
        return "active"
    if fire_count >= 2:
        return "self_healed"
    if total_firing_seconds <= transient_threshold_seconds:
        return "transient"
    return "self_healed"


def _fingerprint_of(alertname: str, labels: dict[str, str]) -> str:
    """Build a stable hash to identify an alert across the window."""
    h = hashlib.sha1()
    h.update(alertname.encode())
    h.update(b"\x00")
    h.update(json.dumps(labels, sort_keys=True).encode())
    return h.hexdigest()[:16]


def enrich_with_annotations(
    groups: list[AlertGroup],
    am_active_alerts: list[dict[str, Any]],
) -> None:
    """Mutate `groups` in place — set `sample_annotation` from the AM
    active-alerts response where the fingerprint matches.

    For resolved (self_healed) alerts the annotation is lost since AM's
    active-alerts response only returns currently-firing alerts. We
    accept that — the alertname + label set is usually enough context
    for a resolved transient to be summarized.
    """
    by_fp: dict[str, str] = {}
    for a in am_active_alerts:
        alertname = a.get("labels", {}).get("alertname", "")
        # Recompute fingerprint the same way aggregate() does, so we
        # match on (alertname, labels) regardless of AM's internal fp
        labels = {k: v for k, v in a.get("labels", {}).items() if k != "alertname"}
        fp = _fingerprint_of(alertname, labels)
        annotations = a.get("annotations", {})
        summary = annotations.get("summary") or annotations.get("description") or ""
        by_fp[fp] = summary[:200]
    for g in groups:
        # The fp computed in aggregate() includes the alertname's labels
        # field, while here we strip alertname before fp'ing. Recompute
        # the group's fp the same way for matching.
        match_labels = {k: v for k, v in g.labels.items() if k != "alertname"}
        match_fp = _fingerprint_of(g.alertname, match_labels)
        if match_fp in by_fp:
            g.sample_annotation = by_fp[match_fp]


# ── P3: Log-pattern aggregator (2026-05-26) ───────────────────────────
#
# Surface logs containing errors/warnings that didn't trigger an alert.
# Two signal types per the P3 spec § 10:
#   - ratio_outlier: 24h count ≥ 3× 7d baseline AND ≥ 50 lines absolute
#   - tripwire:      one of these regexes matches (one occurrence enough)

# Tripwire regexes — always surface even if they only fire once. These
# are patterns the operator definitely wants to see; deferring to the
# statistical-outlier path would let a single panic disappear into the
# "single fire — probably noise" bucket. Each entry: (label, regex).
# Label is what appears in `LogPattern.matched_tripwire`.
_TRIPWIRE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("panic",                re.compile(r"(?i)\bpanic\b")),
    ("fatal",                re.compile(r"(?i)\bfatal\b")),
    ("OOMKilled",            re.compile(r"(?i)OOMKilled")),
    ("CrashLoopBackOff",     re.compile(r"(?i)CrashLoopBackOff")),
    ("ImagePullBackOff",     re.compile(r"(?i)ImagePullBackOff")),
    ("certificate_expired",  re.compile(r"(?i)certificate.{0,30}(expired|invalid)")),
    ("x509_expired",         re.compile(r"(?i)x509.{0,30}(expired|signed by unknown)")),
    ("connection_refused",   re.compile(r"(?i)dial tcp.{0,40}connect: connection refused")),
    ("permission_denied",    re.compile(r"(?i)permission denied")),
    ("evicted",              re.compile(r"(?i)\bevicted\b")),
]

# Secret-redaction regexes — applied to every log line before it's
# included in a LogPattern's `sample_lines`. Over-redaction is OK
# (false positives just produce [REDACTED] in the prompt); under-
# redaction could leak credentials to a GH issue body. False
# positives strongly preferred.
_SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|bearer|authorization)\s*[:=]\s*\S+"),
    re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"),                         # Anthropic keys
    re.compile(r"sk-[a-zA-Z0-9_-]{30,}"),                              # Other API key shapes
    re.compile(r"eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"),  # JWTs (3 segments)
    re.compile(r"\b[a-f0-9]{40,}\b"),                                  # long hex (hashes, sigs)
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),                           # GitHub PATs
    re.compile(r"\bghs_[A-Za-z0-9]{20,}\b"),                           # GitHub App install tokens
]


def _redact(line: str, *, max_len: int = 200) -> str:
    """Mask probable secrets + cap length. Idempotent."""
    for pattern in _SECRET_PATTERNS:
        line = pattern.sub("[REDACTED]", line)
    return line[:max_len]


def aggregate_log_patterns(
    cluster: str,
    *,
    loki_query_fn,                       # callable: same shape as tools.loki.loki_query
    loki_metric_query_fn,                # callable: same shape as tools.loki.loki_metric_query_range
    window_hours: int = 24,
    baseline_days: int = 7,
    ratio_threshold: float = 3.0,
    min_count_floor: int = 50,
    max_patterns: int = 10,
    sample_lines_per_pattern: int = 3,
    now: dt.datetime | None = None,
) -> list[LogPattern]:
    """Surface notable log patterns from Loki history.

    Returns up to `max_patterns` `LogPattern` objects, sorted with
    tripwires first (most-actionable), then ratio outliers by ratio
    descending.

    Per § 5 of the P3 spec:
      1. Loki metric query: per-namespace error+warn line counts over
         the trailing `baseline_days` (default 7d) at 1h granularity
      2. For each namespace series: sum last 24h → `count_24h`
      3. Mean of previous (baseline_days - 1) daily totals → baseline
      4. Outliers: ratio ≥ ratio_threshold AND count_24h ≥ min_count_floor
      5. Tripwires: separate Loki streams query for each tripwire
         regex; if ≥1 match in 24h, surface as a LogPattern
      6. For each surfaced pattern, fetch up to N sample lines (or 0
         for tripwires which already carry their match excerpt)

    Tool dependencies are passed in (not imported) so tests can stub
    them cleanly. In production, callers pass tools.loki.loki_query +
    tools.loki.loki_metric_query_range.

    Failures in any individual Loki call don't abort the whole
    aggregator — we log + continue. A partial result is more useful
    than nothing.
    """
    import logging
    log = logging.getLogger(__name__)

    if now is None:
        now = dt.datetime.now(dt.timezone.utc)

    baseline_start = now - dt.timedelta(days=baseline_days)
    # LogQL: `sum by (namespace) (count_over_time({namespace=~"..+"} |~ "(?i)error|warn|fatal|panic" [1h]))`
    # `{namespace=~".+"}` selects every namespace (Loki requires at
    # least one selector). The regex inside `|~` filters to lines that
    # look error-shaped.
    promql = (
        'sum by (namespace) '
        '(count_over_time({namespace=~".+"} |~ "(?i)level=error|level=warn|panic|fatal" '
        f'[{window_hours}h]))'
    )

    # ── Sequential execution with graceful degradation ──────────────
    # Concurrency was tried (PR #57 at max=12, PR #58 at max=3) — both
    # saturated single-binary homelab Loki and produced ZERO patterns.
    # Sequential trades wall time (~8 min per digest at 60s timeout)
    # for sustainable Loki load. For a once-a-day cron at 06:00 EEST
    # this is fine; the digest can take 16 minutes total across both
    # clusters and still produce findings well before the operator
    # starts their day.
    #
    # If a future Loki HA setup raises capacity, we can revisit
    # parallelization. Until then, sequential is the only thing
    # that actually returns signal.
    outliers: list[LogPattern] = []
    tripwires: list[LogPattern] = []

    # Ratio outliers — two sequential queries (recent + baseline)
    try:
        recent_resp = loki_metric_query_fn(
            cluster, promql,
            start=now - dt.timedelta(hours=window_hours),
            end=now,
            step_seconds=window_hours * 3600,
        )
        baseline_resp = loki_metric_query_fn(
            cluster, promql,
            start=baseline_start,
            end=now - dt.timedelta(hours=window_hours),
            step_seconds=24 * 3600,
        )
        outliers = _build_ratio_outliers(
            recent_resp=recent_resp,
            baseline_resp=baseline_resp,
            ratio_threshold=ratio_threshold,
            min_count_floor=min_count_floor,
            now=now,
            window_hours=window_hours,
        )
    except Exception as e:
        log.warning("digest log-pattern outlier query failed: %r", e)

    # Tripwires — sequential queries, per-tripwire graceful degradation
    for label, pattern in _TRIPWIRE_PATTERNS:
        try:
            resp = loki_query_fn(
                cluster,
                f'{{namespace=~".+"}} |~ "{_logql_escape(pattern.pattern)}"',
                start=now - dt.timedelta(hours=window_hours),
                end=now,
                limit=sample_lines_per_pattern * 5,
            )
            tripwires.extend(_extract_tripwire_patterns(
                resp, label=label, now=now,
                window_hours=window_hours,
                sample_lines_per_pattern=sample_lines_per_pattern,
            ))
        except Exception as e:
            log.warning("digest tripwire query for %s failed: %r", label, e)

    # ── Merge: tripwires first, then ratio outliers, cap at max_patterns
    combined = tripwires + sorted(
        outliers,
        key=lambda p: (-(p.ratio_vs_baseline or 0.0), -p.count_24h),
    )
    # De-dupe by (namespace, level, chronicity, matched_tripwire) — a
    # namespace might both ratio-outlier AND tripwire; we keep both
    # entries since they describe different signals.
    return combined[:max_patterns]


def _logql_escape(pat: str) -> str:
    """Escape a Python regex source for embedding in a LogQL string
    literal. LogQL needs backslashes doubled (it's a JSON-ish string)."""
    return pat.replace("\\", "\\\\").replace('"', '\\"')


def _build_ratio_outliers(
    *,
    recent_resp: dict,
    baseline_resp: dict,
    ratio_threshold: float,
    min_count_floor: int,
    now: dt.datetime,
    window_hours: int,
) -> list[LogPattern]:
    """Compute ratio outliers from two Loki metric responses."""
    # Each response: {data: {result: [{metric: {namespace: X}, values: [[ts, "N"], ...]}]}}
    def _sum_series(resp: dict) -> dict[str, float]:
        out: dict[str, float] = {}
        for series in resp.get("data", {}).get("result", []):
            ns = series.get("metric", {}).get("namespace")
            if not ns:
                continue
            total = sum(float(v) for _ts, v in series.get("values", []))
            out[ns] = total
        return out

    def _daily_baseline(resp: dict) -> dict[str, float]:
        """Mean per-24h-bucket count for each namespace."""
        out: dict[str, float] = {}
        for series in resp.get("data", {}).get("result", []):
            ns = series.get("metric", {}).get("namespace")
            if not ns:
                continue
            daily_totals = [float(v) for _ts, v in series.get("values", [])]
            if not daily_totals:
                continue
            out[ns] = sum(daily_totals) / len(daily_totals)
        return out

    recent = _sum_series(recent_resp)
    baseline = _daily_baseline(baseline_resp)

    patterns: list[LogPattern] = []
    for ns, count in recent.items():
        if count < min_count_floor:
            continue
        base = baseline.get(ns, 0.0)
        # Treat zero baseline as 1.0 to avoid divide-by-zero AND to
        # prevent infinitely-large ratios on never-before-seen errors
        # (which we WANT to surface — but a count of 1 still won't
        # pass min_count_floor, so we don't accidentally flag tiny ns).
        denom = max(base, 1.0)
        ratio = count / denom
        if ratio < ratio_threshold:
            continue
        patterns.append(LogPattern(
            namespace=ns,
            level="error",     # we can't easily distinguish error vs warn at this layer
            chronicity="ratio_outlier",
            count_24h=int(count),
            baseline_mean_24h=round(base, 1),
            ratio_vs_baseline=round(ratio, 2),
            matched_tripwire=None,
            sample_lines=[],   # the LLM can ask for samples separately if needed
            first_seen_at=now - dt.timedelta(hours=window_hours),
            last_seen_at=now,
        ))
    return patterns


def _extract_tripwire_patterns(
    resp: dict,
    *,
    label: str,
    now: dt.datetime,
    window_hours: int,
    sample_lines_per_pattern: int,
) -> list[LogPattern]:
    """Build LogPattern entries from a tripwire Loki streams response.

    One LogPattern per (namespace, label) — i.e. if `panic` matched in
    both `flux-system` and `velero`, we emit two patterns. Within each
    namespace, collect up to N sample lines, redacted.
    """
    results = resp.get("data", {}).get("result", [])
    by_ns: dict[str, dict] = {}
    for stream in results:
        ns = stream.get("stream", {}).get("namespace") or stream.get("stream", {}).get("kubernetes_namespace_name")
        if not ns:
            continue
        bucket = by_ns.setdefault(ns, {"count": 0, "lines": [], "first": None, "last": None})
        for ts, line in stream.get("values", []):
            bucket["count"] += 1
            if len(bucket["lines"]) < sample_lines_per_pattern:
                bucket["lines"].append(_redact(line))
            try:
                t = dt.datetime.fromtimestamp(int(ts) / 1e9, tz=dt.timezone.utc)
                if bucket["first"] is None or t < bucket["first"]:
                    bucket["first"] = t
                if bucket["last"] is None or t > bucket["last"]:
                    bucket["last"] = t
            except (ValueError, TypeError):
                pass

    patterns: list[LogPattern] = []
    fallback_first = now - dt.timedelta(hours=window_hours)
    for ns, bucket in by_ns.items():
        if bucket["count"] == 0:
            continue
        patterns.append(LogPattern(
            namespace=ns,
            level="panic" if label == "panic" else ("fatal" if label == "fatal" else "error"),
            chronicity="tripwire",
            count_24h=bucket["count"],
            baseline_mean_24h=None,
            ratio_vs_baseline=None,
            matched_tripwire=label,
            sample_lines=bucket["lines"],
            first_seen_at=bucket["first"] or fallback_first,
            last_seen_at=bucket["last"] or now,
        ))
    return patterns
