"""Mode A — alert triage.

Cron-triggered every 5 min. Each run:
  1. Polls Alertmanager for active alerts in this cluster.
  2. Skips alerts already deduped to a recent open finding (per state.db).
  3. For each kept alert: gathers context, asks the LLM for a Finding.
  4. Dispatches the Finding to SQLite + Grafana + GH sandbox repo.

Scheduler calls run(cluster=...) (sync). run() drives run_async() in a
fresh event loop. We don't share an asyncio loop with FastAPI's
uvicorn instance because the scheduler thread is separate; spinning a
loop per run is fine — Mode A only fires every 5 min.

Per-mode kill switch is in scheduler.py (not here); if Mode A is
disabled, the scheduler closure never calls into this module.

Surface helpers (`post_annotation`, `gh_issue_create`, `gh_issue_comment`)
are imported at module level so tests can monkeypatch them on this
module without reaching into the underlying tools modules. The
multi-surface emit is done inline in this module (rather than via
`dispatch.dispatch`) so the monkeypatching is honored.
"""
from __future__ import annotations
import asyncio
import dataclasses
import datetime as dt
import logging
import os

from ..llm import triage_alert, LLMBudgetExceeded
from ..schema import Finding
from ..state.db import StateDB
from ..state.dedup import lookup, DedupAction, _DedupActionKind, record
from ..tools.alertmanager import alertmanager_alerts
from ..tools.grafana import post_annotation                  # re-exported for stub-friendliness in tests
from ..tools.github import gh_issue_create, gh_issue_comment  # re-exported for stub-friendliness in tests
from ..dispatch import _issue_body  # rendering helper only — no I/O
from .context import gather_context_for_alert


log = logging.getLogger(__name__)


@dataclasses.dataclass
class ModeResult:
    alerts_seen: int
    findings_emitted: int
    findings_skipped_dedup: int


async def run_async(*, cluster: str) -> ModeResult:
    """Mode A async runner. See module docstring."""
    try:
        alerts = alertmanager_alerts(cluster, active=True, silenced=False, inhibited=False)
    except Exception as e:
        log.warning("alertmanager_alerts failed: %r", e)
        return ModeResult(alerts_seen=0, findings_emitted=0, findings_skipped_dedup=0)

    if not alerts:
        return ModeResult(alerts_seen=0, findings_emitted=0, findings_skipped_dedup=0)

    sdb = StateDB(os.environ["STATE_DB_PATH"])
    model = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
    budget = float(os.environ.get("MODE_A_BUDGET_USD", "0.50"))

    emitted = 0
    skipped = 0

    for alert in alerts:
        # We don't have the LLM's dedup_key yet (the LLM picks the
        # scope_id). Pessimistic dedup-before-LLM uses a conservative
        # key from the alert labels. If the LLM produces a different
        # dedup_key, the post-LLM lookup() will re-decide the action;
        # state.db is still updated correctly under the LLM's key.
        conservative_key = _conservative_dedup_key(alert, cluster)
        pre_action = lookup(sdb, conservative_key)
        # If pre-action is COMMENT and the issue was already re-commented
        # within the last hour, skip — repeated re-fires on the same
        # alert within an hour just create comment noise.
        if pre_action == DedupAction.comment and _recently_commented(sdb, conservative_key, hours=1):
            skipped += 1
            continue

        # Gather context
        context = gather_context_for_alert(alert, cluster=cluster)

        # Call LLM
        try:
            finding = await triage_alert(
                alert=alert,
                context=context,
                cluster=cluster,
                model=model,
                budget_usd=budget,
            )
        except LLMBudgetExceeded as e:
            log.warning("Mode A budget exceeded on alert %s: %r", alert.get("labels"), e)
            continue
        except ValueError as e:
            log.warning("Mode A LLM-output parse failed on alert %s: %r",
                        alert.get("labels"), e)
            continue
        except Exception as e:
            log.warning("Mode A LLM call failed on alert %s: %r",
                        alert.get("labels"), e)
            continue

        # Re-lookup with the LLM's dedup_key and dispatch
        action = lookup(sdb, finding.dedup_key)
        _emit(finding, action, sdb)
        emitted += 1

    return ModeResult(
        alerts_seen=len(alerts),
        findings_emitted=emitted,
        findings_skipped_dedup=skipped,
    )


def _emit(finding: Finding, action: DedupAction, sdb: StateDB) -> None:
    """Multi-surface emit (Grafana + GH + SQLite). Inlined here so the
    module-level imports (`post_annotation`, `gh_issue_create`,
    `gh_issue_comment`) are the live surface — tests can monkeypatch
    them on this module without touching the underlying tools."""
    time_ms = int(finding.created_at.timestamp() * 1000)
    tags = [
        "cluster-agent",
        f"mode:{finding.mode}",
        f"cluster:{finding.cluster}",
        f"severity:{finding.severity}",
    ]

    # 1) Grafana — always
    try:
        post_annotation(cluster=finding.cluster, text=finding.title,
                        tags=tags, time_ms=time_ms)
    except Exception as e:
        log.warning("grafana annotation failed: %r", e)

    # 2) GH — create or comment, conditionally
    repo = os.environ.get("SANDBOX_REPO")
    gh_ref: str | None = None
    if action.kind == _DedupActionKind.CREATE:
        if not repo:
            log.warning("SANDBOX_REPO not set; skipping GH create")
        else:
            try:
                resp = gh_issue_create(
                    repo,
                    title=finding.title,
                    body=_issue_body(finding),
                    labels=[
                        f"mode-{finding.mode}",
                        f"severity-{finding.severity}",
                        f"cluster-{finding.cluster}",
                    ],
                )
                gh_ref = f"{repo}#{resp['number']}"
            except Exception as e:
                log.warning("gh_issue_create failed: %r", e)
    elif action.kind == _DedupActionKind.COMMENT:
        gh_ref = action.gh_issue_ref
        if action.gh_issue_ref and repo:
            try:
                number = int(action.gh_issue_ref.split("#")[-1])
                gh_issue_comment(
                    repo, number,
                    body=f"Re-fired at {finding.created_at.isoformat()}.\n\n" + _issue_body(finding),
                )
            except Exception as e:
                log.warning("gh_issue_comment failed: %r", e)
    elif action.kind == _DedupActionKind.REOPEN:
        # For the P1 dev soak we treat reopen identically to comment.
        gh_ref = action.gh_issue_ref
        if action.gh_issue_ref and repo:
            try:
                number = int(action.gh_issue_ref.split("#")[-1])
                gh_issue_comment(
                    repo, number,
                    body=f"Re-fired after closure at {finding.created_at.isoformat()}.\n\n" + _issue_body(finding),
                )
            except Exception as e:
                log.warning("gh_issue_comment (reopen) failed: %r", e)

    # 3) SQLite — always
    record(
        sdb, finding.dedup_key,
        gh_issue_ref=gh_ref,
        state="open",
        mode=finding.mode,
        cluster=finding.cluster,
        severity=finding.severity,
        payload_json=finding.model_dump_json(),
    )


def _conservative_dedup_key(alert: dict, cluster: str) -> str:
    labels = alert.get("labels", {})
    alertname = labels.get("alertname", "unknown")
    scope = labels.get("pod") or labels.get("namespace") or "global"
    return f"alert:{alertname}:{scope}:{cluster}"


def _recently_commented(sdb: StateDB, dedup_key: str, *, hours: int) -> bool:
    """Has this finding been COMMENTED on (not just created) within the
    window? We use last_seen_at > created_at as the signal that at least
    one prior re-fire was already recorded; otherwise this is a fresh
    finding that we should still dispatch on (the first re-fire).
    """
    row = sdb.fetchone(
        "SELECT created_at, last_seen_at FROM findings WHERE dedup_key=?",
        (dedup_key,),
    )
    if not row:
        return False
    try:
        last = dt.datetime.fromisoformat(row["last_seen_at"])
        created = dt.datetime.fromisoformat(row["created_at"])
    except Exception:
        return False
    if last <= created:
        # Never re-commented yet → don't skip; let the run dispatch a comment.
        return False
    return (dt.datetime.now(dt.timezone.utc) - last) < dt.timedelta(hours=hours)


def run(*, cluster: str) -> ModeResult:
    """Sync entrypoint for APScheduler. See module docstring."""
    return asyncio.run(run_async(cluster=cluster))
