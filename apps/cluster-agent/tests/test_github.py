"""GitHub App tool — JWT → installation access token → REST calls."""
import respx
import httpx

from cluster_agent.tools.github import (
    _build_app_jwt,
    gh_get_installation_token,
    gh_list_prs,
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


@respx.mock
def test_gh_list_prs_returns_array(monkeypatch):
    """gh_list_prs hits /repos/<owner>/<repo>/pulls with the installation token."""
    monkeypatch.setenv("CLUSTER_AGENT_GH_APP_ID", "12345")
    monkeypatch.setenv("CLUSTER_AGENT_GH_APP_PRIVATE_KEY", "LS0tLS1pZ25vcmVk")
    monkeypatch.setenv("CLUSTER_AGENT_GH_APP_INSTALLATION_ID", "67890")

    import cluster_agent.tools.github as gh
    # Short-circuit the JWT + installation-token exchange
    monkeypatch.setattr(gh, "_build_app_jwt", lambda: "fake.jwt")
    monkeypatch.setattr(gh, "gh_get_installation_token", lambda: "ghs_faketoken")

    url = "https://api.github.com/repos/guntars-rakitko/kube-infra/pulls"
    respx.get(url).mock(return_value=httpx.Response(200, json=[
        {"number": 1, "title": "test pr", "state": "open"},
    ]))

    prs = gh_list_prs("guntars-rakitko/kube-infra")
    assert len(prs) == 1
    assert prs[0]["title"] == "test pr"


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


def test_write_allowlist_includes_graduation_repos():
    """The 2026-07-06 findings-graduation must have the ops repo + the
    renamed digest repo on the write-allowlist (and keep the transitional
    sandbox entry). `_assert_write_allowed` returns None (no raise) for
    each."""
    from cluster_agent.tools.github import _assert_write_allowed

    for repo in (
        "guntars-rakitko/kube-infra",
        "guntars-rakitko/cluster-agent-digest",
        "guntars-rakitko/cluster-agent-sandbox",
    ):
        _assert_write_allowed(repo)   # must not raise


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
