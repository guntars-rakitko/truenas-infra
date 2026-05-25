"""Dedup logic — the cluster-agent's anti-spam mechanism for GH issues.

Per spec § 4.4: before creating a new GH issue, the agent looks up the
finding's dedup_key in state.db:
  - open issue exists  → action=comment on existing
  - closed <7d ago     → action=reopen
  - otherwise          → action=create new
"""
import datetime as dt
import pytest

from cluster_agent.state.db import StateDB
from cluster_agent.state.dedup import DedupAction, lookup, record


def test_new_dedup_key_returns_create(tmp_path):
    """A never-seen dedup_key → action=create."""
    db = StateDB(tmp_path / "state.db")
    action = lookup(db, "alert:Foo:pod-0:dev")
    assert action == DedupAction.create


def test_open_issue_returns_comment(tmp_path):
    """An open issue for this dedup_key → action=comment (don't reopen)."""
    db = StateDB(tmp_path / "state.db")
    record(db, "alert:Foo:pod-0:dev", gh_issue_ref="kube-infra#100", state="open")
    action = lookup(db, "alert:Foo:pod-0:dev")
    assert action == DedupAction.comment
    assert action.gh_issue_ref == "kube-infra#100"


def test_recently_closed_issue_returns_reopen(tmp_path):
    """A closed-but-within-7d issue → action=reopen."""
    db = StateDB(tmp_path / "state.db")
    record(
        db, "alert:Foo:pod-0:dev",
        gh_issue_ref="kube-infra#100",
        state="closed",
        closed_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3),
    )
    action = lookup(db, "alert:Foo:pod-0:dev")
    assert action == DedupAction.reopen
    assert action.gh_issue_ref == "kube-infra#100"


def test_long_closed_issue_returns_create(tmp_path):
    """A closed-over-7d-ago issue → action=create (fresh issue)."""
    db = StateDB(tmp_path / "state.db")
    record(
        db, "alert:Foo:pod-0:dev",
        gh_issue_ref="kube-infra#100",
        state="closed",
        closed_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10),
    )
    action = lookup(db, "alert:Foo:pod-0:dev")
    assert action == DedupAction.create
