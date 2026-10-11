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
from typing import Any, Sequence

from ..schema import AlertGroup, Chronicity, KnownEvent, LogPattern
from .known_events import RESTART_KINDS, noise_window, touches


# The chronicity thresholds. The digest-summary's section headers are built
# from these (summary_issue._bucket_label), so the two cannot drift apart:
# until 2026-10-10 those headers said "> 6h" and "> 3 cycles" while the code
# classified at 1h and 3.
CHRONIC_THRESHOLD_SECONDS = 3600
TRANSIENT_THRESHOLD_SECONDS = 300
FLAPPING_THRESHOLD_CYCLES = 3


def aggregate(
    prom_response: dict[str, Any],
    *,
    step_seconds: int = 60,
    now: dt.datetime | None = None,
    chronic_threshold_seconds: int = CHRONIC_THRESHOLD_SECONDS,
    transient_threshold_seconds: int = TRANSIENT_THRESHOLD_SECONDS,
    flapping_threshold_cycles: int = FLAPPING_THRESHOLD_CYCLES,
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
      1. chronic if total firing >= 1h cumulative
      2. flapping if >= 3 cycles (regardless of duration)
      3. active if currently firing but doesn't meet 1 or 2 yet
      4. self_healed if not currently firing and fire count >= 2
      5. transient if one fire of <= 5 min, resolved
      6. self_healed otherwise (one fire longer than 5 min, resolved)
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
# Every count comes from a range query over hour-aligned windows
# (_hour_windows), summed in Python: see that function for why a 24h step
# counted two days. The last `window_hours` are counted in 15-minute slots,
# the baseline days in hours.

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

# Part 5: a .NET client's PostgresException dump (GIKS, kube-infra #1574,
# #1576). Npgsql prints the exception's properties one per line, indented:
#   `      Npgsql.PostgresException (0x80004005): 57P03: the database system is shutting down`
#   `    Severity: FATAL`   (`          Severity: FATAL` for an inner exception)
# The Severity line restates what Postgres itself logged, as a record the
# Postgres filters judge by its own SQLSTATE (_POSTGRES_RESTART_EXCLUDE,
# _POSTGRES_ERROR_FILTER), and it carries no SQLSTATE: a planned restart's
# 57P03 and an auth failure's 28P01 look the same on it. On 2026-10-10 the
# reboots made GIKS's jobserver print 10 (prd) and 6 (dev), all 57P03, and
# each run filed a `fatal` finding; sms-gateway printed 2 during the CNPG
# upgrade (part of #1577). A real database failure still trips `fatal`
# through the server's own record.
_NPGSQL_SEVERITY_LINE_EXCLUDE = r' !~ `^\s+Severity: (?:PANIC|FATAL|ERROR)\s*$`'

# Cheap substring filters first, regexes last. Loki evaluates the stages
# left to right, so the regexes only see what is left.
_RECORD_EXCLUDE = (
    _AUDIT_EXCLUDE
    + _RUNNER_JOB_MESSAGE_EXCLUDE
    + _JSON_DOCUMENT_EXCLUDE
    + _LOKI_QUERY_LOG_EXCLUDE
    + _NPGSQL_SEVERITY_LINE_EXCLUDE
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
    # full structured match. (Two Longhorn excludes, its manager polling a
    # detached engine, went with Longhorn: the MS-A2 clusters never ran it.)
    ("connection_refused",   '|= "connection refused"' + _RECORD_EXCLUDE),
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
#
# ⚠ The keyword stage is a GATE: a line counts only if it says error,
# panic or fatal (any of three cases), or carries Serilog's abbreviated
# error level (`[12:00:00 ERR]`, `FTL]`: GIKS logs Hangfire and Polly
# failures that way without the word, 38 lines a day on msa2-dev and 12
# on msa2-prd, 2026-10-10). Two abbreviated error levels still do not
# count without the word: klog's `E1010 …` (5 a day per cluster, all
# Talos's kubelet "The manifest file is empty, ignoring.") and zerolog's
# coloured `ERR` (none in that day).

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
    ' or "fatal" or "Fatal" or "FATAL" or " ERR]" or " FTL]"'
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


_HOUR = dt.timedelta(hours=1)
# The recent window's step. Its counts are matched against known events,
# whose noise is under an hour long (known_events.noise_window): with hour
# points, "45 min after" an event at 14:59 reached 15:59, and before it,
# 14:00. Counts per slot keep that within 15 minutes.
_SLOT = dt.timedelta(minutes=15)


def _hour_windows(
    now: dt.datetime, *, window_hours: int, baseline_days: int,
) -> tuple[dt.datetime, dt.datetime, dt.datetime, dt.datetime]:
    """(recent_start, end, baseline_start, baseline_end) for the range
    queries of `count_over_time(…[step])`: the recent window in `_SLOT`
    steps, the baseline in hours.

    A point at t counts the step before it, (t-step, t], so N points from
    `start` to `end` inclusive count exactly N steps. Both ends sit on the
    hour: the recent window is the `window_hours` full hours before `now`,
    the baseline the `(baseline_days - 1) * 24` hours before that.

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
    recent_start = end - dt.timedelta(hours=window_hours) + _SLOT
    baseline_end = end - dt.timedelta(hours=window_hours)
    baseline_start = baseline_end - dt.timedelta(hours=(baseline_days - 1) * 24 - 1)
    return recent_start, end, baseline_start, baseline_end


def _count_by_namespace(selector: str, line_filter: str, step: dt.timedelta = _HOUR) -> str:
    """Lines per namespace per step, for the hour-aligned range queries."""
    minutes = int(step.total_seconds() // 60)
    span = f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"
    return f"sum by (namespace) (count_over_time({selector} {line_filter} [{span}]))"


def _utc(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)


def _sum_series(resp: dict) -> dict[str, float]:
    """Total of every point, per namespace."""
    out: dict[str, float] = {}
    for series in resp.get("data", {}).get("result", []):
        ns = series.get("metric", {}).get("namespace")
        if not ns:
            continue
        out[ns] = out.get(ns, 0.0) + sum(float(v) for _ts, v in series.get("values", []))
    return out


def _points(resp: dict) -> dict[str, dict[float, float]]:
    """Per namespace, the count at each point (t → lines in (t-step, t])."""
    out: dict[str, dict[float, float]] = {}
    for series in resp.get("data", {}).get("result", []):
        ns = series.get("metric", {}).get("namespace")
        if not ns:
            continue
        points = out.setdefault(ns, {})
        for ts, v in series.get("values", []):
            points[float(ts)] = points.get(float(ts), 0.0) + float(v)
    return out


def _shape(points: dict[float, float], step: dt.timedelta
           ) -> tuple[dt.datetime, dt.datetime, dt.datetime, float] | None:
    """(first step's start, last step's end, peak hour, peak share) of one
    namespace's counts per step, or None when it has no line. The peak is
    an hour, the earliest of equal ones: a reboot's noise spans its slots."""
    lines = sorted(t for t, v in points.items() if v > 0)
    total = sum(points.values())
    if not lines or total <= 0:
        return None
    by_hour: dict[dt.datetime, float] = {}
    for t in lines:
        hour = (_utc(t) - step).replace(minute=0, second=0, microsecond=0)
        by_hour[hour] = by_hour.get(hour, 0.0) + points[t]
    peak = min(by_hour, key=lambda hour: (-by_hour[hour], hour))
    return _utc(lines[0]) - step, _utc(lines[-1]), peak, round(by_hour[peak] / total, 2)


# Tripwires no planned change explains: a restart does not expire a
# certificate, OOM-kill a pod or lose an image.
_NEVER_EVENT_NOISE = frozenset({"OOMKilled", "ImagePullBackOff",
                                "certificate_expired", "x509_expired"})
# A process dying. After a restart of the same code that is noise; after
# an upgrade, a rollout or a drill the new code or config may be at fault,
# so only a restart (known_events.RESTART_KINDS) explains these.
_RESTART_ONLY_NOISE = frozenset({"panic", "fatal", "CrashLoopBackOff"})


def _mark_event_noise(
    pattern: LogPattern,
    points: dict[float, float],
    events: Sequence[KnownEvent],
    *,
    window_start: dt.datetime,
    window_hours: int,
    step: dt.timedelta,
    ratio_threshold: float,
    min_count_floor: int,
) -> None:
    """Mark a log pattern that is a known event's noise (2026-10-11).

    A step of the window is an event step of the pattern when a known
    event that touches its namespace (known_events.touches) makes noise in
    it (known_events.noise_window: a restart from 15 min before it,
    anything else from its start, until 45 min after). The pattern is that
    noise, `explained_by` the events of the event steps its lines fell in,
    when
      - a tripwire: every line fell in an event step. Never one of
        _NEVER_EVENT_NOISE, and one of _RESTART_ONLY_NOISE only in a
        restart's steps;
      - a ratio outlier: most of its lines fell in event steps, and the
        rest would not make it one (fewer than `min_count_floor`, or under
        `ratio_threshold` × its baseline for those steps). "Most" keeps a
        borderline outlier whose lines are spread over the day from being
        put on an event that added a few (msa2-dev's trivy-system, 3.4×
        with 22% in the reboot hour: 2.9× without it).
    Any other ratio outlier with lines in event steps gets
    `ratio_outside_events` and `count_outside_events`, its ratio and count
    over the other steps, which is what the LLM judges it on.

    Measured on 2026-10-11's digests: five of the six ratio outliers had
    63-98% of their lines in the hour of a reboot, three of them filed as
    findings (kube-infra #1575, #1577, #1578), and the tripwire hits
    outside it fell in the hours of rollouts and Helm upgrades in their
    namespaces. #1577 read sms-gateway's connection_refused hits in the
    hours of its 07:31 and 14:41 rollouts as "failing since 07:00".
    """
    label = pattern.matched_tripwire
    if label in _NEVER_EVENT_NOISE:
        return
    windows = [(e.signal_id, *noise_window(e)) for e in events
               if e.signal_id and touches(e, pattern.namespace)
               and (label not in _RESTART_ONLY_NOISE or e.kind in RESTART_KINDS)]
    steps = int(dt.timedelta(hours=window_hours) / step)
    noisy: dict[dt.datetime, list[str]] = {}
    for k in range(steps):
        begins = window_start + k * step
        ids = [sid for sid, first, last in windows if first < begins + step and last > begins]
        if ids:
            noisy[begins] = ids
    counts = {_utc(t) - step: n for t, n in points.items() if n > 0}
    inside = [begins for begins in counts if begins in noisy]
    if not inside:
        return
    order = {e.signal_id: i for i, e in enumerate(events)}
    explained_by = sorted({sid for begins in inside for sid in noisy[begins]},
                          key=order.__getitem__)
    outside = sum(n for begins, n in counts.items() if begins not in noisy)
    if pattern.chronicity == "tripwire":
        if outside == 0:
            pattern.explained_by = explained_by
        return
    expected = (pattern.baseline_mean_24h or 0.0) * (steps - len(noisy)) / steps
    ratio = outside / max(expected, 1.0)
    mostly_inside = outside <= sum(counts.values()) / 2
    if mostly_inside and (outside < min_count_floor or ratio < ratio_threshold):
        pattern.explained_by = explained_by
    else:
        pattern.ratio_outside_events = round(ratio, 2)
        pattern.count_outside_events = int(outside)


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
    known_events: Sequence[KnownEvent] = (),
) -> list[LogPattern]:
    """Surface notable log patterns from Loki history.

    Returns up to `max_patterns` `LogPattern` objects: tripwires first
    (in `_TRIPWIRE_PATTERNS` order, then by count), then ratio outliers
    by ratio descending, with up to `ratio_slots` places kept for them.
    A pattern that is the noise of one of the `known_events` (numbered
    already: _mark_event_noise) only gets the places left after those.

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

    def _mark(p: LogPattern, points: dict[float, float]) -> None:
        """Mark p if it is a known event's noise. A failure leaves it
        unmarked and recorded: the pattern itself is never lost."""
        try:
            _mark_event_noise(p, points, known_events, window_start=window_start,
                              window_hours=window_hours, step=_SLOT,
                              ratio_threshold=ratio_threshold, min_count_floor=min_count_floor)
        except Exception as e:
            p.explained_by, p.ratio_outside_events, p.count_outside_events = [], None, None
            _failed(f"event noise {p.matched_tripwire or 'ratio_outlier'} {p.namespace}", e)

    def _failed(what: str, e: Exception) -> None:
        log.warning("digest log-pattern query %s failed: %r", what, e)
        if failures is not None:
            detail = type(e).__name__
            # A Loki 400 (a LogQL parse error) says why in its body, which
            # httpx's exception text leaves out. One line, no backticks: it
            # lands in a markdown list in the digest-summary.
            response = getattr(e, "response", None)
            status = getattr(response, "status_code", None)
            if status is not None:
                body = " ".join((getattr(response, "text", "") or "").split())[:120]
                detail += f" {status}" + (f": {body}" if body else "")
            failures.append(f"{what}: {detail}".replace("`", "'"))

    # ── Sequential execution with graceful degradation ──────────────
    # Concurrency was tried (PR #57 at max=12, PR #58 at max=3) — both
    # saturated single-binary homelab Loki and produced ZERO patterns.
    # Sequential trades wall time for sustainable Loki load: up to 24
    # queries per cluster (4 ratio, 10 tripwire, up to 10 sample), each
    # capped at 60 s, so a slow Loki could take ~24 min per cluster.
    # Measured on a healthy one, 2026-10-10: msa2-prd's 24 in 31.5 s.
    # For a once-a-day cron at 06:00 EEST that is fine.
    #
    # If a future Loki HA setup raises capacity, we can revisit
    # parallelization. Until then, sequential is the only thing
    # that actually returns signal.
    outliers: list[LogPattern] = []
    tripwires: list[tuple[LogPattern, str]] = []   # (pattern, its line filter)

    # Ratio outliers: four sequential queries (recent + baseline, each for
    # the non-Postgres lines and the Postgres records), summed per namespace.
    try:
        recent: dict[str, dict[float, float]] = {}
        baseline: dict[str, float] = {}
        for selector, line_filter in ((_ALL_NAMESPACES, _ERROR_LINE_FILTER),
                                      (_POSTGRES_CONTAINERS, _POSTGRES_ERROR_FILTER)):
            for ns, points in _points(loki_metric_query_fn(
                    cluster, _count_by_namespace(selector, line_filter, _SLOT),
                    start=recent_start, end=end, step_seconds=int(_SLOT.total_seconds()))).items():
                merged = recent.setdefault(ns, {})
                for t, n in points.items():
                    merged[t] = merged.get(t, 0.0) + n
            for ns, n in _sum_series(loki_metric_query_fn(
                    cluster, _count_by_namespace(selector, line_filter),
                    start=baseline_start, end=baseline_end, step_seconds=3600)).items():
                baseline[ns] = baseline.get(ns, 0.0) + n
        outliers = _build_ratio_outliers(
            recent=recent,
            step=_SLOT,
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
        for p in outliers:
            _mark(p, recent.get(p.namespace, {}))
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
                cluster, _count_by_namespace(_ALL_NAMESPACES, logql_filter, _SLOT),
                start=recent_start, end=end, step_seconds=int(_SLOT.total_seconds()),
            )
            # Parsed inside the try: a malformed response loses this
            # tripwire, recorded, not every pattern of the run.
            per_ns = _points(resp)
            found: list[tuple[LogPattern, str]] = []
            for ns, count in sorted(_sum_series(resp).items(), key=lambda kv: (-kv[1], kv[0])):
                if count <= 0:
                    continue
                shape = _shape(per_ns.get(ns, {}), _SLOT)
                first, last, peak, share = shape or (window_start, now, None, None)
                pattern = LogPattern(
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
                    peak_hour=peak,
                    peak_share=share,
                )
                _mark(pattern, per_ns.get(ns, {}))
                found.append((pattern, logql_filter))
        except Exception as e:
            _failed(f"tripwire {label}", e)
            continue
        tripwires.extend(found)

    # ── Merge: tripwires first, then ratio outliers, cap at max_patterns.
    # Up to `ratio_slots` places are kept for ratio outliers: on a restart
    # day the readiness-probe `connection_refused` hits alone filled all
    # ten (msa2-dev, 2026-10-10), and no outlier could reach the LLM. A
    # known event's noise gets only the places left over. An outlier is
    # ranked by its ratio outside the event hours when it has one.
    outliers = sorted(outliers, key=lambda p: (
        -(p.ratio_outside_events if p.ratio_outside_events is not None
          else p.ratio_vs_baseline or 0.0), -p.count_24h))
    patterns = [p for p, _f in tripwires] + outliers
    real_trips = [p for p, _f in tripwires if not p.explained_by]
    real_outliers = [p for p in outliers if not p.explained_by]
    trip_kept = real_trips[:max(0, max_patterns - min(len(real_outliers), ratio_slots))]
    kept = trip_kept + real_outliers[:max(0, max_patterns - len(trip_kept))]
    kept += [p for p in patterns if p.explained_by][:max(0, max_patterns - len(kept))]

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
            lines = [line for stream in resp.get("data", {}).get("result", [])
                     for _ts, line in stream.get("values", [])]
            p.sample_lines = [_redact(line) for line in lines[:sample_lines_per_pattern]]
        except Exception as e:
            _failed(f"tripwire {p.matched_tripwire} samples {p.namespace}", e)
    return kept


def _build_ratio_outliers(
    *,
    recent: dict[str, dict[float, float]],
    step: dt.timedelta,
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
    """Ratio outliers from per-namespace counts: the recent window's counts
    per `step`, summed, against the daily mean of the baseline days the
    namespace existed."""
    created = namespace_created or {}
    patterns: list[LogPattern] = []
    for ns, points in recent.items():
        count = sum(points.values())
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
        shape = _shape(points, step)
        first, last, peak, share = shape or (window_start, now, None, None)
        patterns.append(LogPattern(
            namespace=ns,
            level="error",
            chronicity="ratio_outlier",
            count_24h=int(count),
            baseline_mean_24h=round(base, 1),
            ratio_vs_baseline=round(ratio, 2),
            matched_tripwire=None,
            sample_lines=[],   # the LLM can ask for samples separately if needed
            first_seen_at=first,
            last_seen_at=min(last, now),
            peak_hour=peak,
            peak_share=share,
        ))
    return patterns
