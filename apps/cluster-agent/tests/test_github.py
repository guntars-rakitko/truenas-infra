"""GitHub App tool — JWT → installation access token → REST calls."""
import respx
import httpx

import pytest

from cluster_agent.tools.github import (
    _build_app_jwt,
    gh_get_installation_token,
    gh_issue_get,
)


def test_jwt_has_iss_iat_exp(monkeypatch):
    """The App JWT must carry iss (App ID), iat, exp claims."""
    monkeypatch.setenv("CLUSTER_AGENT_GH_APP_ID", "12345")
    # The PEM value doesn't matter — we mock jwt.encode to capture the payload
    monkeypatch.setenv("CLUSTER_AGENT_GH_APP_PRIVATE_KEY", "LS0tLS1pZ25vcmVk")  # b64'd 'ignored'

    called = {}

    def fake_encode(payload, key, algorithm):
        called["payload"] = payload
        called["algorithm"] = algorithm
        return "fake.jwt.token"

    import cluster_agent.tools.github as gh
    monkeypatch.setattr(gh.jwt, "encode", fake_encode)

    token = _build_app_jwt()
    assert token == "fake.jwt.token"
    assert called["payload"]["iss"] == "12345"
    assert "iat" in called["payload"]
    assert "exp" in called["payload"]
    assert called["algorithm"] == "RS256"


def _fake_auth(monkeypatch):
    import cluster_agent.tools.github as gh
    # Short-circuit the JWT + installation-token exchange
    monkeypatch.setattr(gh, "_build_app_jwt", lambda: "fake.jwt")
    monkeypatch.setattr(gh, "gh_get_installation_token", lambda: "ghs_faketoken")


@respx.mock
def test_gh_issue_get_returns_the_issue(monkeypatch):
    """gh_issue_get reads /repos/<owner>/<repo>/issues/<n> with the installation token."""
    _fake_auth(monkeypatch)
    url = "https://api.github.com/repos/guntars-rakitko/kube-infra/issues/1600"
    route = respx.get(url).mock(return_value=httpx.Response(200, json={
        "number": 1600, "state": "closed", "closed_at": "2026-10-10T12:00:00Z"}))
    issue = gh_issue_get("guntars-rakitko/kube-infra", 1600)
    assert issue["state"] == "closed"
    assert route.calls.last.request.headers["Authorization"] == "Bearer ghs_faketoken"


@respx.mock
@pytest.mark.parametrize("status", [404, 410])
def test_gh_issue_get_answers_none_for_an_issue_that_does_not_exist(monkeypatch, status):
    _fake_auth(monkeypatch)
    respx.get("https://api.github.com/repos/guntars-rakitko/kube-infra/issues/5").mock(
        return_value=httpx.Response(status, json={"message": "Not Found"}))
    assert gh_issue_get("guntars-rakitko/kube-infra", 5) is None


@respx.mock
@pytest.mark.parametrize("status,readable", [(200, True), (404, False), (403, False)])
def test_gh_repo_readable(monkeypatch, status, readable):
    from cluster_agent.tools.github import gh_repo_readable
    _fake_auth(monkeypatch)
    respx.get("https://api.github.com/repos/guntars-rakitko/kube-infra").mock(
        return_value=httpx.Response(status, json={}))
    assert gh_repo_readable("guntars-rakitko/kube-infra") is readable


@respx.mock
def test_gh_issue_get_raises_on_any_other_failure(monkeypatch):
    """A 502, or the 301 of a renamed repo: the reconcile decides by repo
    whether that means closed (a retired repo) or "cannot tell"."""
    _fake_auth(monkeypatch)
    respx.get("https://api.github.com/repos/guntars-rakitko/kube-infra/issues/6").mock(
        return_value=httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(httpx.HTTPStatusError):
        gh_issue_get("guntars-rakitko/kube-infra", 6)


def test_gh_issue_create_refuses_non_allowlisted_repo(monkeypatch):
    """Writes to a non-allowlisted repo must raise PermissionError BEFORE
    any HTTP call. Belt-and-suspenders against env-typo / LLM-controlled
    repo escape (P4 security audit follow-up, 2026-05-27)."""
    import cluster_agent.tools.github as gh

    # Patch _gh_headers so we don't need real auth; the assertion should
    # fire before any header lookup.
    monkeypatch.setattr(gh, "_gh_headers", lambda: {})

    # NB: use a repo that is NOT on the write-allowlist. kube-infra was
    # added to the allowlist in the 2026-07-06 findings-graduation, so it
    # can no longer serve as the "forbidden" example.
    try:
        gh.gh_issue_create("guntars-rakitko/giks", "test", "body")
    except PermissionError as e:
        assert "guntars-rakitko/giks" in str(e)
        assert "_WRITE_ALLOWED_REPOS" in str(e)
    else:
        raise AssertionError("expected PermissionError on non-allowlisted repo")


def test_write_allowlist_is_exactly_the_two_destinations():
    """The ops repo (findings) and the digest repo (summaries), and nothing
    else: the transitional `cluster-agent-sandbox` entry went on 2026-10-10,
    with no state.db row naming it."""
    from cluster_agent.tools.github import _WRITE_ALLOWED_REPOS, _assert_write_allowed

    assert _WRITE_ALLOWED_REPOS == {"guntars-rakitko/kube-infra", "guntars-rakitko/cluster-agent-digest"}
    for repo in _WRITE_ALLOWED_REPOS:
        _assert_write_allowed(repo)   # must not raise
    with pytest.raises(PermissionError):
        _assert_write_allowed("guntars-rakitko/cluster-agent-sandbox")


def test_gh_issue_comment_refuses_non_allowlisted_repo(monkeypatch):
    """gh_issue_comment must also enforce the allowlist (used by
    _close_previous_summaries comment + dispatch comment paths)."""
    import cluster_agent.tools.github as gh
    monkeypatch.setattr(gh, "_gh_headers", lambda: {})

    try:
        gh.gh_issue_comment("guntars-rakitko/giks", 1, "body")
    except PermissionError as e:
        assert "guntars-rakitko/giks" in str(e)
    else:
        raise AssertionError("expected PermissionError on non-allowlisted repo")


def test_gh_issue_close_refuses_non_allowlisted_repo(monkeypatch):
    """gh_issue_close must also enforce the allowlist (close ops are
    no less destructive than create)."""
    import cluster_agent.tools.github as gh
    monkeypatch.setattr(gh, "_gh_headers", lambda: {})

    try:
        gh.gh_issue_close("guntars-rakitko/truenas-infra", 1)
    except PermissionError as e:
        assert "truenas-infra" in str(e)
    else:
        raise AssertionError("expected PermissionError on non-allowlisted repo")
