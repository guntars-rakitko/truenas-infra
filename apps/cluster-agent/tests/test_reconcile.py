"""Tests for `_reconcile_finding_states` (2026-07-16 fix).

The digest runner must sync state.db against real GitHub issue state
before building the LLM's "already-open dedup keys" list. Without it,
operator-closed / retired finding issues stay state='open' forever and
the LLM keeps suppressing the chronic condition as "already tracked"
(root cause of the 2026-07 "digest cites closed issues as open" gap).
"""
import datetime as dt

from cluster_agent.modes import daily_digest
from cluster_agent.state.db import StateDB
from cluster_agent.state.dedup import record, lookup, DedupAction


def _seed(sdb, key, ref, *, last_seen_days_ago=40, cluster="dev"):
    """Seed an OPEN finding whose last_seen is backdated, so the
    last_seen fallback close date lands outside the 7d REOPEN_WINDOW."""
    record(sdb, key, gh_issue_ref=ref, state="open", cluster=cluster)
    old = (dt.datetime.now(dt.timezone.utc)
           - dt.timedelta(days=last_seen_days_ago)).isoformat()
    sdb.execute("UPDATE findings SET last_seen_at=? WHERE dedup_key=?", (old, key))


def test_operator_closed_issue_is_marked_closed(tmp_path, monkeypatch):
    """An issue the operator closed on GitHub → state.db reconciled to
    closed, and the key drops out of the LLM's open-dedup list."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:TrivyClusterWideCriticalCVEStorm:trivy-operator:dev",
          "guntars-rakitko/kube-infra#100")

    def fake_list(repo, *, labels=None, state="open", per_page=30):
        if state == "closed":
            return [{"number": 100, "state": "closed",
                     "closed_at": "2026-07-14T00:00:00Z"}]
        return []
    monkeypatch.setattr(daily_digest, "gh_issue_list", fake_list)

    n = daily_digest._reconcile_finding_states(sdb, "guntars-rakitko/kube-infra")
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

    def boom(repo, *, labels=None, state="open", per_page=30):
        raise RuntimeError("301 Moved Permanently — repo renamed")
    monkeypatch.setattr(daily_digest, "gh_issue_list", boom)

    n = daily_digest._reconcile_finding_states(sdb, "guntars-rakitko/kube-infra")
    assert n == 1
    assert daily_digest._load_open_dedup_keys(sdb, "prd") == []
    # last_seen 48d ago → outside REOPEN_WINDOW → CREATE a fresh issue
    assert lookup(sdb, "alert:TrivyClusterWideCriticalCVEStorm:trivy-operator:prd") \
        == DedupAction.create


def test_still_open_issue_is_left_open(tmp_path, monkeypatch):
    """A genuinely-open finding issue stays open — it IS still tracked,
    so the LLM should still dedup against it."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:Foo:x:dev", "guntars-rakitko/kube-infra#200")

    def fake_list(repo, *, labels=None, state="open", per_page=30):
        if state == "open":
            return [{"number": 200, "state": "open", "closed_at": None}]
        return []
    monkeypatch.setattr(daily_digest, "gh_issue_list", fake_list)

    assert daily_digest._reconcile_finding_states(sdb, "guntars-rakitko/kube-infra") == 0
    assert "alert:Foo:x:dev" in daily_digest._load_open_dedup_keys(sdb, "dev")


def test_active_repo_transient_error_does_not_false_close(tmp_path, monkeypatch):
    """A transient GitHub error on the ACTIVE findings repo must NOT
    false-close live findings (fail-safe — a GH 502 shouldn't cause a
    burst of duplicate re-files next run)."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:Foo:x:dev", "guntars-rakitko/kube-infra#300")

    def boom(repo, *, labels=None, state="open", per_page=30):
        raise RuntimeError("502 Bad Gateway")
    monkeypatch.setattr(daily_digest, "gh_issue_list", boom)

    assert daily_digest._reconcile_finding_states(sdb, "guntars-rakitko/kube-infra") == 0
    assert "alert:Foo:x:dev" in daily_digest._load_open_dedup_keys(sdb, "dev")


def test_missing_issue_in_reachable_repo_is_closed(tmp_path, monkeypatch):
    """An open state.db record whose issue is absent from a reachable
    repo (deleted, or paged past the 100-issue window) is treated as
    closed — it isn't an open tracker either way."""
    sdb = StateDB(tmp_path / "state.db")
    _seed(sdb, "alert:Old:x:dev", "guntars-rakitko/kube-infra#5",
          last_seen_days_ago=60)

    monkeypatch.setattr(daily_digest, "gh_issue_list",
                        lambda repo, *, labels=None, state="open", per_page=30: [])

    assert daily_digest._reconcile_finding_states(sdb, "guntars-rakitko/kube-infra") == 1
    assert lookup(sdb, "alert:Old:x:dev") == DedupAction.create


def test_no_open_findings_makes_no_gh_calls(tmp_path, monkeypatch):
    """Cheap fast-path: nothing open → no GitHub calls at all."""
    sdb = StateDB(tmp_path / "state.db")
    calls: list[str] = []
    monkeypatch.setattr(daily_digest, "gh_issue_list",
                        lambda *a, **k: calls.append("x") or [])
    assert daily_digest._reconcile_finding_states(sdb, "guntars-rakitko/kube-infra") == 0
    assert calls == []
