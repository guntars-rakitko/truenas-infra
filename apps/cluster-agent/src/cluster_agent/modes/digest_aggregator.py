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
# Surface logs containing errors that didn't trigger an alert.
# Two signal types per the P3 spec § 10:
#   - ratio_outlier: 24h count of ERROR-LEVEL lines ≥ 3× the namespace's
#                    daily baseline AND ≥ 50 lines absolute
#                    (_ERROR_LINE_FILTER / _POSTGRES_ERROR_FILTER below)
#   - tripwire:      one of these regexes matches (one occurrence enough)
#
# Every count comes from an hourly-step range query over hour-aligned
# windows (_hour_windows), summed in Python: see that function for why a
# 24h step counted two days.

# Tripwire regexes — always surface even if they only fire once. These
# are patterns the operator definitely wants to see; deferring to the
# statistical-outlier path would let a single panic disappear into the
# "single fire — probably noise" bucket.
#
# Each entry: (label, logql_line_filter). LogQL filter is sent verbatim
# to Loki — must start with `|=` or `|~`. Per Grafana's query best-
# practices (https://grafana.com/docs/loki/latest/query/bp-query/),
# substring filters (`|=`) are dramatically faster than regex (`|~`),
# especially for rare-match queries that have to scan everything to
# confirm zero matches. We use `or` to OR case variations within a
# single substring match (Loki 2.9+).
#
# History: was regex (`re.compile`) until 2026-05-26 when the daily-
# digest tripwire scan was 5/10 timing out at 60s. Switching to
# substring filters: same matches, ~5-10× faster on Loki per Grafana
# benchmarks. The 5 failing patterns (panic/fatal/cert_expired/x509/
# evicted) all used regex with backtracking quantifiers — exactly the
# anti-pattern Loki docs warn against.
#
# ── Negative filters: drop lines that RECORD a keyword ────────────────
# Every tripwire query carries `_RECORD_EXCLUDE` (built below). Each
# part drops a class of line that contains a crash keyword because it
# is a RECORD of something (an API call, a CVE report, a job payload,
# a query string), not because a component crashed.
#
# Part 1: kube-apiserver audit events (`audit.k8s.io/v1` JSON
# records) contain crash-keyword
# strings (OOMKilled, CrashLoopBackOff, panic, fatal, etc.) inside
# their `requestURI` / `responseObject` fields — those are records of
# API CALLS describing other pods' state, not actual component crashes.
# A Prometheus Operator list/watch reconnection storm (e.g. after a
# control-plane node disruption) routinely generates 15-50 such events
# per minute, all of which trigger every keyword tripwire in lockstep
# (identical 15× counts across panic/fatal/OOMKilled/etc. is the
# smoking-gun signature). Filter at the Loki level so they never reach
# the LLM. Hit on the 2026-05-27 incident (cluster-agent-sandbox
# #39 + #44).
_AUDIT_EXCLUDE = ' != "audit.k8s.io"'

# Part 2: GitHub Actions runner job-message dumps (kube-infra #1287,
# earlier #912). The runner's Worker component logs the whole job
# message at INFO. Each line looks like
# `[WORKER 2026-09-24 23:30:30Z INFO Worker]   "v": "<PR body>"`, so PR
# bodies and commit messages end up in Loki. In this homelab those texts
# routinely discuss panics, OOMKills and refused connections. Measured on
# dev 2026-09-26 (72h): EVERY actions-runner-system hit for panic (7),
# fatal (28), OOMKilled (7), connection refused (7) and evicted (7) was
# one of these lines. The filter matches only the Worker component's INFO
# lines; the runner's ERR/WARN lines still reach the tripwires.
_RUNNER_JOB_MESSAGE_EXCLUDE = ' != "INFO Worker]"'

# Part 3: pretty-printed JSON documents, in practice Trivy scan reports
# (kube-infra #1263/#1268). kube-infra's trivy-operator HelmRelease forces
# `scanJob.compressLogs: "false"` (required: the compressed report
# overflowed etcd, #820). So every scan job prints its full indented
# report JSON to stdout and Alloy ships it to Loki. CVE descriptions say
# "can panic" and "fatal". Measured on prd 2026-09-26 (24h): of 1000
# trivy-system panic hits, 989 were `"Description":`, 2 `"Title":` and 9
# were bare `"https://…panic-dos…",` entries from a `References` array.
# The triage's key-list filter (Description/Title/created_by) would have
# let those 9 through and kept the tripwire firing. So this filter is
# structural: an indented line that starts with a quote is a member or
# array element of a pretty-printed JSON document. It is data, not an
# event. Real failures do not have that shape: trivy's
# `<ts>\tFATAL\tFatal error\trun error: image scan error…` (the #1312
# signal), the operator's compact `{"level":"error",…}`, Go's
# `panic: …` at column 0 and stack frames (`\t/usr/…`, a slash after the
# tab, not a quote) all survive. Live check on dev and prd (24h, all 10
# tripwires, every namespace except trivy-system): the filter removed 0
# lines. A regex is fine here: Loki applies it only to lines that already
# passed the tripwire's `|=` substring filter.
_JSON_DOCUMENT_EXCLUDE = r' !~ `^\s+"`'

# Part 4: Loki's own query log. Loki logs every query it runs with the
# full LogQL text (`caller=metrics.go|engine.go|roundtrip.go|
# spanlogger.go … org_id=fake … query="…"`). Any query that contains a
# keyword therefore plants that keyword in monitoring/loki-0's log. That
# covers this digest's own ratio-outlier query, which lists
# panic/fatal, as well as any operator or agent who searched Loki for
# "OOMKilled". The next digest's tripwire then reports it: on 2026-09-26
# the prd and dev OOMKilled tripwires both returned monitoring/loki
# lines and nothing else. The filter needs `org_id=` before ` query="`,
# so Loki's real errors (flush/ingest `level=error … org_id=fake
# msg=… err="… connection refused"`, which carry no query field) still
# match.
_LOKI_QUERY_LOG_EXCLUDE = r' !~ `org_id=.* query="`'

# Cheap substring filters first, regexes last. Loki evaluates the stages
# left to right, so the regexes only see what is left.
_RECORD_EXCLUDE = (
    _AUDIT_EXCLUDE
    + _RUNNER_JOB_MESSAGE_EXCLUDE
    + _JSON_DOCUMENT_EXCLUDE
    + _LOKI_QUERY_LOG_EXCLUDE
)

# Postgres records that every PLANNED restart writes, under any name:
# SQLSTATE 57P01 (admin_shutdown, "terminating connection due to
# administrator command") and 57P03 (cannot_connect_now, "the database
# system is starting up" / "is shutting down"). On one node each reboot,
# CNPG hibernation and weekly restore drill (its temporary `*-verify`
# Cluster) writes them as FATAL records: kube-infra #1438, #1439, #1447,
# #1509 were nothing else. A crash or a failed recovery has other codes.
# Two forms: a Postgres record's own field, and CNPG's instance manager
# quoting the server's answer (`{"level":"error","msg":"Error collecting
# user query",…,"error":"…FATAL: the database system is shutting down
# (SQLSTATE 57P03)"}`: its metrics exporter connecting during a smart
# shutdown, ~170 lines per restart, 2026-10-10 14:00–15:00Z).
_POSTGRES_RESTART_EXCLUDE = r' !~ `"sql_state_code":"57P0[13]"|\(SQLSTATE 57P0[13]\)`'

_TRIPWIRE_PATTERNS: list[tuple[str, str]] = [
    # kube-apiserver's `"Unhandled Error" err="FinishRequest: post-timeout
    # activity - time-elapsed: …, panicked: false, …"` says a request
    # finished after its timeout, and that nothing panicked. It shows up in
    # every restart window (kube-infra #1535, #1536).
    ("panic",                '|= "panic" or "Panic" or "PANIC"'
                             + ' != "panicked: false"'
                             + _RECORD_EXCLUDE),
    ("fatal",                '|= "fatal" or "FATAL" or "Fatal"' + _RECORD_EXCLUDE
                             + _POSTGRES_RESTART_EXCLUDE),
    ("OOMKilled",            '|= "OOMKilled"' + _RECORD_EXCLUDE),
    ("CrashLoopBackOff",     '|= "CrashLoopBackOff"' + _RECORD_EXCLUDE),
    ("ImagePullBackOff",     '|= "ImagePullBackOff"' + _RECORD_EXCLUDE),
    # certificate/x509 expiry — two AND-chained substrings replace the
    # `.{0,30}` quantifier regex. Loki processes left-to-right, so
    # `certificate` filters down first, then `expired` on the survivors.
    ("certificate_expired",  '|= "certificate" |= "expired"' + _RECORD_EXCLUDE),
    ("x509_expired",         '|= "x509" |= "expired"' + _RECORD_EXCLUDE),
    # Simpler than the original — most "dial tcp ... connect: connection
    # refused" lines contain exactly this substring, no need for the
    # full structured match.
    #
    # ALSO filter Longhorn manager warnings against detached engines.
    # RWO Job volumes (e.g. renovate-cache attached to the Renovate
    # CronJob every 2h) cycle attached→detached with their Job's
    # lifecycle. While detached, Longhorn's manager periodically polls
    # the engine pod for clone-status / purge-status and gets connection
    # refused because the engine pod doesn't exist. Not a real failure
    # signal — expected lifecycle. Hit on 2026-05-28 (cluster-agent-
    # sandbox #43, renovate-cache pvc-0ce5a477).
    ("connection_refused",   '|= "connection refused"'
                             + ' != "Failed to get clone status"'
                             + ' != "Failed to get purge status"'
                             + _RECORD_EXCLUDE),
    ("permission_denied",    '|= "permission denied"' + _RECORD_EXCLUDE),
    # Kubelet emits `Evicted` in Pod event reasons (capital E); some
    # kubelet log paths use lowercase. Cover both.
    # Grafana's search index logs `level=info msg="index evicted from
    # cache"` whenever a cache entry expires: 216 lines a day on each
    # cluster (2026-10-10), every one of the "evicted" hits. It is its
    # cache, not a pod.
    ("evicted",              '|= "Evicted" or "evicted"'
                             + ' != "index evicted from cache"'
                             + _RECORD_EXCLUDE),
]

# ── The ratio outlier's line filters (2026-10-10) ─────────────────────
# The ratio outlier counts ERROR-LEVEL lines. Until 2026-10-10 it counted
# every line holding error/warn/panic/fatal in any case, which counted:
#   - Kyverno 1.19.1's client-go deprecation warnings, ~21,700 a day per
#     cluster at INFO (`INF …/rest/warnings.go:107 > Warning: …`): "warn"
#     matched `warnings.go` (kube-infra #1534, #1537);
#   - every CNPG Postgres record, checkpoints included: each carries the
#     JSON key "error_severity" (#1487);
#   - the apiserver's audit records (the tripwires already excluded them,
#     the ratio query did not): 16,728 of msa2-prd's 49,367 counted lines
#     in the 24h to 2026-10-10 18:00Z. This filter counts 5,238 of them.
# Now the keyword stage is error/panic/fatal only, the tripwires' record
# exclusions apply, a line whose OWN level is below error is dropped, and
# a Postgres record is judged by its own severity (a separate query).

# A line's own level below error (info/warn/debug/trace), in the formats
# this estate's namespaces emit. A line with no level token stays: free
# text that says "error" still counts. Kubernetes event lines are JSON
# with no `level` field, so a Warning event that says "error" counts.
_BELOW_ERROR_LEVEL_EXCLUDE = (
    # zerolog console, coloured (Kyverno): `\x1b[32mINF\x1b[0m`.
    r' !~ `\x1b\[[0-9;]*m(?:INF|WRN|DBG|TRC)\x1b\[0m`'
    # klog: `I1010 12:34:56.789012 …`, `W1010 …`.
    r' !~ `^[IW][0-9]{4} [0-9]{2}:[0-9]{2}:[0-9]{2}`'
    # logfmt: `level=info`, `lvl=warn`.
    r' !~ `(?:^|\s)(?:level|lvl)=(?:info|warn|warning|debug|trace|INFO|WARN|WARNING|DEBUG|TRACE)(?:\s|$)`'
    # JSON (zap, zerolog, controller-runtime, CNPG's instance manager).
    r' !~ `"(?:level|lvl|severity)":"(?:info|warn|warning|debug|trace|INFO|WARN|WARNING|DEBUG|TRACE)"`'
    # zap console (the Doppler operator): `<ts>\tINFO\t…`.
    r' !~ `^\S+\t(?:INFO|WARN|DEBUG)\t`'
    # GitHub Actions runner diagnostics: `[WORKER 2026-10-10 12:00:00Z INFO
    # ExecutionContext]   "errorMessages": [],`. Its ERR lines (`ERR  `)
    # stay. _RUNNER_JOB_MESSAGE_EXCLUDE drops only the Worker's job dump.
    r' !~ `^\[(?:WORKER|RUNNER) [0-9-]+ [0-9:]+Z (?:INFO|WARN|VERB) `'
    # Serilog console (GIKS): `[12:00:00 INF] Executed DbCommand …`.
    r' !~ `^\[[0-9]{2}:[0-9]{2}:[0-9]{2} (?:INF|WRN|DBG|VRB)\]`'
)

# CNPG instance pods write every Postgres record as JSON with
# `"logger":"postgres"`, from the container named `postgres` (read on both
# clusters, 2026-10-10). The record's wrapper is always `"level":"info"`;
# its real severity is `error_severity` (LOG, WARNING, ERROR, FATAL, …).
_POSTGRES_RECORD = '`"logger":"postgres"`'
_ALL_NAMESPACES = '{namespace=~".+"}'
_POSTGRES_CONTAINERS = '{namespace=~".+", container="postgres"}'

_ERROR_LINE_FILTER = (
    '|= "error" or "Error" or "ERROR" or "panic" or "Panic" or "PANIC"'
    ' or "fatal" or "Fatal" or "FATAL"'
    + ' != ' + _POSTGRES_RECORD
    + _RECORD_EXCLUDE
    + _POSTGRES_RESTART_EXCLUDE
    + _BELOW_ERROR_LEVEL_EXCLUDE
)
_POSTGRES_ERROR_FILTER = (
    '|= ' + _POSTGRES_RECORD
    + r' |~ `"error_severity":"(?:ERROR|FATAL|PANIC)"`'
    + _POSTGRES_RESTART_EXCLUDE
)

# A namespace name is a DNS label; anything else never reaches a selector.
_NS_NAME = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")

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


def _hour_windows(
    now: dt.datetime, *, window_hours: int, baseline_days: int,
) -> tuple[dt.datetime, dt.datetime, dt.datetime, dt.datetime]:
    """(recent_start, end, baseline_start, baseline_end) for hourly-step
    range queries of `count_over_time(…[1h])`.

    A point at t counts the hour (t-1h, t], so N points from `start` to
    `end` inclusive count exactly N hours. Both ends sit on the hour: the
    recent window is the `window_hours` full hours before `now`, the
    baseline the `(baseline_days - 1) * 24` hours before that.

    ⚠ Why not one point with a 24h step, as until 2026-10-10: Loki runs
    with `align_queries_with_step: true` (kube-infra loki.yaml), which
    rounds start and end down to a multiple of the step, so a 24h-step
    query over [now-24h, now] evaluated at TWO UTC midnights, and summing
    them counted two days (the older one also sat in the baseline, and
    00:00Z to the run time was never counted). Every ratio was about 2×
    too high: a 3× threshold acted like 1.5×. Hour-aligned windows are
    unchanged by the rounding, and their hourly splits cache across runs.
    """
    end = now.replace(minute=0, second=0, microsecond=0)
    recent_start = end - dt.timedelta(hours=window_hours - 1)
    baseline_end = end - dt.timedelta(hours=window_hours)
    baseline_start = baseline_end - dt.timedelta(hours=(baseline_days - 1) * 24 - 1)
    return recent_start, end, baseline_start, baseline_end


def _count_by_namespace(selector: str, line_filter: str) -> str:
    """Lines per namespace per hour, for the hour-aligned range queries."""
    return f"sum by (namespace) (count_over_time({selector} {line_filter} [1h]))"


def _sum_series(resp: dict) -> dict[str, float]:
    """Total of every point, per namespace."""
    out: dict[str, float] = {}
    for series in resp.get("data", {}).get("result", []):
        ns = series.get("metric", {}).get("namespace")
        if not ns:
            continue
        out[ns] = out.get(ns, 0.0) + sum(float(v) for _ts, v in series.get("values", []))
    return out


def _seen_span(resp: dict) -> dict[str, tuple[dt.datetime, dt.datetime]]:
    """(first, last) hour with a matching line, per namespace. Hour
    precision: the point at t counts (t-1h, t]."""
    out: dict[str, tuple[dt.datetime, dt.datetime]] = {}
    for series in resp.get("data", {}).get("result", []):
        ns = series.get("metric", {}).get("namespace")
        hours = sorted(float(ts) for ts, v in series.get("values", []) if float(v) > 0)
        if ns and hours:
            out[ns] = (
                dt.datetime.fromtimestamp(hours[0], tz=dt.timezone.utc) - dt.timedelta(hours=1),
                dt.datetime.fromtimestamp(hours[-1], tz=dt.timezone.utc),
            )
    return out


def aggregate_log_patterns(
    cluster: str,
    *,
    loki_query_fn,                       # callable: same shape as tools.loki.loki_query
    loki_metric_query_fn,                # callable: same shape as tools.loki.loki_metric_query_range
    window_hours: int = 24,
    baseline_days: int = 7,
    ratio_threshold: float = 3.0,
    min_count_floor: int = 50,
    min_history_days: float = 3.0,
    namespace_created: dict[str, dt.datetime] | None = None,
    max_patterns: int = 10,
    ratio_slots: int = 3,
    sample_lines_per_pattern: int = 3,
    now: dt.datetime | None = None,
    failures: list[str] | None = None,
) -> list[LogPattern]:
    """Surface notable log patterns from Loki history.

    Returns up to `max_patterns` `LogPattern` objects: tripwires first
    (in `_TRIPWIRE_PATTERNS` order, then by count), then ratio outliers
    by ratio descending, with up to `ratio_slots` places kept for them.

      1. Ratio outliers: error-level lines per namespace (_ERROR_LINE_FILTER,
         plus Postgres records by their own severity, _POSTGRES_ERROR_FILTER)
         over the last `window_hours` against the daily mean of the
         `baseline_days - 1` days before it. Surfaced when the count is
         ≥ `min_count_floor` and ≥ `ratio_threshold` × that mean.
      2. Tripwires: each `_TRIPWIRE_PATTERNS` filter's TRUE count per
         namespace (a metric query), surfaced on any occurrence.
      3. Up to `sample_lines_per_pattern` redacted lines for each tripwire
         pattern that made the cut, one query per namespace.

    A namespace with a known creation time (`namespace_created`, from
    kube-state-metrics) is judged on the baseline days it existed, and
    is no ratio outlier until it has `min_history_days` of them: on its
    first day everything it logs is "N× a baseline of 0" (kube-infra
    #1486, #1401/#1402). Unknown namespaces are judged on the full window.

    Tool dependencies are passed in (not imported) so tests can stub
    them cleanly. In production, callers pass tools.loki.loki_query +
    tools.loki.loki_metric_query_range.

    Failures in any individual Loki call don't abort the whole
    aggregator — we log + continue. A partial result is more useful
    than nothing.

    ⚠ But a failed query is NOT a clean result: a tripwire that timed
    out was never checked, and "no pattern" for it means nothing. When
    the caller passes a `failures` list, one short line per failed
    query (`"tripwire panic: ReadTimeout"`) is appended to it so the
    daily summary can say which signals went unchecked. Measured
    2026-09-26: 8 of dev's 10 tripwire queries hit the 60s timeout
    (dev Loki's trivy-system ingest), and nothing outside the container
    log said so.
    """
    import logging
    log = logging.getLogger(__name__)

    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    recent_start, end, baseline_start, baseline_end = _hour_windows(
        now, window_hours=window_hours, baseline_days=baseline_days,
    )
    window_start = end - dt.timedelta(hours=window_hours)

    def _failed(what: str, e: Exception) -> None:
        log.warning("digest log-pattern query %s failed: %r", what, e)
        if failures is not None:
            failures.append(f"{what}: {type(e).__name__}")

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
    tripwires: list[tuple[LogPattern, str]] = []   # (pattern, its line filter)

    # Ratio outliers: four sequential queries (recent + baseline, each for
    # the non-Postgres lines and the Postgres records), summed per namespace.
    try:
        recent: dict[str, float] = {}
        baseline: dict[str, float] = {}
        for selector, line_filter in ((_ALL_NAMESPACES, _ERROR_LINE_FILTER),
                                      (_POSTGRES_CONTAINERS, _POSTGRES_ERROR_FILTER)):
            query = _count_by_namespace(selector, line_filter)
            for ns, n in _sum_series(loki_metric_query_fn(
                    cluster, query, start=recent_start, end=end, step_seconds=3600)).items():
                recent[ns] = recent.get(ns, 0.0) + n
            for ns, n in _sum_series(loki_metric_query_fn(
                    cluster, query, start=baseline_start, end=baseline_end, step_seconds=3600)).items():
                baseline[ns] = baseline.get(ns, 0.0) + n
        outliers = _build_ratio_outliers(
            recent=recent,
            baseline_totals=baseline,
            baseline_days=baseline_days - 1,
            baseline_until=window_start,
            namespace_created=namespace_created,
            min_history_days=min_history_days,
            ratio_threshold=ratio_threshold,
            min_count_floor=min_count_floor,
            window_start=window_start,
            now=now,
        )
    except Exception as e:
        _failed("ratio_outlier", e)

    # Tripwires: one metric query each, for the true count per namespace.
    # Until 2026-10-10 a streams query with `limit=15` across ALL
    # namespaces counted the lines it returned, so every count was capped
    # at 15 (the "15 fatal" findings were the cap; "evicted" was really
    # 216) and a noisy namespace could push another's real hit out.
    for label, logql_filter in _TRIPWIRE_PATTERNS:
        try:
            resp = loki_metric_query_fn(
                cluster, _count_by_namespace(_ALL_NAMESPACES, logql_filter),
                start=recent_start, end=end, step_seconds=3600,
            )
        except Exception as e:
            _failed(f"tripwire {label}", e)
            continue
        spans = _seen_span(resp)
        for ns, count in sorted(_sum_series(resp).items(), key=lambda kv: (-kv[1], kv[0])):
            if count <= 0:
                continue
            first, last = spans.get(ns, (window_start, now))
            tripwires.append((LogPattern(
                namespace=ns,
                level="panic" if label == "panic" else ("fatal" if label == "fatal" else "error"),
                chronicity="tripwire",
                count_24h=int(count),
                baseline_mean_24h=None,
                ratio_vs_baseline=None,
                matched_tripwire=label,
                sample_lines=[],
                first_seen_at=first,
                last_seen_at=min(last, now),
            ), logql_filter))

    # ── Merge: tripwires first, then ratio outliers, cap at max_patterns.
    # Up to `ratio_slots` places are kept for ratio outliers: on a restart
    # day the readiness-probe `connection_refused` hits alone filled all
    # ten (msa2-dev, 2026-10-10), and no outlier could reach the LLM.
    outliers = sorted(outliers, key=lambda p: (-(p.ratio_vs_baseline or 0.0), -p.count_24h))
    trip_kept = [p for p, _f in tripwires][:max_patterns - min(len(outliers), ratio_slots)]
    kept = trip_kept + outliers[:max_patterns - len(trip_kept)]

    # Sample lines, only for the tripwire patterns that made the cut: one
    # narrow query each (one namespace, `limit` lines, newest first).
    line_filter_of = {id(p): f for p, f in tripwires}
    for p in kept:
        line_filter = line_filter_of.get(id(p))
        if line_filter is None or not _NS_NAME.fullmatch(p.namespace):
            continue
        try:
            resp = loki_query_fn(
                cluster, f'{{namespace="{p.namespace}"}} {line_filter}',
                start=window_start, end=now, limit=sample_lines_per_pattern,
            )
        except Exception as e:
            _failed(f"tripwire {p.matched_tripwire} samples {p.namespace}", e)
            continue
        lines = [line for stream in resp.get("data", {}).get("result", [])
                 for _ts, line in stream.get("values", [])]
        p.sample_lines = [_redact(line) for line in lines[:sample_lines_per_pattern]]
    return kept


def _build_ratio_outliers(
    *,
    recent: dict[str, float],
    baseline_totals: dict[str, float],
    baseline_days: int,
    baseline_until: dt.datetime,
    namespace_created: dict[str, dt.datetime] | None,
    min_history_days: float,
    ratio_threshold: float,
    min_count_floor: int,
    window_start: dt.datetime,
    now: dt.datetime,
) -> list[LogPattern]:
    """Ratio outliers from per-namespace totals: the recent window's count
    against the daily mean of the baseline days the namespace existed."""
    created = namespace_created or {}
    patterns: list[LogPattern] = []
    for ns, count in recent.items():
        if count < min_count_floor:
            continue
        days = float(baseline_days)
        born = created.get(ns)
        if born is not None:
            days = min(days, (baseline_until - born).total_seconds() / 86400)
        if days < min_history_days:
            continue   # too new to have a baseline
        base = baseline_totals.get(ns, 0.0) / days
        # A zero baseline counts as 1.0: no divide-by-zero, and a quiet
        # namespace that suddenly logs errors still surfaces (with the
        # floor above keeping "1 → 5" out).
        ratio = count / max(base, 1.0)
        if ratio < ratio_threshold:
            continue
        patterns.append(LogPattern(
            namespace=ns,
            level="error",
            chronicity="ratio_outlier",
            count_24h=int(count),
            baseline_mean_24h=round(base, 1),
            ratio_vs_baseline=round(ratio, 2),
            matched_tripwire=None,
            sample_lines=[],   # the LLM can ask for samples separately if needed
            first_seen_at=window_start,
            last_seen_at=now,
        ))
    return patterns
