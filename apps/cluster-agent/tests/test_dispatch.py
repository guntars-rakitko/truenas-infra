"""Dispatch — write Finding to all configured surfaces."""
from __future__ import annotations
import json
import datetime as dt
from unittest.mock import MagicMock

import pytest

from cluster_agent.dispatch import dispatch
from cluster_agent.state.dedup import DedupAction, _DedupActionKind
from cluster_agent.schema import Finding, Evidence


def _make_finding() -> Finding:
    return Finding(
        id="01JK3R8Q9M01234567890123XY",
        mode="A", cluster="dev", severity="medium",
        title="Test finding",
        summary="Test summary",
        evidence=[Evidence(type="alert", ref="Alertmanager/X@now")],
        root_cause_hypothesis=None,
        confidence=0.6,
        recommended_action="do thing",
        runbook_ref=None,
        auto_action=None,
        dedup_key="alert:X:y:dev",
    )


def test_dispatch_create_writes_all_three_surfaces(tmp_path, monkeypatch):
    """On action=create, dispatch writes: SQLite + Grafana + new GH issue."""
    from cluster_agent import dispatch as d
    from cluster_agent.state import db as db_mod

    # Real SQLite (in-memory equivalent via tmp_path)
    sdb_path = tmp_path / "state.db"
    monkeypatch.setenv("STATE_DB_PATH", str(sdb_path))
    sdb = db_mod.StateDB(sdb_path)

    # Stub Grafana + GH
    gr = MagicMock(return_value="42")
    gh = MagicMock(return_value={"number": 7, "html_url": "https://github.com/foo/bar/issues/7"})
    monkeypatch.setattr(d, "post_annotation", gr)
    monkeypatch.setattr(d, "gh_issue_create", gh)
    monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")

    finding = _make_finding()
    action = DedupAction(kind=_DedupActionKind.CREATE)
    result = dispatch(finding, action, db=sdb)

    assert result.gh_issue_ref == "guntars-rakitko/cluster-agent-sandbox#7"
    assert result.grafana_annotation_id == "42"
    assert gr.called
    assert gh.called
    # SQLite has the finding
    row = sdb.fetchone("SELECT dedup_key, gh_issue_ref, state FROM findings WHERE dedup_key=?",
                       (finding.dedup_key,))
    assert row["dedup_key"] == "alert:X:y:dev"
    assert row["gh_issue_ref"] == "guntars-rakitko/cluster-agent-sandbox#7"
    assert row["state"] == "open"


def test_dispatch_comment_does_not_create_new_issue(tmp_path, monkeypatch):
    """On action=comment, dispatch posts a comment on the existing issue
    (NOT a new one) and writes Grafana annotation + updates SQLite."""
    from cluster_agent import dispatch as d
    from cluster_agent.state import db as db_mod

    sdb_path = tmp_path / "state.db"
    sdb = db_mod.StateDB(sdb_path)

    gr = MagicMock(return_value="43")
    gh_create = MagicMock()    # MUST NOT be called
    gh_comment = MagicMock(return_value={"id": 999})
    monkeypatch.setattr(d, "post_annotation", gr)
    monkeypatch.setattr(d, "gh_issue_create", gh_create)
    monkeypatch.setattr(d, "gh_issue_comment", gh_comment)
    monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")

    finding = _make_finding()
    action = DedupAction(kind=_DedupActionKind.COMMENT, gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#5")
    result = dispatch(finding, action, db=sdb)

    assert gh_create.called is False
    assert gh_comment.called is True
    assert result.gh_issue_ref == "guntars-rakitko/cluster-agent-sandbox#5"
    assert result.grafana_annotation_id == "43"
