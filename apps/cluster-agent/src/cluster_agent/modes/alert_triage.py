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
"""
from __future__ import annotations
import asyncio
import dataclasses
import datetime as dt
import hashlib
import logging
import os

from ..emit.metrics import MODE_A_TICKS_SKIPPED
from ..llm import triage_alert, LLMBudgetExceeded
from ..state.db import StateDB
from ..state.dedup import lookup, DedupAction
from ..tools.alertmanager import alertmanager_alerts
from ..dispatch import dispatch
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
        MODE_A_TICKS_SKIPPED.labels(cluster=cluster, reason="no_alerts").inc()
        return ModeResult(alerts_seen=0, findings_emitted=0, findings_skipped_dedup=0)

    sdb = StateDB(os.environ["STATE_DB_PATH"])
    model = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
    budget = float(os.environ.get("MODE_A_BUDGET_USD", "0.50"))

    # Tick-level cost gate (2026-05-26): if the active alert set is
    # byte-for-byte the same as last tick AND every alert in the set
    # already has an open GH issue in state.db, this tick would just
    # re-fire the LLM on the same alerts and produce the same findings
    # the previous tick already produced. Skip the whole tick — saves
    # ~$0.035/run/cluster of pure waste. NEW alerts (hash changes) still
    # trigger immediately; alerts whose GH issue was closed by operator
    # (all_have_open_issues=False) also fall through to normal flow so
    # the REOPEN dispatch path fires.
    alert_set_hash = _hash_alert_set(alerts)
    all_have_issues = _all_alerts_have_open_issues(sdb, alerts, cluster)
    prev = sdb.fetchone(
        "SELECT last_alert_set_hash, all_have_open_issues FROM mode_a_tick_state "
        "WHERE cluster = ?",
        (cluster,),
    )
    if (
        prev is not None
        and prev["last_alert_set_hash"] == alert_set_hash
        and prev["all_have_open_issues"]
        and all_have_issues
    ):
        # Update the tick-state row so last_evaluated_at advances (lets
        # operator see in state.db that the agent is still alive even
        # when it's not calling the LLM).
        sdb.execute(
            "UPDATE mode_a_tick_state SET last_evaluated_at = ? WHERE cluster = ?",
            (dt.datetime.now(dt.timezone.utc).isoformat(), cluster),
        )
        MODE_A_TICKS_SKIPPED.labels(cluster=cluster, reason="alert_set_unchanged").inc()
        log.info(
            "mode_a tick skipped (cluster=%s alerts_seen=%d): alert-set unchanged "
            "+ all have open issues", cluster, len(alerts),
        )
        return ModeResult(
            alerts_seen=len(alerts),
            findings_emitted=0,
            findings_skipped_dedup=len(alerts),
        )

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
        dispatch(finding, action, db=sdb)
        emitted += 1

    # Record the tick state so the next tick can short-circuit if
    # nothing changed. We mark all_have_open_issues based on the post-
    # dispatch state (every alert this tick processed either created
    # or commented on an issue, so they all have refs now).
    final_all_have = _all_alerts_have_open_issues(sdb, alerts, cluster)
    sdb.execute(
        "INSERT INTO mode_a_tick_state (cluster, last_alert_set_hash, "
        "last_evaluated_at, all_have_open_issues) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(cluster) DO UPDATE SET "
        "last_alert_set_hash = excluded.last_alert_set_hash, "
        "last_evaluated_at = excluded.last_evaluated_at, "
        "all_have_open_issues = excluded.all_have_open_issues",
        (
            cluster,
            alert_set_hash,
            dt.datetime.now(dt.timezone.utc).isoformat(),
            1 if final_all_have else 0,
        ),
    )

    return ModeResult(
        alerts_seen=len(alerts),
        findings_emitted=emitted,
        findings_skipped_dedup=skipped,
    )


def _hash_alert_set(alerts: list[dict]) -> str:
    """Stable hash of (alertname, fingerprint) tuples for the active set.

    Alertmanager's `fingerprint` field is its hash of the alert's labels,
    so two structurally-identical alerts (same name + same labels) hash
    to the same value. Using fingerprint avoids label-ordering issues
    we'd hit if we hashed labels directly.

    Sorted before hashing so the order AM returns alerts in (which can
    vary tick-to-tick) doesn't change the hash.
    """
    fingerprints = sorted(
        (
            a.get("labels", {}).get("alertname", ""),
            a.get("fingerprint", ""),
        )
        for a in alerts
    )
    h = hashlib.sha256()
    for name, fp in fingerprints:
        h.update(name.encode())
        h.update(b"\x00")
        h.update(fp.encode())
        h.update(b"\x00")
    return h.hexdigest()


def _all_alerts_have_open_issues(sdb: StateDB, alerts: list[dict], cluster: str) -> bool:
    """Does every alert in the set already have an open GH issue?

    Uses the conservative dedup key (same one the runner uses pre-LLM)
    to look up state.db. An alert with state='open' AND a non-NULL
    gh_issue_ref counts as having an open issue. If ANY alert is missing
    an issue (e.g. operator closed it manually, or it's a brand-new
    alert that hasn't been triaged yet), returns False — the runner
    will then go down the normal flow and dispatch.
    """
    for alert in alerts:
        key = _conservative_dedup_key(alert, cluster)
        row = sdb.fetchone(
            "SELECT state, gh_issue_ref FROM findings WHERE dedup_key = ?",
            (key,),
        )
        if row is None or row["state"] != "open" or not row["gh_issue_ref"]:
            return False
    return True


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
