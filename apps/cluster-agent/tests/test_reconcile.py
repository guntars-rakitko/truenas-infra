"""Tests for `_reconcile_finding_states` (2026-07-16 fix).

The digest runner must sync state.db against real GitHub issue state
before building the LLM's "already-open dedup keys" list. Without it,
operator-closed / retired finding issues stay state='open' forever and
the LLM keeps suppressing the chronic condition as "already tracked"
(root cause of the 2026-07 "digest cites closed issues as open" gap).

Since 2026-10-10 each issue is read on its own (`gh_issue_get`); the old
listing read one page of 100 per state and closed every open finding
whose issue was not on that page.
"""
import datetime as dt

import pytest

from cluster_agent.modes import daily_digest
from cluster_agent.state.db import StateDB
from cluster_agent.state.dedup import record, lookup, DedupAction

_ACTIVE = "guntars-rakitko/kube-infra"


def _seed(sdb, key, ref, *, last_seen_days_ago=40, cluster="dev"):
    """Seed an OPEN finding whose last_seen is backdated, so the
    last_seen fallback close date lands outside the 7d REOPEN_WINDOW."""
    record(sdb, key, gh_issue_ref=ref, state="open", cluster=cluster)
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(days=last_seen_days_ago)).isoformat()
    sdb.execute("UPDATE findings SET last_seen_at=? WHERE dedup_key=?", (old, key))


def _issues(table):
    """A fake gh_issue_get over {(repo, number): issue-or-None-or-Exception}."""
    calls = []

    def fake(repo, number):
        calls.append((repo, number))
        value = table[(repo, number)]
        if isinstance(value, Exception):
            raise value
        return value
    return fake, calls


def test_operator_closed_issue_is_marked_closed(tmp_path, monkeypatch):
    """An issue the operator closed on GitHub → state.db reconciled to
    closed, and the key drops out of the LLM's open-dedup list."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:TrivyClusterWideCriticalCVEStorm:trivy-operator:dev", f"{_ACTIVE}#100")
    fake, _ = _issues({(_ACTIVE, 100): {"number": 100, "state": "closed",
                                        "closed_at": "2026-07-14T00:00:00Z"}})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)

    n = daily_digest._reconcile_finding_states(sdb, _ACTIVE)
    assert n == 1
    assert daily_digest._load_open_dedup_keys(sdb, "dev") == []
    row = sdb.fetchone(
        "SELECT state, closed_at FROM findings WHERE dedup_key=?",
        ("alert:TrivyClusterWideCriticalCVEStorm:trivy-operator:dev",),
    )
    assert row["state"] == "closed"
    assert row["closed_at"] == "2026-07-14T00:00:00Z"   # real GH close date, not fallback


def test_retired_repo_unreachable_closes_with_last_seen_fallback(tmp_path, monkeypatch):
    """The pre-graduation `cluster-agent-sandbox` repo redirects/404s.
    Its stale open records are reconciled closed using last_seen_at as
    the close date → old enough that the next emit CREATEs fresh (not a
    comment on the dead sandbox issue)."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:TrivyClusterWideCriticalCVEStorm:trivy-operator:prd",
          "guntars-rakitko/cluster-agent-sandbox#49",
          last_seen_days_ago=48, cluster="prd")
    fake, _ = _issues({("guntars-rakitko/cluster-agent-sandbox", 49):
                       RuntimeError("301 Moved Permanently — repo renamed")})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)

    n = daily_digest._reconcile_finding_states(sdb, _ACTIVE)
    assert n == 1
    assert daily_digest._load_open_dedup_keys(sdb, "prd") == []
    # last_seen 48d ago → outside REOPEN_WINDOW → CREATE a fresh issue
    assert lookup(sdb, "alert:TrivyClusterWideCriticalCVEStorm:trivy-operator:prd") \
        == DedupAction.create


def test_still_open_issue_is_left_open(tmp_path, monkeypatch):
    """A genuinely-open finding issue stays open — it IS still tracked,
    so the LLM should still dedup against it."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:Foo:x:dev", f"{_ACTIVE}#200")
    fake, _ = _issues({(_ACTIVE, 200): {"number": 200, "state": "open", "closed_at": None}})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)

    assert daily_digest._reconcile_finding_states(sdb, _ACTIVE) == 0
    assert "alert:Foo:x:dev" in daily_digest._load_open_dedup_keys(sdb, "dev")


def test_an_open_issue_far_down_a_busy_repo_stays_open(tmp_path, monkeypatch):
    """The old listing read ONE page of 100: an open finding issue the page
    did not reach (kube-infra has thousands of issues and PRs) was closed in
    state.db while open on GitHub. A per-issue read has no page to miss."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:Foo:x:dev", f"{_ACTIVE}#1600")
    fake, calls = _issues({(_ACTIVE, 1600): {"number": 1600, "state": "open", "closed_at": None}})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)

    assert daily_digest._reconcile_finding_states(sdb, _ACTIVE) == 0
    assert calls == [(_ACTIVE, 1600)]
    assert "alert:Foo:x:dev" in daily_digest._load_open_dedup_keys(sdb, "dev")


def test_active_repo_transient_error_does_not_false_close(tmp_path, monkeypatch):
    """A transient GitHub error on the ACTIVE findings repo must NOT
    false-close live findings (fail-safe — a GH 502 shouldn't cause a
    burst of duplicate re-files next run)."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:Foo:x:dev", f"{_ACTIVE}#300")
    fake, _ = _issues({(_ACTIVE, 300): RuntimeError("502 Bad Gateway")})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)

    assert daily_digest._reconcile_finding_states(sdb, _ACTIVE) == 0
    assert "alert:Foo:x:dev" in daily_digest._load_open_dedup_keys(sdb, "dev")


def test_a_deleted_issue_is_closed(tmp_path, monkeypatch):
    """GitHub says the issue does not exist (gh_issue_get → None on 404/410)
    while the repo itself answers: it is not an open tracker either way."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:Old:x:dev", f"{_ACTIVE}#5", last_seen_days_ago=60)
    _seed(sdb, "alert:Old2:x:dev", f"{_ACTIVE}#6", last_seen_days_ago=60)
    fake, _ = _issues({(_ACTIVE, 5): None, (_ACTIVE, 6): None})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)
    probes: list[str] = []
    monkeypatch.setattr(daily_digest, "gh_repo_readable", lambda repo: probes.append(repo) or True)

    assert daily_digest._reconcile_finding_states(sdb, _ACTIVE) == 2
    assert lookup(sdb, "alert:Old:x:dev") == DedupAction.create
    assert probes == [_ACTIVE]          # asked once per run


@pytest.mark.parametrize("probe", ["unreadable", "error"])
def test_a_404_on_an_unreadable_active_repo_closes_nothing(tmp_path, monkeypatch, probe):
    """GitHub answers 404 for a whole private repo the App can no longer
    read. On the ACTIVE repo that must not close every live finding."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:Live:x:dev", f"{_ACTIVE}#7")
    fake, _ = _issues({(_ACTIVE, 7): None})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)

    def readable(repo):
        if probe == "error":
            raise RuntimeError("502")
        return False
    monkeypatch.setattr(daily_digest, "gh_repo_readable", readable)

    assert daily_digest._reconcile_finding_states(sdb, _ACTIVE) == 0
    assert "alert:Live:x:dev" in daily_digest._load_open_dedup_keys(sdb, "dev")


def test_alias_rows_of_one_issue_cost_one_read_and_close_together(tmp_path, monkeypatch):
    """Alias keys (modes/signals.py) share their issue's ref: one read for
    the issue, and every row on it follows its state."""
    sdb = StateDB(tmp_path / "state.db")
    for key in ("alert:A:ns:dev", "alert:B:ns:dev", "log:ratio:ns:dev"):
        _seed(sdb, key, f"{_ACTIVE}#700")
    fake, calls = _issues({(_ACTIVE, 700): {"number": 700, "state": "closed",
                                           "closed_at": "2026-10-09T08:00:00Z"}})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)

    assert daily_digest._reconcile_finding_states(sdb, _ACTIVE) == 3
    assert calls == [(_ACTIVE, 700)]
    assert daily_digest._load_open_dedup_keys(sdb, "dev") == []


def test_no_open_findings_makes_no_gh_calls(tmp_path, monkeypatch):
    """Cheap fast-path: nothing open → no GitHub calls at all."""
    sdb = StateDB(tmp_path / "state.db")
    fake, calls = _issues({})
    monkeypatch.setattr(daily_digest, "gh_issue_get", fake)
    assert daily_digest._reconcile_finding_states(sdb, _ACTIVE) == 0
    assert calls == []
