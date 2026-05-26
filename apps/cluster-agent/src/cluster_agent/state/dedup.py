"""Dedup logic — controls when to create/comment/reopen GH issues.

Per spec § 4.4:
  - open issue exists for this dedup_key  → comment on existing
  - closed <7d ago                         → reopen
  - otherwise                              → create new
"""
from __future__ import annotations
import datetime as dt
import enum
from dataclasses import dataclass

from .db import StateDB


REOPEN_WINDOW = dt.timedelta(days=7)


class _DedupActionKind(enum.Enum):
    CREATE = "create"
    COMMENT = "comment"
    REOPEN = "reopen"


@dataclass
class DedupAction:
    """Result of dedup lookup. `gh_issue_ref` set when kind != create.

    Equality is sentinel-friendly so tests can write:

        action = lookup(db, key)
        assert action == DedupAction.create   # bare enum equality

    AND payload-aware so we can assert on the gh_issue_ref:

        action = lookup(db, key)
        assert action == DedupAction.comment
        assert action.gh_issue_ref == "kube-infra#100"
    """
    kind: _DedupActionKind
    gh_issue_ref: str | None = None

    def __eq__(self, other: object) -> bool:
        # Bare-enum equality (sentinel comparison)
        if isinstance(other, _DedupActionKind):
            return self.kind == other
        # Action-to-Action equality (compares both fields)
        if isinstance(other, DedupAction):
            return self.kind == other.kind and self.gh_issue_ref == other.gh_issue_ref
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.kind, self.gh_issue_ref))


# Class-attribute sentinels — lets callers write `DedupAction.create`
# without instantiating. These are enum members, not DedupAction
# instances; DedupAction.__eq__ compares against them via isinstance check.
DedupAction.create = _DedupActionKind.CREATE       # type: ignore[attr-defined]
DedupAction.comment = _DedupActionKind.COMMENT     # type: ignore[attr-defined]
DedupAction.reopen = _DedupActionKind.REOPEN       # type: ignore[attr-defined]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def lookup(db: StateDB, dedup_key: str) -> DedupAction:
    """Decide what to do for a finding with this dedup_key."""
    row = db.fetchone(
        "SELECT gh_issue_ref, state, closed_at FROM findings WHERE dedup_key=?",
        (dedup_key,),
    )
    if row is None:
        return DedupAction(kind=_DedupActionKind.CREATE)
    if row["state"] == "open":
        # Orphan record handling: if a prior run's gh_issue_create failed
        # (rate limit, transient API error, missing crypto dep, etc.) but
        # dispatch() still wrote state=open + gh_issue_ref=None, treat as
        # CREATE so the next fire retries instead of silently no-op'ing.
        # First surfaced 2026-05-26 during P1 Mode A soak when the
        # pyjwt[crypto] dep gap blocked GH creates for one fire.
        if not row["gh_issue_ref"]:
            return DedupAction(kind=_DedupActionKind.CREATE)
        return DedupAction(kind=_DedupActionKind.COMMENT, gh_issue_ref=row["gh_issue_ref"])
    # state == 'closed'
    if row["closed_at"]:
        closed_at = dt.datetime.fromisoformat(row["closed_at"])
        if _now() - closed_at <= REOPEN_WINDOW:
            return DedupAction(kind=_DedupActionKind.REOPEN, gh_issue_ref=row["gh_issue_ref"])
    # closed but out-of-window → create fresh
    return DedupAction(kind=_DedupActionKind.CREATE)


def record(
    db: StateDB,
    dedup_key: str,
    *,
    gh_issue_ref: str | None = None,
    state: str = "open",
    closed_at: dt.datetime | None = None,
    mode: str = "A",
    cluster: str = "dev",
    severity: str = "medium",
    payload_json: str = "{}",
) -> None:
    """Upsert a finding. Used by mode runners after creating/updating a GH issue."""
    now_iso = _now().isoformat()
    closed_iso = closed_at.isoformat() if closed_at else None
    db.execute(
        """
        INSERT INTO findings (
            dedup_key, gh_issue_ref, state, created_at, last_seen_at, closed_at,
            mode, cluster, severity, payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(dedup_key) DO UPDATE SET
            gh_issue_ref=excluded.gh_issue_ref,
            state=excluded.state,
            last_seen_at=excluded.last_seen_at,
            closed_at=excluded.closed_at,
            severity=excluded.severity,
            payload_json=excluded.payload_json
        """,
        (
            dedup_key, gh_issue_ref, state, now_iso, now_iso, closed_iso,
            mode, cluster, severity, payload_json,
        ),
    )
