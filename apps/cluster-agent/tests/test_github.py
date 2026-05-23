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
