"""Tests for modules/verify.py — phase 10 (post-apply verification matrix)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import yaml


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# A throwaway git repo shaped like the mikrotik-infra clone that `phase verify`
# reads its DNS records from. Isolated from the operator's git config (global
# commit signing, hooks), which would otherwise make `git commit` prompt or fail.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, env=_GIT_ENV,
    ).stdout.strip()


def _make_mikrotik_clone(root: Path, dns_yaml: str | None) -> Path:
    """Init a repo, commit `configs/dns.yaml` (omitted when None), and point
    refs/remotes/origin/main at that commit — i.e. a freshly fetched clone."""
    repo = root / "mikrotik-infra"
    (repo / "configs").mkdir(parents=True)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    if dns_yaml is not None:
        (repo / "configs" / "dns.yaml").write_text(dns_yaml, encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _dns_yaml(records: dict[str, str]) -> str:
    return yaml.safe_dump(
        {"records": [{"name": n, "address": a} for n, a in records.items()]})


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

    cli = _mk_cli([[{"name": "pxe", "state": "RUNNING"}]])
    r = check_app(cli, app_name="pxe")
    assert r.passed is True


def test_check_app_fails_when_deploying() -> None:
    from truenas_infra.modules.verify import check_app

    cli = _mk_cli([[{"name": "pxe", "state": "DEPLOYING"}]])
    r = check_app(cli, app_name="pxe")
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


# The router records the all-pass fixture declares AND the fake resolver
# answers. Includes the msa2 BUILD records, which the old hand-mirrored
# config/dns.yaml never carried.
_ROUTER_RECORDS: dict[str, str] = {
    "nas.w1.lv": "10.10.5.10",
    "minio-prd.w1.lv": "10.10.5.20",
    "minio-dev.w1.lv": "10.10.5.20",
    "wiki.w1.lv": "10.10.5.20",
    "s3-prd.w1.lv": "10.10.10.10",
    "s3-dev.w1.lv": "10.10.15.10",
    "kub-prd-01.w1.lv": "10.10.5.11",
    "kub-dev-01.w1.lv": "10.10.5.14",
    "msa2-dev-01.w1.lv": "10.10.5.18",
    "msa2-prd-01.w1.lv": "10.10.5.17",
    "router.w1.lv": "10.10.0.1",
    "admin-dev.giks.lv": "10.10.15.20",
}


def _all_pass_cli() -> MagicMock:
    """A cli whose every API-backed check passes, so the DNS source is the
    only variable in the run() tests below."""
    return _mk_cli([
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
        # apps — ⚠ one entry per ENABLED app in config/apps.yaml, in order.
        # verify derives its list from that file (since 2026-09-23), so adding
        # or removing an app there changes how many cli.call()s happen here.
        # A short list shows up as StopIteration from the mock's side_effect,
        # which is what this fixture did for months after apps were added
        # without updating it.
        [{"name": "minio-prd", "state": "RUNNING"}],
        [{"name": "minio-dev", "state": "RUNNING"}],
        [{"name": "traefik", "state": "RUNNING"}],
        [{"name": "wiki", "state": "RUNNING"}],
        [{"name": "cluster-agent", "state": "RUNNING"}],
        # cert expiry
        [{"id": 3, "name": "w1-wildcard", "parsed": {"days_left": 70}}],
    ])


def _stub_network(monkeypatch) -> None:
    """No real DNS or TLS: the fake resolver answers every _ROUTER_RECORDS
    name correctly and nothing else."""
    from truenas_infra.modules import verify

    monkeypatch.setattr(verify, "_dig_short",
                        lambda host, resolver: _ROUTER_RECORDS.get(host))
    monkeypatch.setattr(verify, "_tls_handshake_cert",
                        lambda host, port, timeout: {
                            "subject": "CN=*.w1.lv",
                            "issuer": "CN=R12, O=Let's Encrypt, C=US",
                            "sans": ["*.w1.lv", "w1.lv"],
                        })


def _run_and_capture(cli: MagicMock) -> tuple[int, dict[str, dict]]:
    """run() plus its per-check log events, keyed by check name."""
    import structlog.testing

    from truenas_infra.modules.verify import run

    with structlog.testing.capture_logs() as logs:
        rc = run(cli, _Ctx(apply=False), only=None)
    events = {e["name"]: e for e in logs if e.get("event") in ("check_passed", "check_failed")}
    return rc, events


def test_run_returns_zero_when_all_pass(monkeypatch, tmp_path) -> None:
    _stub_network(monkeypatch)
    repo = _make_mikrotik_clone(tmp_path, _dns_yaml(_ROUTER_RECORDS))
    monkeypatch.setenv("MIKROTIK_INFRA_DIR", str(repo))

    rc, events = _run_and_capture(_all_pass_cli())

    assert rc == 0
    dns = events["dns records"]
    assert dns["event"] == "check_passed"
    n = len(_ROUTER_RECORDS)
    assert f"{n}/{n} resolve correctly" in dns["message"]
    # The source is named, commit and all, so a stale clone is visible.
    assert f"mikrotik-infra origin/main {_git(repo, 'rev-parse', '--short', 'HEAD')}" \
        in dns["message"]


def test_run_fails_when_mikrotik_clone_missing(monkeypatch) -> None:
    """Positive control for the test above: the SAME all-pass run, minus the
    clone (conftest points MIKROTIK_INFRA_DIR at a path that does not exist),
    must fail the matrix, naming the path. A missing source is never a skip."""
    _stub_network(monkeypatch)

    rc, events = _run_and_capture(_all_pass_cli())

    assert rc != 0
    dns = events["dns records"]
    assert dns["event"] == "check_failed"
    assert "/nonexistent/mikrotik-infra-for-tests" in dns["message"]
    # ...and it is the ONLY failure, so it alone flipped the rc.
    assert [k for k, e in events.items() if e["event"] == "check_failed"] == ["dns records"]


def test_run_fails_when_router_disagrees_with_declaration(monkeypatch, tmp_path) -> None:
    """A record the declaration carries but the router answers differently
    (the msa2 re-address, half done) fails the matrix."""
    _stub_network(monkeypatch)
    moved = {**_ROUTER_RECORDS, "msa2-prd-01.w1.lv": "10.10.5.11"}
    repo = _make_mikrotik_clone(tmp_path, _dns_yaml(moved))
    monkeypatch.setenv("MIKROTIK_INFRA_DIR", str(repo))

    rc, events = _run_and_capture(_all_pass_cli())

    assert rc != 0
    assert "msa2-prd-01.w1.lv→10.10.5.17 (want 10.10.5.11)" in events["dns records"]["message"]


# ─── load_router_dns_records ─────────────────────────────────────────────────


def test_load_router_dns_records_reads_origin_main_not_the_worktree(monkeypatch, tmp_path) -> None:
    """The shared clone may be parked on a branch with unmerged records; verify
    must vet what is MERGED (origin/main), not whatever is checked out."""
    from truenas_infra.modules import verify

    repo = _make_mikrotik_clone(tmp_path, _dns_yaml({"nas.w1.lv": "10.10.5.10"}))
    # A local commit origin/main does not have, plus an uncommitted edit on top.
    (repo / "configs" / "dns.yaml").write_text(
        _dns_yaml({"branch-only.w1.lv": "10.10.5.99"}), encoding="utf-8")
    _git(repo, "commit", "-qam", "unmerged branch work")
    (repo / "configs" / "dns.yaml").write_text(
        _dns_yaml({"dirty.w1.lv": "10.10.5.98"}), encoding="utf-8")
    monkeypatch.setenv("MIKROTIK_INFRA_DIR", str(repo))

    records, source = verify.load_router_dns_records()

    assert [r["name"] for r in records] == ["nas.w1.lv"]
    assert source.startswith("mikrotik-infra origin/main ")


def test_load_router_dns_records_honours_ref_override(monkeypatch, tmp_path) -> None:
    from truenas_infra.modules import verify

    repo = _make_mikrotik_clone(tmp_path, _dns_yaml({"nas.w1.lv": "10.10.5.10"}))
    (repo / "configs" / "dns.yaml").write_text(
        _dns_yaml({"branch-only.w1.lv": "10.10.5.99"}), encoding="utf-8")
    _git(repo, "commit", "-qam", "branch work, synced to the router by hand")
    monkeypatch.setenv("MIKROTIK_INFRA_DIR", str(repo))
    monkeypatch.setenv("MIKROTIK_DNS_REF", "HEAD")

    records, source = verify.load_router_dns_records()

    assert [r["name"] for r in records] == ["branch-only.w1.lv"]
    assert source.startswith("mikrotik-infra HEAD ")


def test_load_router_dns_records_raises_naming_missing_clone(monkeypatch, tmp_path) -> None:
    import pytest

    from truenas_infra.modules import verify

    monkeypatch.setenv("MIKROTIK_INFRA_DIR", str(tmp_path / "no-such-clone"))
    with pytest.raises(RuntimeError, match="clone not found at .*no-such-clone"):
        verify.load_router_dns_records()


def test_load_router_dns_records_raises_when_ref_unknown(monkeypatch, tmp_path) -> None:
    """A clone that was never fetched has no origin/main."""
    import pytest

    from truenas_infra.modules import verify

    repo = _make_mikrotik_clone(tmp_path, _dns_yaml({"nas.w1.lv": "10.10.5.10"}))
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    monkeypatch.setenv("MIKROTIK_INFRA_DIR", str(repo))
    with pytest.raises(RuntimeError, match="origin/main"):
        verify.load_router_dns_records()


def test_load_router_dns_records_raises_when_file_absent_at_ref(monkeypatch, tmp_path) -> None:
    import pytest

    from truenas_infra.modules import verify

    repo = _make_mikrotik_clone(tmp_path, None)
    monkeypatch.setenv("MIKROTIK_INFRA_DIR", str(repo))
    with pytest.raises(RuntimeError, match="configs/dns.yaml"):
        verify.load_router_dns_records()


def test_load_router_dns_records_raises_when_no_records(monkeypatch, tmp_path) -> None:
    import pytest

    from truenas_infra.modules import verify

    repo = _make_mikrotik_clone(tmp_path, "records: []\n")
    monkeypatch.setenv("MIKROTIK_INFRA_DIR", str(repo))
    with pytest.raises(RuntimeError, match="declares no records"):
        verify.load_router_dns_records()


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
        # apps — the enabled apps of config/apps.yaml, as in the test above,
        # so the ONLY failure is the missing pool
        [{"name": "minio-prd", "state": "RUNNING"}],
        [{"name": "minio-dev", "state": "RUNNING"}],
        [{"name": "traefik", "state": "RUNNING"}],
        [{"name": "wiki", "state": "RUNNING"}],
        [{"name": "cluster-agent", "state": "RUNNING"}],
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


def test_check_dns_records_fails_when_nothing_to_check(monkeypatch) -> None:
    """0 checked is not "0/0 resolve correctly": an empty declaration, or one
    where every record is `preserve: true`, must not read as a clean matrix."""
    from truenas_infra.modules import verify

    monkeypatch.setattr(verify, "_dig_short", lambda *a, **k: "10.10.0.1")
    for records in ([], [{"name": "ntp.w1.lv", "address": "10.10.0.1", "preserve": True}]):
        r = verify.check_dns_records(records=records, source="src-label")
        assert r.passed is False
        assert "no records to check" in r.message
        assert "src-label" in r.message


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
        [{"name": "pxe", "state": "RUNNING"}],
        # minio-prd CRASHED
        [{"name": "minio-prd", "state": "CRASHED"}],
        [{"name": "minio-dev", "state": "RUNNING"}],
        [{"name": "meshcentral", "state": "RUNNING"}],
        [{"name": "traefik", "state": "RUNNING"}],
        [{"name": "wiki", "state": "RUNNING"}],
        [{"name": "homepage", "state": "RUNNING"}],
        [{"name": "amtctl", "state": "RUNNING"}],
        [{"id": 3, "name": "w1-wildcard", "parsed": {"days_left": 70}}],
    ])

    rc = run(cli, _Ctx(apply=False), only=None)
    assert rc != 0
