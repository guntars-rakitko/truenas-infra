"""Tests for modules/shares.py — phase 7 (NFS + SMB shares with per-VLAN binding)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── load_shares_config ──────────────────────────────────────────────────────


def test_load_shares_config_parses_all_sections(tmp_path: Path) -> None:
    from truenas_infra.modules.shares import load_shares_config

    yaml_file = tmp_path / "shares.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            nfs:
              service:
                enable: true
                bindip: [10.10.10.10, 10.10.15.10]
              shares:
                - name: longhorn-prd
                  path: /mnt/tank/kube/prd/longhorn
                  networks: [10.10.10.0/24]
                  maproot_user: nobody
                  maproot_group: nogroup

            smb:
              service:
                enable: true
                bindip: [10.10.20.10]
              shares:
                - name: general
                  path: /mnt/tank/shared/general
                  hostsallow: [10.10.20.0/24]
                  browsable: true
            """
        ).strip()
    )

    cfg = load_shares_config(yaml_file)

    assert cfg.nfs.enable is True
    assert cfg.nfs.bindip == ("10.10.10.10", "10.10.15.10")
    assert len(cfg.nfs_shares) == 1
    assert cfg.nfs_shares[0].path == "/mnt/tank/kube/prd/longhorn"
    assert cfg.nfs_shares[0].networks == ("10.10.10.0/24",)
    assert cfg.nfs_shares[0].comment == "longhorn-prd"

    assert cfg.smb.enable is True
    assert cfg.smb.bindip == ("10.10.20.10",)
    assert len(cfg.smb_shares) == 1
    assert cfg.smb_shares[0].name == "general"
    assert cfg.smb_shares[0].purpose == "DEFAULT_SHARE"


# ─── ensure_nfs_service ──────────────────────────────────────────────────────


def test_ensure_nfs_service_updates_bindip() -> None:
    from truenas_infra.modules.shares import NfsServiceSpec, ensure_nfs_service

    live_config = {"id": 1, "bindip": [], "v4": True}
    live_service = [{"id": 1, "service": "nfs", "enable": False, "state": "STOPPED"}]
    cli = _mk_cli([
        live_config,
        live_service,
        {**live_config, "bindip": ["10.10.10.10", "10.10.15.10"]},  # nfs.update result
        True,   # service.update
        True,   # service.start
    ])

    spec = NfsServiceSpec(enable=True, bindip=("10.10.10.10", "10.10.15.10"))
    diff = ensure_nfs_service(cli, spec=spec, apply=True)

    assert diff.changed is True
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "nfs.update" in names
    assert "service.update" in names
    assert "service.start" in names
    update = next(c for c in cli.call.call_args_list if c.args[0] == "nfs.update")
    assert sorted(update.args[1]["bindip"]) == ["10.10.10.10", "10.10.15.10"]


def test_ensure_nfs_service_noop_when_match() -> None:
    from truenas_infra.modules.shares import NfsServiceSpec, ensure_nfs_service

    live_config = {"id": 1, "bindip": ["10.10.10.10", "10.10.15.10"], "v4": True}
    live_service = [{"id": 1, "service": "nfs", "enable": True, "state": "RUNNING"}]
    cli = _mk_cli([live_config, live_service])

    spec = NfsServiceSpec(enable=True, bindip=("10.10.10.10", "10.10.15.10"))
    diff = ensure_nfs_service(cli, spec=spec, apply=True)

    assert diff.changed is False


# ─── ensure_nfs_share ────────────────────────────────────────────────────────


def test_ensure_nfs_share_creates_when_missing() -> None:
    from truenas_infra.modules.shares import NfsShareSpec, ensure_nfs_share

    cli = _mk_cli([
        [],                # sharing.nfs.query
        {"id": 1},         # sharing.nfs.create
    ])
    spec = NfsShareSpec(
        path="/mnt/tank/kube/prd/longhorn",
        networks=("10.10.10.0/24",),
        comment="longhorn-prd",
        maproot_user="nobody",
        maproot_group="nogroup",
    )
    diff = ensure_nfs_share(cli, spec=spec, apply=True)

    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "sharing.nfs.create")
    payload = create.args[1]
    assert payload["path"] == "/mnt/tank/kube/prd/longhorn"
    assert payload["networks"] == ["10.10.10.0/24"]


def test_ensure_nfs_share_noop_when_match() -> None:
    from truenas_infra.modules.shares import NfsShareSpec, ensure_nfs_share

    existing = {
        "id": 1,
        "path": "/mnt/tank/kube/prd/longhorn",
        "networks": ["10.10.10.0/24"],
        "hosts": [],
        "comment": "longhorn-prd",
        "maproot_user": "nobody",
        "maproot_group": "nogroup",
        "enabled": True,
    }
    cli = _mk_cli([[existing]])
    spec = NfsShareSpec(
        path="/mnt/tank/kube/prd/longhorn",
        networks=("10.10.10.0/24",),
        comment="longhorn-prd",
        maproot_user="nobody",
        maproot_group="nogroup",
    )
    diff = ensure_nfs_share(cli, spec=spec, apply=True)
    assert diff.changed is False


# ─── ensure_smb_service ──────────────────────────────────────────────────────


def test_ensure_smb_service_updates_bindip() -> None:
    from truenas_infra.modules.shares import SmbServiceSpec, ensure_smb_service

    live_config = {"id": 1, "bindip": []}
    live_service = [{"id": 2, "service": "cifs", "enable": False, "state": "STOPPED"}]
    cli = _mk_cli([
        live_config,
        live_service,
        {**live_config, "bindip": ["10.10.20.10"]},
        True,
        True,
    ])

    spec = SmbServiceSpec(enable=True, bindip=("10.10.20.10",))
    diff = ensure_smb_service(cli, spec=spec, apply=True)
    assert diff.changed is True


# ─── ensure_smb_share ────────────────────────────────────────────────────────


def test_ensure_smb_share_creates_when_missing() -> None:
    from truenas_infra.modules.shares import SmbShareSpec, ensure_smb_share

    cli = _mk_cli([[], {"id": 1}])
    spec = SmbShareSpec(
        name="general",
        path="/mnt/tank/shared/general",
        purpose="DEFAULT_SHARE",
        browsable=True,
    )
    diff = ensure_smb_share(cli, spec=spec, apply=True)
    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "sharing.smb.create")
    payload = create.args[1]
    assert payload["name"] == "general"
    assert payload["path"] == "/mnt/tank/shared/general"
    assert payload["purpose"] == "DEFAULT_SHARE"
    # hostsallow/guestok removed from 25.10 schema — must not appear.
    assert "hostsallow" not in payload
    assert "guestok" not in payload


# ─── run() orchestration ─────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        import structlog
        self.log = structlog.get_logger("test")


def test_run_orchestrates_nfs_and_smb(tmp_path: Path) -> None:
    from truenas_infra.modules.shares import run

    cfg_path = tmp_path / "shares.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            nfs:
              service:
                enable: true
                bindip: [10.10.10.10]
              shares:
                - name: longhorn-prd
                  path: /mnt/tank/kube/prd/longhorn
                  networks: [10.10.10.0/24]

            smb:
              service:
                enable: true
                bindip: [10.10.20.10]
              shares:
                - name: general
                  path: /mnt/tank/shared/general
                  hostsallow: [10.10.20.0/24]
            """
        ).strip()
    )

    cli = _mk_cli([
        # nfs service: already enabled + matching bindip (noop)
        {"id": 1, "bindip": ["10.10.10.10"], "v4": True},
        [{"id": 1, "service": "nfs", "enable": True, "state": "RUNNING"}],
        # nfs.share: query=[] + create
        [],
        {"id": 1},
        # smb service: similar noop
        {"id": 1, "bindip": ["10.10.20.10"], "workgroup": "HOME"},
        [{"id": 2, "service": "cifs", "enable": True, "state": "RUNNING"}],
        # smb.share: query=[] + create
        [],
        {"id": 2},
    ])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_path)

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "sharing.nfs.create" in names
    assert "sharing.smb.create" in names
