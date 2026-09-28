"""Grafana annotation API client: the NodePort path with a service-account token.

kube-infra#1366: the agent posts to the `grafana-nas` NodePort (clusters.py
GRAFANA_URLS) with `Authorization: Bearer <GRAFANA_SA_TOKEN_<KEY>>`, and never
with `X-WEBAUTH-*` headers, the path that let it act as any Grafana user.
"""
import json

import httpx
import pytest
import respx

from cluster_agent import clusters
from cluster_agent.tools import k8s_proxy
from cluster_agent.tools.grafana import post_annotation


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setenv("GRAFANA_SA_TOKEN_DEV", "glsa_dev_test")
    monkeypatch.setenv("GRAFANA_SA_TOKEN_PRD", "glsa_prd_test")


@respx.mock
@pytest.mark.parametrize("key,base,token", [
    ("dev", "http://10.10.5.12:30030", "glsa_dev_test"),
    ("prd", "http://10.10.5.11:30030", "glsa_prd_test"),
])
def test_post_annotation_goes_to_the_keys_grafana_with_its_token(tokens, key, base, token):
    route = respx.post(f"{base}/api/annotations").mock(
        return_value=httpx.Response(200, json={"id": 12345, "message": "Annotation added"})
    )

    ann_id = post_annotation(
        cluster=key,
        text="cluster-agent Mode A: PodCrashLooping in pocket-id",
        tags=["cluster-agent", "mode:A", "severity:medium"],
        time_ms=1700000000000,
    )

    assert ann_id == "12345"
    assert route.call_count == 1
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {token}"
    # The #1366 hole: no auth.proxy header may ever leave this client.
    assert not [h for h in req.headers if h.lower().startswith("x-webauth")]
    body = json.loads(req.content)
    assert body == {
        "time": 1700000000000,
        "tags": ["cluster-agent", "mode:A", "severity:medium"],
        "text": "cluster-agent Mode A: PodCrashLooping in pocket-id",
    }


@respx.mock
def test_time_end_is_sent_when_given(tokens):
    route = respx.post("http://10.10.5.11:30030/api/annotations").mock(
        return_value=httpx.Response(200, json={"id": 7})
    )
    post_annotation(cluster="prd", text="t", tags=[], time_ms=1, time_end_ms=2)
    assert json.loads(route.calls.last.request.content)["timeEnd"] == 2


@respx.mock
def test_the_token_is_stripped(monkeypatch):
    """`doppler secrets set` fed by a pipe can keep a trailing newline, which
    would make an invalid Authorization header."""
    monkeypatch.setenv("GRAFANA_SA_TOKEN_DEV", "  glsa_dev_test\n")
    route = respx.post("http://10.10.5.12:30030/api/annotations").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    post_annotation(cluster="dev", text="t", tags=[], time_ms=0)
    assert route.calls.last.request.headers["Authorization"] == "Bearer glsa_dev_test"


@pytest.mark.parametrize("value", [None, "", "   \n"])
def test_a_missing_token_raises_naming_the_variable_only(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("GRAFANA_SA_TOKEN_PRD", raising=False)
    else:
        monkeypatch.setenv("GRAFANA_SA_TOKEN_PRD", value)
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r".*").mock(return_value=httpx.Response(200, json={"id": 1}))
        with pytest.raises(RuntimeError, match="GRAFANA_SA_TOKEN_PRD is not set"):
            post_annotation(cluster="prd", text="t", tags=[], time_ms=0)
        assert route.call_count == 0  # nothing is sent without a token


@respx.mock
def test_a_rejected_token_raises(tokens):
    """401 is what a from-zero rebuild gives (the service account is gone with
    Grafana's database). It must raise, so dispatch counts it."""
    respx.post("http://10.10.5.11:30030/api/annotations").mock(
        return_value=httpx.Response(401, json={"message": "invalid API key"})
    )
    with pytest.raises(httpx.HTTPStatusError):
        post_annotation(cluster="prd", text="t", tags=[], time_ms=0)


def test_post_annotation_unknown_cluster_raises(tokens):
    """Unknown key → ValueError so callers can't silently miss the wrong Grafana."""
    with pytest.raises(ValueError, match="unknown cluster"):
        post_annotation(cluster="stg", text="x", tags=[], time_ms=0)


@respx.mock
def test_the_url_follows_the_map(tokens, monkeypatch):
    """Positive control for GRAFANA_URLS: a sentinel address must be the one
    posted to (the map moves with a key's kubeconfig, clusters.py)."""
    monkeypatch.setitem(clusters.GRAFANA_URLS, "dev", "http://192.0.2.7:30030/")
    route = respx.post("http://192.0.2.7:30030/api/annotations").mock(
        return_value=httpx.Response(200, json={"id": 3})
    )
    assert post_annotation(cluster="dev", text="t", tags=[], time_ms=0) == "3"
    assert route.call_count == 1


def test_the_apiserver_proxy_client_has_no_write_path():
    """kube-infra#1366 left the SA `get` only on services/proxy, so a POST
    helper would be a 403 waiting to happen. Grafana goes through this
    module's own client."""
    assert not hasattr(k8s_proxy, "proxy_post")
