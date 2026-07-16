"""Mode A — daily-digest runner (2026-05-26 pivot).

Replaces the 5-min per-alert triage model. Fires once per day per
cluster at DAILY_DIGEST_HOUR:MINUTE in DAILY_DIGEST_TZ. Each run:

  1. Pull 24h of ALERTS metric history from Prometheus (via apiserver
     proxy)
  2. Aggregate into AlertGroups with chronicity classification
  3. Enrich currently-firing groups with annotations from AM /api/v2/alerts
  4. Pull context excerpts (Loki + kubectl describe) for chronic +
     flapping groups only — transient/self_healed get no context
  5. Look up open finding dedup_keys for this cluster from state.db
     (so LLM can avoid re-creating issues for things already tracked)
  6. ONE LLM call → Report (0..N actionable Findings)
  7. For each Finding: dispatch through existing pipeline (Grafana
     annotation + GH issue + state.db record)
  8. Update mode_runs audit row with cost + token totals

If 0 findings emitted (quiet day or all noise), the digest is "silent" —
no GH issues created. The metric `cluster_agent_run_total{mode='A',
status='success_quiet'}` distinguishes quiet runs from active ones.
"""
from __future__ import annotations
import asyncio
import dataclasses
import datetime as dt
import json
import logging
import os
import re
from collections import defaultdict
from typing import Any

from ..emit.metrics import (
    cluster_agent_run_total,
    cluster_agent_run_duration_seconds,
    cluster_agent_last_success_timestamp,
)
from ..llm import triage_digest, LLMBudgetExceeded
from ..state.db import StateDB
from ..state.dedup import lookup, mark_closed, DedupAction
from ..tools.alertmanager import alertmanager_alerts, alertmanager_history
from ..tools.loki import loki_query, loki_metric_query_range
from ..tools.kubectl import kubectl_describe, ToolError
from ..tools.github import gh_issue_list
from ..dispatch import dispatch, _findings_repo
from .digest_aggregator import (
    aggregate,
    aggregate_log_patterns,
    enrich_with_annotations,
    _redact,
)


log = logging.getLogger(__name__)


@dataclasses.dataclass
class DigestResult:
    cluster: str
    alert_groups_seen: int
    findings_emitted: int
    quiet_period: bool
    summary: str
    error: str | None = None


async def run_async(*, cluster: str) -> DigestResult:
    """Daily-digest runner. See module docstring."""
    start = dt.datetime.now(dt.timezone.utc)
    window_hours = int(os.environ.get("DAILY_DIGEST_WINDOW_HOURS", "24"))
    model = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
    budget = float(os.environ.get("DAILY_DIGEST_BUDGET_USD", "0.50"))

    # ── 1. Fetch raw 24h alert history from Prom ────────────────────
    try:
        history = alertmanager_history(cluster, since_hours=window_hours)
    except Exception as e:
        log.warning("digest %s: alertmanager_history failed: %r", cluster, e)
        cluster_agent_run_total.labels(mode="A", status="error").inc()
        return DigestResult(
            cluster=cluster, alert_groups_seen=0, findings_emitted=0,
            quiet_period=True, summary="", error=f"history fetch failed: {e}",
        )

    # ── 2. Aggregate into AlertGroups ───────────────────────────────
    groups = aggregate(history, step_seconds=60)
    if not groups:
        # Genuinely quiet 24h — no alerts fired at all
        cluster_agent_run_total.labels(mode="A", status="success_quiet").inc()
        log.info("digest %s: quiet period — no alerts in last %dh", cluster, window_hours)
        return DigestResult(
            cluster=cluster, alert_groups_seen=0, findings_emitted=0,
            quiet_period=True, summary=f"No alerts fired in the last {window_hours}h.",
        )

    # ── 3. Enrich with annotations from current AM active set ───────
    try:
        active = alertmanager_alerts(cluster, active=True, silenced=False, inhibited=False)
        enrich_with_annotations(groups, active)
    except Exception as e:
        # Non-fatal — annotations are nice-to-have, alertname+labels
        # alone are usually enough for the LLM to identify the issue.
        log.warning("digest %s: annotation enrichment failed: %r", cluster, e)

    # ── 4. Pull context excerpts for chronic + flapping groups only ─
    context_excerpts = _gather_context_for_chronic(groups, cluster)

    # ── 4b. P3: Log-pattern mining (statistical outliers + tripwires) ─
    # Failures here don't abort the digest — log patterns are
    # supplementary signal, not the primary input. If Loki is down or
    # the metric query fails, we still produce a useful alert digest.
    try:
        log_patterns = aggregate_log_patterns(
            cluster,
            loki_query_fn=loki_query,
            loki_metric_query_fn=loki_metric_query_range,
            window_hours=window_hours,
        )
    except Exception as e:
        log.warning("digest %s: log-pattern mining failed: %r", cluster, e)
        log_patterns = []

    # ── 5. Open issue dedup keys ────────────────────────────────────
    sdb = StateDB(os.environ["STATE_DB_PATH"])
    # 5a. Reconcile state.db against real GH issue state FIRST, so the
    # dedup list below reflects what's actually still open. Without this,
    # operator-closed findings stay state='open' forever and the LLM keeps
    # skipping the chronic condition as "already tracked" — see
    # _reconcile_finding_states docstring.
    try:
        _reconcile_finding_states(sdb, _findings_repo())
    except Exception as e:  # never let reconcile break the digest
        log.warning("digest %s: finding-state reconcile failed: %r", cluster, e)
    open_keys = _load_open_dedup_keys(sdb, cluster)

    # ── 6. ONE LLM call → Report ────────────────────────────────────
    try:
        report = await triage_digest(
            cluster=cluster,
            alert_groups=groups,
            log_patterns=log_patterns,
            open_issue_keys=open_keys,
            context_excerpts=context_excerpts,
            window_hours=window_hours,
            model=model,
            budget_usd=budget,
        )
    except LLMBudgetExceeded as e:
        log.warning("digest %s budget exceeded: %r", cluster, e)
        cluster_agent_run_total.labels(mode="A", status="aborted_budget").inc()
        return DigestResult(
            cluster=cluster, alert_groups_seen=len(groups), findings_emitted=0,
            quiet_period=True, summary="", error=f"budget exceeded: {e}",
        )
    except ValueError as e:
        log.warning("digest %s LLM-output parse failed: %r", cluster, e)
        cluster_agent_run_total.labels(mode="A", status="error").inc()
        return DigestResult(
            cluster=cluster, alert_groups_seen=len(groups), findings_emitted=0,
            quiet_period=True, summary="", error=f"LLM parse failed: {e}",
        )
    except Exception as e:
        log.warning("digest %s LLM call failed: %r", cluster, e)
        cluster_agent_run_total.labels(mode="A", status="error").inc()
        return DigestResult(
            cluster=cluster, alert_groups_seen=len(groups), findings_emitted=0,
            quiet_period=True, summary="", error=f"LLM call failed: {e}",
        )

    # ── 7. Dispatch each Finding through the existing pipeline ──────
    emitted = 0
    dispatch_refs: dict[str, str] = {}    # finding.id → "owner/repo#NN"
    for finding in report.findings:
        try:
            action = lookup(sdb, finding.dedup_key)
            result = dispatch(finding, action, db=sdb)
            emitted += 1
            if result is not None and result.gh_issue_ref:
                dispatch_refs[finding.id] = result.gh_issue_ref
        except Exception as e:
            log.warning("digest %s: dispatch of finding %s failed: %r",
                        cluster, finding.id, e)

    # ── 7b. Optional daily-summary delivery ─────────────────────────
    # One markdown summary per cluster per day listing ALL alert
    # groups (including chronic-but-known noise the LLM didn't escalate),
    # so the operator can spot recurring self-healing patterns that
    # might point at a real underlying issue. Zero LLM cost — pure
    # in-memory aggregation of AlertGroup data we already have.
    # Destinations are CSV in Doppler DIGEST_SUMMARY:
    #   ""           → disabled
    #   "issue"      → GH issue only
    #   "email"      → email only
    #   "email,issue"→ both
    try:
        from .summary_issue import emit_summary
        # Digest-SUMMARY destination — the renamed permanent digest repo
        # (distinct from the per-finding destination, which dispatch()
        # routes to FINDINGS_REPO/kube-infra). Falls back to the legacy
        # SANDBOX_REPO through the rename cutover. (2026-07-06 graduation.)
        repo = os.environ.get("DIGEST_REPO") or os.environ.get(
            "SANDBOX_REPO", "guntars-rakitko/cluster-agent-digest"
        )
        emit_summary(
            repo=repo,
            cluster=cluster,
            groups=groups,
            log_patterns=log_patterns,
            findings=report.findings,
            dispatch_refs=dispatch_refs,
            window_hours=window_hours,
            model=model,
            digest_summary=report.summary or "",
        )
    except Exception as e:
        # Summary delivery is best-effort — never break the digest.
        log.warning("digest %s: summary emit failed: %r", cluster, e)

    # ── 8. Audit + metrics ──────────────────────────────────────────
    duration = (dt.datetime.now(dt.timezone.utc) - start).total_seconds()
    cluster_agent_run_duration_seconds.labels(mode="A").observe(duration)
    cluster_agent_run_total.labels(
        mode="A",
        status="success_quiet" if report.quiet_period else "success",
    ).inc()
    cluster_agent_last_success_timestamp.labels(mode="A").set(dt.datetime.now().timestamp())

    log.info(
        "digest %s done in %.1fs: %d groups, %d findings, quiet=%s",
        cluster, duration, len(groups), emitted, report.quiet_period,
    )
    return DigestResult(
        cluster=cluster, alert_groups_seen=len(groups), findings_emitted=emitted,
        quiet_period=report.quiet_period, summary=report.summary,
    )


def _gather_context_for_chronic(groups: list, cluster: str) -> str:
    """Pull kubectl describe + a small Loki excerpt for each chronic/
    flapping/active alert group. Self-healed and transient groups get
    no context — they're presumed noise and not worth API calls.

    **All raw output is passed through `_redact()`** before being
    appended to the context. The context blob flows downstream into:
    the LLM prompt, GH issue bodies (public-by-default sandbox repo),
    the SES email body, and the audit log. Any of these is an
    unacceptable secret-leak surface for un-redacted log/describe
    text. P4 hardening (2026-05-27): redaction added after the security
    audit flagged that `_redact()` was only applied to the P3
    tripwire path, not the per-chronic-alert context path.
    """
    # Namespace label is RFC-1123 (`[a-z0-9-]{1,63}`), but defensively
    # validate before string-interpolating into LogQL to bound any
    # malformed-label blast radius. See security audit P4 follow-up.
    _NS_RE = re.compile(r"^[a-z0-9-]{1,63}$")

    lines: list[str] = []
    investigate_chronicities = {"chronic", "flapping", "active"}
    for g in groups:
        if g.chronicity not in investigate_chronicities:
            continue
        namespace = g.labels.get("namespace") or g.labels.get("pod_namespace")
        pod = g.labels.get("pod")
        if not namespace:
            continue
        if not _NS_RE.fullmatch(namespace):
            log.warning(
                "digest %s: skipping chronic %s — namespace %r doesn't "
                "match RFC-1123, refusing to interpolate into LogQL",
                cluster, g.alertname, namespace,
            )
            continue
        lines.append(f"\n### {g.alertname} ({g.fingerprint}) — {g.chronicity}")
        # kubectl describe pod if we have one, otherwise namespace
        if pod:
            try:
                desc = kubectl_describe(cluster, "pods", pod, namespace=namespace)
                lines.append(f"```\n{_redact(desc[:1500])}\n```")
            except ToolError as e:
                lines.append(f"_(kubectl describe failed: {e})_")
        # Loki — last 30min of logs from the namespace
        try:
            logs = loki_query(
                cluster,
                f'{{namespace="{namespace}"}} |~ "(?i)error|warn|fail"',
                limit=20,
            )
            log_lines = _extract_log_excerpts(logs, max_lines=10)
            if log_lines:
                lines.append("Recent error/warning log lines:")
                lines.append("```")
                lines.extend(_redact(line) for line in log_lines)
                lines.append("```")
        except Exception as e:
            lines.append(f"_(loki query failed: {e})_")
    return "\n".join(lines)


def _extract_log_excerpts(loki_response: dict, *, max_lines: int) -> list[str]:
    """Pull up to N log lines from a Loki query response."""
    results = loki_response.get("data", {}).get("result", [])
    excerpts: list[str] = []
    for stream in results:
        for ts_value in stream.get("values", []):
            if len(excerpts) >= max_lines:
                return excerpts
            ts, line = ts_value
            excerpts.append(line[:200])
    return excerpts


def _reconcile_finding_states(sdb: StateDB, findings_repo: str | None) -> int:
    """Sync state.db 'open' findings against their real GitHub issue state.

    **Why this exists (2026-07-16 fix).** dispatch.record() only ever
    writes state='open'; nothing teaches state.db that an issue was closed
    *by the operator*. But finding issues are human-close-only (the
    `needs-review` triage queue), so every operator close leaves the
    state.db row stuck at state='open' forever. `_load_open_dedup_keys`
    then keeps handing those stale keys to the LLM, the prompt says "skip —
    already tracked by an open issue", and the chronic condition is never
    re-filed even though nothing is actually tracking it. This is the root
    cause of the "digest cites closed issues as still-open" gap: on 2026-07
    every finding issue was closed yet the Trivy / Longhorn / etc. chronic
    alerts were still being suppressed against sandbox#NN records last-seen
    in May. (Spec: docs/superpowers/specs/2026-07-16-finding-state-reconcile.md.)

    For each open finding, look up its GH issue's real state (grouped by
    repo → ≤2 API calls per distinct repo). Mark it closed in state.db when
    the issue is closed on GitHub, the issue is gone, or its repo is
    unreachable (e.g. the retired `cluster-agent-sandbox`). closed_at =
    the GH close date when known, else the finding's last_seen_at (old →
    lands outside REOPEN_WINDOW → the next emit CREATEs a fresh issue in
    the active findings repo instead of commenting on a dead one).

    Best-effort: a GH error on the *active* findings repo is swallowed so a
    transient GitHub outage can't false-close live findings; a retired /
    unreachable *other* repo is treated as "all closed" (correct — nothing
    is tracking those anymore). Returns the number of rows closed.
    """
    rows = [
        dict(r)
        for r in sdb.execute(
            "SELECT dedup_key, gh_issue_ref, last_seen_at FROM findings "
            "WHERE state='open' AND gh_issue_ref IS NOT NULL"
        ).fetchall()
    ]
    if not rows:
        return 0

    by_repo: dict[str, list[tuple[dict, int]]] = defaultdict(list)
    for r in rows:
        repo, sep, num = r["gh_issue_ref"].rpartition("#")
        if sep and repo and num.isdigit():
            by_repo[repo].append((r, int(num)))

    closed = 0
    for repo, items in by_repo.items():
        # number -> (state, closed_at); None sentinel = repo unreachable
        state_map: dict[int, tuple[str, str | None]] | None = {}
        try:
            for st in ("open", "closed"):
                for issue in gh_issue_list(repo, state=st, per_page=100):
                    state_map[issue["number"]] = (issue["state"], issue.get("closed_at"))
        except Exception as e:
            log.warning("reconcile: gh_issue_list(%s) failed: %r", repo, e)
            if repo == findings_repo:
                # Active repo + transient error → do NOT false-close.
                continue
            state_map = None  # retired/unreachable repo → treat all as closed

        for r, num in items:
            if state_map is None:
                gh_state, gh_closed = "closed", None
            else:
                gh = state_map.get(num)
                # Missing from a reachable repo (deleted / paged past 100)
                # is treated as closed — it's not an open tracker either way.
                gh_state, gh_closed = gh if gh is not None else ("closed", None)
            if gh_state == "closed":
                mark_closed(sdb, r["dedup_key"], closed_at=gh_closed or r["last_seen_at"])
                closed += 1

    if closed:
        log.info("reconcile: closed %d stale-open finding(s) vs live GH state", closed)
    return closed


def _load_open_dedup_keys(sdb: StateDB, cluster: str) -> list[str]:
    """List dedup_keys for currently-open findings in this cluster."""
    rows = sdb.execute(
        "SELECT dedup_key FROM findings WHERE state = 'open' AND cluster = ? "
        "ORDER BY last_seen_at DESC LIMIT 50",
        (cluster,),
    ).fetchall()
    return [r["dedup_key"] for r in rows]


def run(*, cluster: str) -> DigestResult:
    """Sync entrypoint for APScheduler."""
    return asyncio.run(run_async(cluster=cluster))
