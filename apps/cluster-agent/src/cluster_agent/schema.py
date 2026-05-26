"""Finding schema — the persistence boundary between LLM output and storage.

See spec § 4.4 for the full design. The schema is strict (Pydantic
validates at construction time) so a malformed LLM response throws at
the parse step rather than silently corrupting downstream:
  - mode/cluster/severity: enumerated literals only
  - confidence: clamped to [0.0, 1.0]
  - title: ≤ 200 chars (GH issue title limit)
  - id: 26-char ULID (no validation beyond length — ULIDs are
    base32-Crockford but enforcing the alphabet adds little value
    when the producer is our own code, not user input)
"""
from __future__ import annotations
import datetime as dt
from typing import Literal, Annotated
from pydantic import BaseModel, Field, field_validator


Mode = Literal["A", "B", "D", "E", "F", "G", "H", "I", "J"]
Cluster = Literal["dev", "prd", "nas", "global"]
Severity = Literal["high", "medium", "low", "info"]


class Evidence(BaseModel):
    """One piece of evidence the agent looked at while producing a finding.

    `excerpt` is optional — for logs we sometimes inline a snippet; for
    metrics/commits the `ref` (e.g. PromQL query, commit SHA) is enough.
    """
    type: Literal["alert", "log", "metric", "commit", "helmrelease", "pr", "issue", "doc"]
    ref: str
    excerpt: str | None = None


class AutoAction(BaseModel):
    """Action the agent already took as part of resolving this finding.

    Nullable on Finding — most findings just record the issue; only Mode F
    (auto-PR) + Mode J (auto-merge) actually take an action.
    """
    type: Literal["draft_pr", "comment", "label", "issue_create"]
    ref: str


class Finding(BaseModel):
    """Structured output of an agent mode run.

    One Finding may correspond to one GH issue (subject to dedup) or
    one wiki report entry. Stored in state.db.findings, dispatched to
    GH/wiki/email/Grafana per the spec § 3.3.
    """
    id: Annotated[str, Field(min_length=26, max_length=26)]   # ULID
    created_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    mode: Mode
    cluster: Cluster
    severity: Severity
    title: str
    summary: str
    evidence: list[Evidence]
    root_cause_hypothesis: str | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    recommended_action: str | None = None
    runbook_ref: str | None = None
    auto_action: AutoAction | None = None
    dedup_key: str

    @field_validator("title")
    @classmethod
    def title_must_be_short(cls, v: str) -> str:
        if len(v) > 200:
            raise ValueError("title must be ≤ 200 chars (GH issue title limit)")
        return v


# ── Daily Digest (P2, 2026-05-26) ────────────────────────────────────
#
# The digest model replaces the per-alert 5-min triage model with a
# once-daily synthesis pass: ONE LLM call per cluster examines 24h of
# alert history + Loki excerpts and produces a curated Report that
# lists ONLY the things the operator should act on. Most days that's
# 0-3 Findings; bad days may be more. Self-healed transients are
# summarized in narrative form but do NOT create GH issues.


Chronicity = Literal[
    "chronic",      # firing > 1h cumulative duration in window
    "transient",    # one-off, resolved within 5min
    "flapping",     # > 3 fire/resolve cycles in window
    "self_healed",  # fired and resolved without intervention
    "active",       # currently firing, not yet long enough to call chronic
]


class AlertGroup(BaseModel):
    """One (alertname, fingerprint) group's behaviour over the digest window.

    Produced by `digest_aggregator` from raw Prometheus ALERTS-metric
    time-series; consumed by the digest prompt. The LLM never sees raw
    fire-by-fire events — it sees this pre-classified summary, which
    keeps the prompt to ~10 lines per group instead of ~200 raw points.
    """
    alertname: str
    fingerprint: str
    labels: dict[str, str]   # everything from the alert's labels block
    fire_count: int          # distinct fire→resolve cycles in window
    total_firing_seconds: int   # cumulative time alert was in state=firing
    chronicity: Chronicity
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
    currently_firing: bool
    sample_annotation: str | None = None   # one-line summary from the latest alert annotations


class Report(BaseModel):
    """Output of one daily-digest LLM call (one Report per cluster per day).

    The digest is the agent's narrative for the cluster's last 24h. The
    operator reads `summary` first; `findings` is the actionable list
    (each Finding → 1 GH issue via the existing dispatch path). If
    `quiet_period=True`, no findings emitted — agent saw nothing worth
    paging on. `total_alerts_24h` / `chronic_count` etc. are telemetry
    so the operator can scan whether the digest is finding real signal
    or just rubber-stamping a quiet cluster.
    """
    id: Annotated[str, Field(min_length=26, max_length=26)]   # ULID
    created_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    mode: Mode = "A"
    cluster: Cluster
    digest_window_hours: int
    summary: str                          # 2-4 sentence narrative for the operator
    quiet_period: bool                    # true = nothing actionable, no findings
    findings: list[Finding]               # 0..N actionable items, each → 1 GH issue
    total_alerts_24h: int                 # raw count of distinct fire events
    chronic_count: int
    transient_count: int
    self_healed_count: int

    @field_validator("summary")
    @classmethod
    def summary_must_be_reasonable(cls, v: str) -> str:
        if len(v) > 2000:
            raise ValueError("digest summary must be ≤ 2000 chars (operator-readable)")
        return v


# ── P3: Log-pattern mining (2026-05-26) ────────────────────────────
#
# Extends Mode A's digest to surface logs containing errors/warnings
# that didn't trigger an Alertmanager alert. Two flavors of signal:
#
#   - ratio_outlier: namespace's 24h error/warn line count is ≥3× its
#                    7d rolling baseline AND ≥50 lines absolute (so
#                    a busy namespace getting busier shows up, but
#                    "5 → 15 errors" doesn't).
#
#   - tripwire:      one of ~10 hard-coded regexes matched (panic,
#                    OOMKilled, CrashLoopBackOff, certificate expired,
#                    etc.). Always surfaced regardless of count/ratio
#                    — one occurrence is enough.
#
# These feed into the digest LLM call as additional context. The LLM
# decides whether to elevate a pattern to a full Finding (default
# severity:info) or fold it into an alert-derived Finding's evidence.

LogPatternChronicity = Literal["ratio_outlier", "tripwire"]


class LogPattern(BaseModel):
    """One notable log pattern surfaced from 24h of Loki history.

    Produced by digest_aggregator.aggregate_log_patterns(); consumed
    by the digest prompt. NOT a Finding — the LLM decides whether a
    pattern is worth elevating.

    For tripwires (count-independent), `baseline_mean_24h` and
    `ratio_vs_baseline` are None; `matched_tripwire` carries the
    regex label that fired.

    For ratio_outliers, `matched_tripwire` is None; the ratio fields
    carry the statistical detail.

    `sample_lines` are up to 3 representative log lines with secrets
    redacted (see _redact() in digest_aggregator). Lines are capped
    at 200 chars each. Empty list is allowed (LLM-only-the-stats case).
    """
    namespace: str
    level: Literal["error", "warn", "fatal", "panic"]
    chronicity: LogPatternChronicity
    count_24h: int
    baseline_mean_24h: float | None = None       # None for tripwires
    ratio_vs_baseline: float | None = None       # None for tripwires
    matched_tripwire: str | None = None          # short label, e.g. "panic" / "OOMKilled"
    sample_lines: list[str] = Field(default_factory=list)
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
