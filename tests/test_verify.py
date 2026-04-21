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


def test_run_returns_zero_when_all_pass() -> None:
    from truenas_infra.modules.verify import run

    cli = _mk_cli([
        # pool
        [{"name": "tank", "status": "ONLINE", "healthy": True}],
        # datasets
        [{"name": n} for n in (
            "tank/kube/prd", "tank/kube/dev",
            "tank/media", "tank/shared/general", "tank/system",
        )],
        # nfs service
        [{"id": 1, "service": "nfs", "state": "RUNNING", "enable": True}],
        # smb service
        [{"id": 2, "service": "cifs", "state": "RUNNING", "enable": True}],
        # ups service
        [{"id": 3, "service": "ups", "state": "RUNNING", "enable": True}],
        # apps (4 of them: netboot-xyz, minio-prd, minio-dev, meshcentral)
        [{"name": "netboot-xyz", "state": "RUNNING"}],
        [{"name": "minio-prd", "state": "RUNNING"}],
        [{"name": "minio-dev", "state": "RUNNING"}],
        [{"name": "meshcentral", "state": "RUNNING"}],
    ])

    rc = run(cli, _Ctx(apply=False), only=None)
    assert rc == 0


def test_run_returns_nonzero_when_any_fail() -> None:
    from truenas_infra.modules.verify import run

    cli = _mk_cli([
        # pool — MISSING
        [],
        # datasets
        [],
        # services
        [{"id": 1, "service": "nfs", "state": "RUNNING", "enable": True}],
        [{"id": 2, "service": "cifs", "state": "RUNNING", "enable": True}],
        [{"id": 3, "service": "ups", "state": "RUNNING", "enable": True}],
        # apps
        [{"name": "netboot-xyz", "state": "RUNNING"}],
        [{"name": "minio-prd", "state": "RUNNING"}],
        [{"name": "minio-dev", "state": "RUNNING"}],
        [{"name": "meshcentral", "state": "RUNNING"}],
    ])

    rc = run(cli, _Ctx(apply=False), only=None)
    assert rc != 0


def test_run_fails_when_a_new_app_is_missing() -> None:
    """Regression coverage: a MinIO instance not being in RUNNING should
    fail the verify matrix (previously only netboot-xyz was checked)."""
    from truenas_infra.modules.verify import run

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
    ])

    rc = run(cli, _Ctx(apply=False), only=None)
    assert rc != 0
