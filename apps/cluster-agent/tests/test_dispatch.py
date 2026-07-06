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


def test_dispatch_grafana_failure_increments_metric_but_does_not_break_pipeline(tmp_path, monkeypatch):
    """Grafana annotation is best-effort — if it raises, dispatch still
    writes GH + SQLite, AND the dispatch_errors_total counter ticks so
    Prometheus can alert on sustained failures."""
    from cluster_agent import dispatch as d
    from cluster_agent.state import db as db_mod
    from cluster_agent.emit.metrics import DISPATCH_ERRORS

    sdb = db_mod.StateDB(tmp_path / "state.db")

    def boom(**_):
        raise RuntimeError("apiserver-proxy returned 401")

    gh = MagicMock(return_value={"number": 11, "html_url": "https://example/issues/11"})
    monkeypatch.setattr(d, "post_annotation", boom)
    monkeypatch.setattr(d, "gh_issue_create", gh)
    monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")

    before = DISPATCH_ERRORS.labels(surface="grafana_annotation")._value.get()
    result = dispatch(_make_finding(), DedupAction(kind=_DedupActionKind.CREATE), db=sdb)
    after = DISPATCH_ERRORS.labels(surface="grafana_annotation")._value.get()

    # GH still got the finding even though Grafana failed
    assert result.gh_issue_ref == "guntars-rakitko/cluster-agent-sandbox#11"
    # Grafana annotation didn't return an ID
    assert result.grafana_annotation_id is None
    # And the failure counter ticked exactly once
    assert after - before == 1


# ── 2026-07-06 graduation: findings → kube-infra, routing + labels ────────

def test_dispatch_create_routes_to_findings_repo_with_review_labels(tmp_path, monkeypatch):
    """Findings file into FINDINGS_REPO (the ops repo) carrying the
    `cluster-agent` provenance + `needs-review` triage labels."""
    from cluster_agent import dispatch as d
    from cluster_agent.state import db as db_mod

    sdb = db_mod.StateDB(tmp_path / "state.db")
    gr = MagicMock(return_value="1")
    gh = MagicMock(return_value={"number": 42})
    monkeypatch.setattr(d, "post_annotation", gr)
    monkeypatch.setattr(d, "gh_issue_create", gh)
    monkeypatch.setenv("FINDINGS_REPO", "guntars-rakitko/kube-infra")
    monkeypatch.delenv("FINDINGS_MIN_SEVERITY", raising=False)

    result = dispatch(_make_finding(), DedupAction(kind=_DedupActionKind.CREATE), db=sdb)

    assert result.gh_issue_ref == "guntars-rakitko/kube-infra#42"
    assert gh.call_args.args[0] == "guntars-rakitko/kube-infra"
    labels = gh.call_args.kwargs["labels"]
    assert "cluster-agent" in labels
    assert "needs-review" in labels
    assert "severity-medium" in labels
    assert "kub-dev" in labels


def test_dispatch_comment_targets_ref_repo_not_env_repo(tmp_path, monkeypatch):
    """Migration safety: a finding created in the OLD repo (its stored
    repo-qualified gh_issue_ref) keeps being commented on THERE, even after
    FINDINGS_REPO has been repointed at kube-infra. Commenting on the env
    repo with the old number would hit the wrong kube-infra issue."""
    from cluster_agent import dispatch as d
    from cluster_agent.state import db as db_mod

    sdb = db_mod.StateDB(tmp_path / "state.db")
    gh_comment = MagicMock(return_value={"id": 1})
    monkeypatch.setattr(d, "post_annotation", MagicMock(return_value="1"))
    monkeypatch.setattr(d, "gh_issue_create", MagicMock())
    monkeypatch.setattr(d, "gh_issue_comment", gh_comment)
    monkeypatch.setenv("FINDINGS_REPO", "guntars-rakitko/kube-infra")   # NEW target

    action = DedupAction(
        kind=_DedupActionKind.COMMENT,
        gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#5",         # OLD repo
    )
    dispatch(_make_finding(), action, db=sdb)

    assert gh_comment.called
    assert gh_comment.call_args.args[0] == "guntars-rakitko/cluster-agent-sandbox"  # NOT kube-infra
    assert gh_comment.call_args.args[1] == 5
    body = gh_comment.call_args.kwargs.get("body") or gh_comment.call_args.args[2]
    assert "Still firing as of" in body


def test_dispatch_findings_min_severity_floor_suppresses_below(tmp_path, monkeypatch):
    """FINDINGS_MIN_SEVERITY (inert escape hatch) suppresses the GH issue
    for a below-floor finding but still fires Grafana + records state."""
    from cluster_agent import dispatch as d
    from cluster_agent.state import db as db_mod

    sdb = db_mod.StateDB(tmp_path / "state.db")
    gr = MagicMock(return_value="9")
    gh = MagicMock()   # MUST NOT be called (medium < high floor)
    monkeypatch.setattr(d, "post_annotation", gr)
    monkeypatch.setattr(d, "gh_issue_create", gh)
    monkeypatch.setenv("FINDINGS_REPO", "guntars-rakitko/kube-infra")
    monkeypatch.setenv("FINDINGS_MIN_SEVERITY", "high")

    result = dispatch(_make_finding(), DedupAction(kind=_DedupActionKind.CREATE), db=sdb)

    assert gh.called is False                     # no issue filed
    assert result.grafana_annotation_id == "9"    # Grafana still fired
    assert result.gh_issue_ref is None
    row = sdb.fetchone("SELECT state FROM findings WHERE dedup_key=?",
                       (_make_finding().dedup_key,))
    assert row["state"] == "open"                 # state still recorded
