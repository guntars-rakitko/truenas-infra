"""Loki, Prometheus, Alertmanager tools — via K8s apiserver proxy.

All three tools now use k8s_proxy.proxy_get() rather than direct basic-auth
HTTP hits. Tests mock the apiserver proxy URLs (not the *.w1.lv hostnames)
and inject a minimal fake kubeconfig via the KUBECONFIG_DEV env var.
"""
import base64

import httpx
import respx
import yaml

from cluster_agent.tools.loki import loki_query
from cluster_agent.tools.prometheus import prometheus_query, prometheus_query_range
from cluster_agent.tools.alertmanager import alertmanager_alerts, alertmanager_history


def _fake_kubeconfig() -> str:
    """Return a base64-encoded minimal kubeconfig for tests.

    No CA data — k8s_proxy._tls_verify() returns False (skip verify),
    which is fine in tests.
    """
    kc = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": "dev", "cluster": {
            "server": "https://test-api:6443",
        }}],
        "users": [{"name": "agent", "user": {"token": "fake-token"}}],
        "contexts": [{"name": "dev", "context": {"cluster": "dev", "user": "agent"}}],
        "current-context": "dev",
    }
    return base64.b64encode(yaml.dump(kc).encode()).decode()


@respx.mock
def test_loki_query_via_apiserver_proxy(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/loki:3100/proxy/loki/api/v1/query_range"
    )
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [{"stream": {"app": "x"}, "values": []}],
        },
    }))
    result = loki_query("dev", '{app="x"}', limit=10)
    assert result["status"] == "success"
    assert len(result["data"]["result"]) == 1


@respx.mock
def test_prometheus_query_via_apiserver_proxy(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query"
    )
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": {"resultType": "vector", "result": []},
    }))
    result = prometheus_query("dev", "up")
    assert result["status"] == "success"


@respx.mock
def test_prometheus_query_range_via_apiserver_proxy(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query_range"
    )
    respx.get(url).mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": {"resultType": "matrix", "result": []},
    }))
    result = prometheus_query_range(
        "dev", "up", start="2026-05-25T00:00:00Z", end="2026-05-25T01:00:00Z"
    )
    assert result["status"] == "success"


@respx.mock
def test_alertmanager_lists_via_apiserver_proxy(monkeypatch):
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/alerts"
    )
    respx.get(url).mock(return_value=httpx.Response(200, json=[
        {"labels": {"alertname": "Watchdog"}, "status": {"state": "active"}},
    ]))
    alerts = alertmanager_alerts("dev")
    assert len(alerts) == 1
    assert alerts[0]["labels"]["alertname"] == "Watchdog"


@respx.mock
def test_alertmanager_history_excludes_info_inhibitor_but_keeps_watchdog(monkeypatch):
    """The 24h ALERTS pull must drop the InfoInhibitor meta-alert at the
    source (kube-infra #1286) and must NOT drop Watchdog.

    InfoInhibitor restates "an info alert exists in this namespace" and
    flaps with pending info alerts; the digest filed it as a finding.
    Watchdog must stay: it keeps the history non-empty, and
    daily_digest.run_async returns before log mining + summary on an
    empty history. The assertion reads the query off the wire (the
    respx route), not off a constant, so it fails if the call site stops
    using the filtered query.
    """
    monkeypatch.setenv("KUBECONFIG_DEV", _fake_kubeconfig())
    url = (
        "https://test-api:6443/api/v1/namespaces/monitoring"
        "/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query_range"
    )
    route = respx.get(url).mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": {"resultType": "matrix", "result": []},
    }))
    alertmanager_history("dev", since_hours=24)

    assert route.call_count == 1
    sent = route.calls.last.request.url.params["query"]
    assert sent.startswith("ALERTS{"), sent
    assert 'alertstate="firing"' in sent, sent
    assert 'alertname!="InfoInhibitor"' in sent, sent
    assert "Watchdog" not in sent, (
        "Watchdog must not be excluded at the source: an empty history "
        "makes run_async skip log mining and the summary"
    )


def test_the_cluster_ca_is_trusted_from_memory_never_a_temp_file(monkeypatch):
    """Until 2026-10-10 each proxied query wrote the CA to a new temp file
    and never removed it."""
    import datetime as dt
    import ssl
    import tempfile
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    from cluster_agent.tools import k8s_proxy

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "kubernetes-test-ca")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)

    def no_temp_files(*a, **kw):
        raise AssertionError("the CA must not be written to a temp file")
    monkeypatch.setattr(tempfile, "NamedTemporaryFile", no_temp_files)

    kc = {"clusters": [{"cluster": {"certificate-authority-data": base64.b64encode(pem).decode()}}]}
    ctx = k8s_proxy._tls_verify(kc)
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()] == [
        {"commonName": "kubernetes-test-ca"}]
