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
from typing import Any

from ..emit.metrics import (
    cluster_agent_run_total,
    cluster_agent_run_duration_seconds,
    cluster_agent_last_success_timestamp,
)
from ..llm import triage_digest, LLMBudgetExceeded
from ..state.db import StateDB
from ..state.dedup import lookup, DedupAction
from ..tools.alertmanager import alertmanager_alerts, alertmanager_history
from ..tools.loki import loki_query, loki_metric_query_range
from ..tools.kubectl import kubectl_describe, ToolError
from ..dispatch import dispatch
from .digest_aggregator import aggregate, aggregate_log_patterns, enrich_with_annotations


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
    for finding in report.findings:
        try:
            action = lookup(sdb, finding.dedup_key)
            dispatch(finding, action, db=sdb)
            emitted += 1
        except Exception as e:
            log.warning("digest %s: dispatch of finding %s failed: %r",
                        cluster, finding.id, e)

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
    no context — they're presumed noise and not worth API calls."""
    lines: list[str] = []
    investigate_chronicities = {"chronic", "flapping", "active"}
    for g in groups:
        if g.chronicity not in investigate_chronicities:
            continue
        namespace = g.labels.get("namespace") or g.labels.get("pod_namespace")
        pod = g.labels.get("pod")
        if not namespace:
            continue
        lines.append(f"\n### {g.alertname} ({g.fingerprint}) — {g.chronicity}")
        # kubectl describe pod if we have one, otherwise namespace
        if pod:
            try:
                desc = kubectl_describe(cluster, "pods", pod, namespace=namespace)
                lines.append(f"```\n{desc[:1500]}\n```")
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
                lines.extend(log_lines)
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
