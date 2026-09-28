"""The key -> cluster map (clusters.py) and the two GitHub surfaces that use it.

The label on a finding or a digest-summary issue is the cluster behind the key,
as kube-infra names it. At the MS-A2 cutover each key moves cluster, one at a
time, so every label site must read the map and none may build a name itself.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from cluster_agent import clusters
from cluster_agent.modes import summary_issue as si


def test_the_map_names_the_clusters_the_kubeconfigs_point_at():
    # A tripwire, changed on purpose in the same cycle as a key's kubeconfig
    # (scripts/render-cluster-agent-kubeconfigs.sh). See clusters.py.
    assert clusters.CLUSTER_NAMES == {"dev": "kub-dev", "prd": "msa2-prd"}
    assert clusters.PREVIOUS_NAMES == {"dev": (), "prd": ("kub-prd",)}


def test_a_key_without_a_cluster_is_its_own_name():
    # `nas` / `global` are Finding.cluster values with no kubeconfig behind
    # them. They used to become `kub-nas` / `kub-global`, which name nothing.
    assert clusters.cluster_name("nas") == "nas"
    assert clusters.all_names("global") == ("global",)


def test_all_names_puts_the_current_name_first(monkeypatch):
    monkeypatch.setitem(clusters.CLUSTER_NAMES, "prd", "new-prd")
    monkeypatch.setitem(clusters.PREVIOUS_NAMES, "prd", ("old-prd",))
    assert clusters.all_names("prd") == ("new-prd", "old-prd")


def test_finding_label_is_the_mapped_cluster(tmp_path, monkeypatch):
    """Positive control for the map: a sentinel name must reach the label, and
    no `kub-` name may be synthesised beside it."""
    from cluster_agent import dispatch as d
    from cluster_agent.schema import Evidence, Finding
    from cluster_agent.state import db as db_mod
    from cluster_agent.state.dedup import DedupAction, _DedupActionKind

    monkeypatch.setitem(clusters.CLUSTER_NAMES, "prd", "sentinel-prd")
    gh = MagicMock(return_value={"number": 1})
    monkeypatch.setattr(d, "post_annotation", MagicMock(return_value="1"))
    monkeypatch.setattr(d, "gh_issue_create", gh)
    monkeypatch.setenv("FINDINGS_REPO", "o/r")
    monkeypatch.delenv("FINDINGS_MIN_SEVERITY", raising=False)
    finding = Finding(
        id="01JK3R8Q9M01234567890123XY", mode="A", cluster="prd", severity="low",
        title="t", summary="s", evidence=[Evidence(type="alert", ref="x")],
        root_cause_hypothesis=None, confidence=0.5, recommended_action="a",
        runbook_ref=None, auto_action=None, dedup_key="alert:X:y:prd",
    )

    d.dispatch(finding, DedupAction(kind=_DedupActionKind.CREATE),
               db=db_mod.StateDB(tmp_path / "state.db"))

    labels = gh.call_args.kwargs["labels"]
    assert "sentinel-prd" in labels
    assert not [lb for lb in labels if lb.startswith("kub-")]


def test_summary_issue_label_is_the_mapped_cluster(monkeypatch):
    monkeypatch.setitem(clusters.CLUSTER_NAMES, "dev", "sentinel-dev")
    create = MagicMock(return_value={"number": 5})
    monkeypatch.setattr(si, "gh_issue_create", create)
    monkeypatch.setattr(si, "gh_issue_list", MagicMock(return_value=[]))

    si.emit_summary_issue(repo="o/r", cluster="dev", title="t", body="b")

    assert create.call_args.kwargs["labels"] == ["digest-summary", "sentinel-dev", "mode-A"]


@pytest.fixture
def moved_prd(monkeypatch):
    """prd has just moved cluster: new name, the old one kept."""
    monkeypatch.setitem(clusters.CLUSTER_NAMES, "prd", "new-prd")
    monkeypatch.setitem(clusters.PREVIOUS_NAMES, "prd", ("old-prd",))


def test_first_run_after_a_move_closes_the_summary_filed_under_the_old_name(
    monkeypatch, moved_prd,
):
    open_issues = {
        "new-prd": [],
        "old-prd": [{"number": 11, "title": "prd digest 2026-09-29"}],
    }
    listed: list[list[str]] = []

    def fake_list(repo, *, labels=None, state="open", per_page=30):
        listed.append(labels)
        return open_issues[labels[1]]

    close = MagicMock()
    monkeypatch.setattr(si, "gh_issue_list", fake_list)
    monkeypatch.setattr(si, "gh_issue_close", close)

    n = si._close_previous_summaries(repo="o/r", cluster="prd", today_iso="2026-09-30")

    assert listed == [["digest-summary", "new-prd"], ["digest-summary", "old-prd"]]
    assert n == 1
    assert close.call_args.args[:2] == ("o/r", 11)


def test_an_issue_carrying_both_names_is_closed_once(monkeypatch, moved_prd):
    both = {"number": 12, "title": "prd digest 2026-09-29"}
    monkeypatch.setattr(si, "gh_issue_list",
                        lambda repo, *, labels=None, state="open", per_page=30: [both])
    close = MagicMock()
    monkeypatch.setattr(si, "gh_issue_close", close)

    assert si._close_previous_summaries(repo="o/r", cluster="prd", today_iso="2026-09-30") == 1
    assert close.call_count == 1


def test_a_failed_list_under_one_name_does_not_skip_the_other(monkeypatch, moved_prd):
    def flaky_list(repo, *, labels=None, state="open", per_page=30):
        if labels[1] == "new-prd":
            raise RuntimeError("boom")
        return [{"number": 13, "title": "prd digest 2026-09-29"}]

    close = MagicMock()
    monkeypatch.setattr(si, "gh_issue_list", flaky_list)
    monkeypatch.setattr(si, "gh_issue_close", close)

    assert si._close_previous_summaries(repo="o/r", cluster="prd", today_iso="2026-09-30") == 1
    assert close.call_args.args[:2] == ("o/r", 13)


def test_todays_summary_is_never_closed(monkeypatch, moved_prd):
    monkeypatch.setattr(si, "gh_issue_list",
                        lambda repo, *, labels=None, state="open", per_page=30:
                        [{"number": 14, "title": "prd digest 2026-09-30"}])
    close = MagicMock()
    monkeypatch.setattr(si, "gh_issue_close", close)

    assert si._close_previous_summaries(repo="o/r", cluster="prd", today_iso="2026-09-30") == 0
    assert not close.called
