"""Tests for modules/verify.py — phase 10 (post-apply verification matrix)."""

from __future__ import annotations

from unittest.mock import MagicMock


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── check_pool ──────────────────────────────────────────────────────────────


def test_check_pool_passes_when_online() -> None:
    from truenas_infra.modules.verify import check_pool

    cli = _mk_cli([[{"name": "tank", "status": "ONLINE", "healthy": True}]])
    r = check_pool(cli, pool_name="tank")
    assert r.passed is True
    assert "ONLINE" in r.message


def test_check_pool_fails_when_missing() -> None:
    from truenas_infra.modules.verify import check_pool

    cli = _mk_cli([[]])
    r = check_pool(cli, pool_name="tank")
    assert r.passed is False


def test_check_pool_fails_when_degraded() -> None:
    from truenas_infra.modules.verify import check_pool

    cli = _mk_cli([[{"name": "tank", "status": "DEGRADED", "healthy": False}]])
    r = check_pool(cli, pool_name="tank")
    assert r.passed is False


# ─── check_service ───────────────────────────────────────────────────────────


def test_check_service_passes_when_running() -> None:
    from truenas_infra.modules.verify import check_service

    cli = _mk_cli([[{"id": 1, "service": "nfs", "state": "RUNNING", "enable": True}]])
    r = check_service(cli, service_name="nfs")
    assert r.passed is True


def test_check_service_fails_when_stopped() -> None:
    from truenas_infra.modules.verify import check_service

    cli = _mk_cli([[{"id": 1, "service": "nfs", "state": "STOPPED", "enable": False}]])
    r = check_service(cli, service_name="nfs")
    assert r.passed is False


# ─── check_app ───────────────────────────────────────────────────────────────


def test_check_app_passes_when_running() -> None:
    from truenas_infra.modules.verify import check_app

    cli = _mk_cli([[{"name": "netboot-xyz", "state": "RUNNING"}]])
    r = check_app(cli, app_name="netboot-xyz")
    assert r.passed is True


def test_check_app_fails_when_deploying() -> None:
    from truenas_infra.modules.verify import check_app

    cli = _mk_cli([[{"name": "netboot-xyz", "state": "DEPLOYING"}]])
    r = check_app(cli, app_name="netboot-xyz")
    assert r.passed is False


# ─── check_datasets ──────────────────────────────────────────────────────────


def test_check_datasets_passes_when_all_present() -> None:
    from truenas_infra.modules.verify import check_datasets

    live = [{"name": n} for n in ("tank/kube/prd", "tank/kube/dev", "tank/media")]
    cli = _mk_cli([live])
    r = check_datasets(cli, expected=("tank/kube/prd", "tank/kube/dev"))
    assert r.passed is True


def test_check_datasets_fails_when_missing() -> None:
    from truenas_infra.modules.verify import check_datasets

    cli = _mk_cli([[{"name": "tank/kube/prd"}]])
    r = check_datasets(cli, expected=("tank/kube/prd", "tank/kube/dev"))
    assert r.passed is False
    assert "tank/kube/dev" in r.message


# ─── run() orchestration ─────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        import structlog
        self.log = structlog.get_logger("test")


def test_run_returns_zero_when_all_pass(monkeypatch) -> None:
    from truenas_infra.modules import verify
    from truenas_infra.modules.verify import run

    # Stub out the network probes so tests don't touch DNS or TLS.
    monkeypatch.setattr(verify, "_dig_short",
                        lambda host, resolver: {
                            "nas.w1.lv": "10.10.5.10",
                            "mc.w1.lv": "10.10.5.20",
                            "pxe.w1.lv": "10.10.5.20",
                            "minio-prd.w1.lv": "10.10.5.20",
                            "minio-dev.w1.lv": "10.10.5.20",
                            "s3-prd.w1.lv": "10.10.10.10",
                            "s3-dev.w1.lv": "10.10.15.10",
                            "kub-prd-01.w1.lv": "10.10.5.11",
                            "kub-prd-02.w1.lv": "10.10.5.12",
                            "kub-prd-03.w1.lv": "10.10.5.13",
                            "kub-dev-01.w1.lv": "10.10.5.14",
                            "kub-dev-02.w1.lv": "10.10.5.15",
                            "kub-dev-03.w1.lv": "10.10.5.16",
                            "traefik-nas.w1.lv": "10.10.5.20",
                        }.get(host))
    monkeypatch.setattr(verify, "_tls_handshake_cert",
                        lambda host, port, timeout: {
                            "subject": "CN=*.w1.lv",
                            "issuer": "CN=R12, O=Let's Encrypt, C=US",
                            "sans": ["*.w1.lv", "w1.lv"],
                        })

    cli = _mk_cli([
        # pool
        [{"name": "tank", "status": "ONLINE", "healthy": True}],
        # datasets
        [{"name": n} for n in (
            "tank/kube/prd", "tank/kube/dev",
            "tank/media", "tank/shared/general", "tank/system",
        )],
        # nfs / cifs / ups services
        [{"id": 1, "service": "nfs", "state": "RUNNING", "enable": True}],
        [{"id": 2, "service": "cifs", "state": "RUNNING", "enable": True}],
        [{"id": 3, "service": "ups", "state": "RUNNING", "enable": True}],
        # apps (5 of them now — traefik added)
        [{"name": "netboot-xyz", "state": "RUNNING"}],
        [{"name": "minio-prd", "state": "RUNNING"}],
        [{"name": "minio-dev", "state": "RUNNING"}],
        [{"name": "meshcentral", "state": "RUNNING"}],
        [{"name": "traefik", "state": "RUNNING"}],
        # cert expiry
        [{"id": 3, "name": "w1-wildcard", "parsed": {"days_left": 70}}],
    ])

    rc = run(cli, _Ctx(apply=False), only=None)
    assert rc == 0


def test_run_returns_nonzero_when_any_fail(monkeypatch) -> None:
    from truenas_infra.modules import verify
    from truenas_infra.modules.verify import run
    monkeypatch.setattr(verify, "_dig_short", lambda *a, **k: "10.10.0.0")
    monkeypatch.setattr(verify, "_tls_handshake_cert",
                        lambda *a, **k: {"subject": "", "issuer": "", "sans": ["*.w1.lv", "w1.lv"]})

    cli = _mk_cli([
        # pool — MISSING (this fails us)
        [],
        [],
        [{"id": 1, "service": "nfs", "state": "RUNNING", "enable": True}],
        [{"id": 2, "service": "cifs", "state": "RUNNING", "enable": True}],
        [{"id": 3, "service": "ups", "state": "RUNNING", "enable": True}],
        [{"name": "netboot-xyz", "state": "RUNNING"}],
        [{"name": "minio-prd", "state": "RUNNING"}],
        [{"name": "minio-dev", "state": "RUNNING"}],
        [{"name": "meshcentral", "state": "RUNNING"}],
        [{"name": "traefik", "state": "RUNNING"}],
        [{"id": 3, "name": "w1-wildcard", "parsed": {"days_left": 70}}],
    ])

    rc = run(cli, _Ctx(apply=False), only=None)
    assert rc != 0


# ─── check_cert_expiry ───────────────────────────────────────────────────────


def test_check_cert_expiry_passes_when_cert_has_plenty_of_days() -> None:
    from truenas_infra.modules.verify import check_cert_expiry

    cli = _mk_cli([[{"id": 3, "name": "w1-wildcard", "parsed": {"days_left": 60}}]])
    r = check_cert_expiry(cli, cert_name="w1-wildcard", warn_days=14, fail_days=7)
    assert r.passed is True
    assert "60" in r.message


def test_check_cert_expiry_fails_when_under_fail_threshold() -> None:
    from truenas_infra.modules.verify import check_cert_expiry

    cli = _mk_cli([[{"id": 3, "name": "w1-wildcard", "parsed": {"days_left": 5}}]])
    r = check_cert_expiry(cli, cert_name="w1-wildcard", warn_days=14, fail_days=7)
    assert r.passed is False


def test_check_cert_expiry_fails_when_cert_missing() -> None:
    from truenas_infra.modules.verify import check_cert_expiry

    cli = _mk_cli([[]])
    r = check_cert_expiry(cli, cert_name="w1-wildcard", warn_days=14, fail_days=7)
    assert r.passed is False
    assert "not found" in r.message.lower()


# ─── check_tls_https (socket-level TLS probe) ────────────────────────────────


def test_check_tls_https_passes_when_chain_validates(monkeypatch) -> None:
    """TLS handshake succeeds + peer cert SAN covers the hostname."""
    from truenas_infra.modules import verify

    def fake_fetch(host, port, timeout):
        return {
            "subject": "CN=*.w1.lv",
            "issuer": "CN=R12, O=Let's Encrypt, C=US",
            "sans": ["*.w1.lv", "w1.lv"],
        }
    monkeypatch.setattr(verify, "_tls_handshake_cert", fake_fetch)

    r = verify.check_tls_https(host="mc.w1.lv", port=443)
    assert r.passed is True
    assert "R12" in r.message or "Let's Encrypt" in r.message


def test_check_tls_https_fails_when_handshake_errors(monkeypatch) -> None:
    from truenas_infra.modules import verify

    def fake_fetch(host, port, timeout):
        raise ConnectionError("connection refused")
    monkeypatch.setattr(verify, "_tls_handshake_cert", fake_fetch)

    r = verify.check_tls_https(host="mc.w1.lv", port=443)
    assert r.passed is False
    assert "refused" in r.message


def test_check_tls_https_fails_when_san_mismatch(monkeypatch) -> None:
    from truenas_infra.modules import verify

    def fake_fetch(host, port, timeout):
        return {
            "subject": "CN=*.other.lv",
            "issuer": "Let's Encrypt",
            "sans": ["*.other.lv"],
        }
    monkeypatch.setattr(verify, "_tls_handshake_cert", fake_fetch)

    r = verify.check_tls_https(host="mc.w1.lv", port=443)
    assert r.passed is False
    assert "san" in r.message.lower()


# ─── check_dns_records ───────────────────────────────────────────────────────


def test_check_dns_records_passes_when_all_resolve_correctly(monkeypatch) -> None:
    from truenas_infra.modules import verify

    # Fake resolver — returns the expected IP for every (host, resolver) pair.
    def fake_dig(host: str, resolver: str):
        mapping = {
            ("nas.w1.lv", "10.10.0.1"): "10.10.5.10",
            ("mc.w1.lv", "10.10.0.1"):  "10.10.5.20",
        }
        return mapping.get((host, resolver))
    monkeypatch.setattr(verify, "_dig_short", fake_dig)

    records = [
        {"name": "nas.w1.lv", "address": "10.10.5.10"},
        {"name": "mc.w1.lv",  "address": "10.10.5.20"},
    ]
    r = verify.check_dns_records(records=records, internal_resolver="10.10.0.1")
    assert r.passed is True


def test_check_dns_records_fails_when_any_record_wrong(monkeypatch) -> None:
    from truenas_infra.modules import verify

    def fake_dig(host: str, resolver: str):
        if host == "mc.w1.lv": return "10.10.5.99"  # WRONG
        return "10.10.5.10"
    monkeypatch.setattr(verify, "_dig_short", fake_dig)

    records = [
        {"name": "nas.w1.lv", "address": "10.10.5.10"},
        {"name": "mc.w1.lv",  "address": "10.10.5.20"},
    ]
    r = verify.check_dns_records(records=records, internal_resolver="10.10.0.1")
    assert r.passed is False
    assert "mc.w1.lv" in r.message


def test_run_fails_when_a_new_app_is_missing(monkeypatch) -> None:
    """Regression coverage: a MinIO instance not being in RUNNING should
    fail the verify matrix."""
    from truenas_infra.modules import verify
    from truenas_infra.modules.verify import run
    monkeypatch.setattr(verify, "_dig_short", lambda *a, **k: "10.10.0.0")
    monkeypatch.setattr(verify, "_tls_handshake_cert",
                        lambda *a, **k: {"subject": "", "issuer": "", "sans": ["*.w1.lv", "w1.lv"]})

    cli = _mk_cli([
        [{"name": "tank", "status": "ONLINE", "healthy": True}],
        [{"name": n} for n in (
            "tank/kube/prd", "tank/kube/dev",
            "tank/media", "tank/shared/general", "tank/system",
        )],
        [{"id": 1, "service": "nfs", "state": "RUNNING", "enable": True}],
        [{"id": 2, "service": "cifs", "state": "RUNNING", "enable": True}],
        [{"id": 3, "service": "ups", "state": "RUNNING", "enable": True}],
        [{"name": "netboot-xyz", "state": "RUNNING"}],
        # minio-prd CRASHED
        [{"name": "minio-prd", "state": "CRASHED"}],
        [{"name": "minio-dev", "state": "RUNNING"}],
        [{"name": "meshcentral", "state": "RUNNING"}],
        [{"name": "traefik", "state": "RUNNING"}],
        [{"id": 3, "name": "w1-wildcard", "parsed": {"days_left": 70}}],
    ])

    rc = run(cli, _Ctx(apply=False), only=None)
    assert rc != 0
