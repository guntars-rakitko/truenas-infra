"""Multi-surface emit for Mode A findings.

Three surfaces, in this fixed order (each best-effort — a failure on
a later surface doesn't roll back the earlier ones):

  1. Grafana annotation — always. Vertical line on the dashboards.
  2. GitHub issue (sandbox repo) — only on action.create OR
     action.reopen. action.comment posts a comment on the existing
     issue, doesn't create a new one.
  3. SQLite state.db — always. Records the dedup_key + gh_issue_ref
     for the next dedup lookup.

P2 will switch the GH destination from the sandbox repo to the real
kube-infra issues (gated on operator's review of ≥20 findings during
P1 soak per spec § 7.3).
"""
from __future__ import annotations
import dataclasses
import logging
import os

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
        # For the P1 dev soak we treat reopen identically to comment —
        # the issue stays open, we add a re-fire comment. Promoting
        # reopen-as-state-change to a separate GH API call lands in P2.
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
