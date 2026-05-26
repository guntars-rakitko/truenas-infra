"""Aggregate raw Prometheus ALERTS history into per-(alertname, fingerprint)
groups with chronicity classification.

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
from typing import Any

from ..schema import AlertGroup, Chronicity


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
