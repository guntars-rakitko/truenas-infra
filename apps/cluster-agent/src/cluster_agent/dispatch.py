"""Multi-surface emit for Mode A findings.

Three surfaces, in this fixed order (each best-effort — a failure on
a later surface doesn't roll back the earlier ones):

  1. Grafana annotation — always. Vertical line on the dashboards.
  2. GitHub issue — only on action.create OR action.reopen.
     action.comment posts a "still firing" comment on the existing
     issue, doesn't create a new one.
  3. SQLite state.db — always. Records the dedup_key + gh_issue_ref
     for the next dedup lookup.

**Destination (2026-07-06 graduation — spec:
truenas-infra/docs/superpowers/specs/2026-07-06-cluster-agent-digest-graduation.md).**
Individual findings (ALL severities) file in the ops repo via
`FINDINGS_REPO` (→ `guntars-rakitko/kube-infra`); the daily digest-
SUMMARY has its own destination (`DIGEST_REPO`, handled in
modes/summary_issue.py). `FINDINGS_REPO` falls back to `DIGEST_REPO`
then `SANDBOX_REPO` so a half-applied cutover never 0-outs findings.
`FINDINGS_MIN_SEVERITY` (default empty = every finding files) is an
inert escape hatch if low/info findings ever prove noisy in the ops
repo. On COMMENT/REOPEN the comment lands on the repo embedded in the
stored `gh_issue_ref` (not the current env repo) — so findings created
in the old repo before the cutover keep being updated there.
"""
from __future__ import annotations
import dataclasses
import logging
import os

from .emit.metrics import DISPATCH_ERRORS
from .schema import Finding
from .state.db import StateDB
from .state.dedup import DedupAction, _DedupActionKind, record
from .tools.grafana import post_annotation
from .tools.github import gh_issue_create, gh_issue_comment


log = logging.getLogger(__name__)


@dataclasses.dataclass
class DispatchResult:
    finding_id: str
    gh_issue_ref: str | None
    grafana_annotation_id: str | None


def _issue_body(finding: Finding) -> str:
    """Render a Finding as a GitHub-flavored markdown issue body."""
    lines: list[str] = [
        f"**Mode:** {finding.mode}  ·  **Cluster:** {finding.cluster}  ·  **Severity:** {finding.severity}  ·  **Confidence:** {finding.confidence:.2f}",
        "",
        f"## Summary",
        "",
        finding.summary,
    ]
    if finding.root_cause_hypothesis:
        lines += ["", "## Root cause hypothesis", "", finding.root_cause_hypothesis]
    if finding.recommended_action:
        lines += ["", "## Recommended action", "", finding.recommended_action]
    if finding.runbook_ref:
        lines += ["", f"Runbook: `{finding.runbook_ref}`"]
    lines += ["", "## Evidence", ""]
    for ev in finding.evidence:
        if ev.excerpt:
            lines.append(f"- **{ev.type}** `{ev.ref}` — `{ev.excerpt[:200]}`")
        else:
            lines.append(f"- **{ev.type}** `{ev.ref}`")
    lines += ["", "---", f"dedup_key: `{finding.dedup_key}`  ·  finding_id: `{finding.id}`"]
    return "\n".join(lines)


# Severity ordering for the optional FINDINGS_MIN_SEVERITY floor.
_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3}


def _findings_repo() -> str | None:
    """Repo NEW findings file into. `FINDINGS_REPO` graduated findings to
    the ops repo (kube-infra); falls back to `DIGEST_REPO` then the legacy
    `SANDBOX_REPO` so a half-applied cutover never silently drops findings."""
    return (
        os.environ.get("FINDINGS_REPO")
        or os.environ.get("DIGEST_REPO")
        or os.environ.get("SANDBOX_REPO")
    )


def _below_findings_floor(severity: str) -> bool:
    """True iff FINDINGS_MIN_SEVERITY is set AND this finding is below it.

    Empty/unset/invalid floor ⇒ always False (every finding files — the
    default). Escape hatch only: set to `medium`/`high` if low/info
    findings ever prove noisy in the ops repo. Applies to CREATE only —
    an already-open issue keeps getting recurrence comments regardless."""
    floor = os.environ.get("FINDINGS_MIN_SEVERITY", "").strip().lower()
    if floor not in _SEVERITY_RANK:
        return False
    return _SEVERITY_RANK.get(severity, 0) < _SEVERITY_RANK[floor]


def _recurrence_comment(finding: Finding, *, reopened: bool = False) -> str:
    """Body for a dedup COMMENT/REOPEN. Leads with an explicit
    'still firing as of <date>' so a live-but-known finding is
    distinguishable from one that went quiet, then repeats the current
    evidence for context."""
    date = finding.created_at.date().isoformat()
    lead = (
        f"**Re-fired after closure — still firing as of {date}**"
        if reopened
        else f"**Still firing as of {date}**"
    )
    return f"{lead} (re-observed {finding.created_at.isoformat()}).\n\n" + _issue_body(finding)


def _comment_target(gh_issue_ref: str, fallback_repo: str | None) -> tuple[str | None, int | None]:
    """Split a stored `owner/repo#NN` ref into (repo, number).

    The ref is repo-qualified, so a COMMENT/REOPEN lands on the issue's
    OWN repo — critical across the sandbox→kube-infra cutover, where an
    in-flight finding's issue lives in the old repo while new findings go
    to the new one. Legacy refs without a repo prefix fall back to
    `fallback_repo`."""
    ref_repo, sep, ref_num = gh_issue_ref.rpartition("#")
    if not sep:
        return fallback_repo, None
    repo = ref_repo or fallback_repo
    return (repo, int(ref_num)) if ref_num.isdigit() else (repo, None)


def dispatch(finding: Finding, action: DedupAction, *, db: StateDB) -> DispatchResult:
    """Write the finding to all 3 surfaces. Returns refs for each."""
    time_ms = int(finding.created_at.timestamp() * 1000)
    tags = [
        "cluster-agent",
        f"mode:{finding.mode}",
        f"cluster:{finding.cluster}",
        f"severity:{finding.severity}",
    ]

    # 1) Grafana — always
    grafana_id: str | None = None
    try:
        grafana_id = post_annotation(
            cluster=finding.cluster,
            text=finding.title,
            tags=tags,
            time_ms=time_ms,
        )
    except Exception as e:
        log.warning("grafana annotation failed: %r", e)
        DISPATCH_ERRORS.labels(surface="grafana_annotation").inc()

    # 2) GH — create or comment, conditionally
    repo = _findings_repo()
    gh_ref: str | None = None
    if action.kind == _DedupActionKind.CREATE:
        if _below_findings_floor(finding.severity):
            log.info(
                "finding %s severity=%s below FINDINGS_MIN_SEVERITY floor; "
                "Grafana-only, no issue filed",
                finding.id, finding.severity,
            )
        elif not repo:
            log.warning(
                "no findings repo set (FINDINGS_REPO/DIGEST_REPO/SANDBOX_REPO); "
                "skipping GH create"
            )
        else:
            try:
                resp = gh_issue_create(
                    repo,
                    title=finding.title,
                    body=_issue_body(finding),
                    labels=[
                        # `cluster-agent` = provenance (lets the operator
                        # filter/hide the bot's issues in the ops repo);
                        # `needs-review` = the triage queue
                        # (`label:needs-review` == "surfaced, not yet
                        # triaged"). Both are created in kube-infra by the
                        # 2026-07-06 graduation rollout.
                        "cluster-agent",
                        "needs-review",
                        f"mode-{finding.mode}",
                        f"severity-{finding.severity}",
                        # `kub-{cluster}` matches the Loki/Prom cluster
                        # label convention (`cluster=kub-prd` / `kub-dev`
                        # is what gets stamped on every series). Keeps
                        # GH issue labels grep-compatible with metrics.
                        f"kub-{finding.cluster}",
                    ],
                )
                gh_ref = f"{repo}#{resp['number']}"
            except Exception as e:
                log.warning("gh_issue_create failed: %r", e)
                DISPATCH_ERRORS.labels(surface="gh_issue_create").inc()
    elif action.kind == _DedupActionKind.COMMENT:
        gh_ref = action.gh_issue_ref
        if action.gh_issue_ref:
            ref_repo, ref_num = _comment_target(action.gh_issue_ref, repo)
            if ref_repo and ref_num is not None:
                try:
                    gh_issue_comment(ref_repo, ref_num, body=_recurrence_comment(finding))
                except Exception as e:
                    log.warning("gh_issue_comment failed: %r", e)
                    DISPATCH_ERRORS.labels(surface="gh_issue_comment").inc()
    elif action.kind == _DedupActionKind.REOPEN:
        # Closed <7d ago and the problem recurred. Add a "re-fired after
        # closure" recurrence comment on the issue's OWN repo. (Promoting
        # this to an actual state=open reopen is a small follow-up noted in
        # the graduation spec — findings are human-close-only, so the
        # comment already notifies watchers of the recurrence.)
        gh_ref = action.gh_issue_ref
        if action.gh_issue_ref:
            ref_repo, ref_num = _comment_target(action.gh_issue_ref, repo)
            if ref_repo and ref_num is not None:
                try:
                    gh_issue_comment(
                        ref_repo, ref_num,
                        body=_recurrence_comment(finding, reopened=True),
                    )
                except Exception as e:
                    log.warning("gh_issue_comment (reopen) failed: %r", e)
                    DISPATCH_ERRORS.labels(surface="gh_issue_reopen_comment").inc()

    # 3) SQLite — always (record / upsert)
    record(
        db, finding.dedup_key,
        gh_issue_ref=gh_ref,
        state="open",
        mode=finding.mode,
        cluster=finding.cluster,
        severity=finding.severity,
        payload_json=finding.model_dump_json(),
    )

    return DispatchResult(
        finding_id=finding.id,
        gh_issue_ref=gh_ref,
        grafana_annotation_id=grafana_id,
    )
