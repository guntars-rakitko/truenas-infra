"""Tests for modules/pool.py — phase 4 (one-shot RAIDZ1 pool creation)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── load_pool_config ────────────────────────────────────────────────────────


def test_load_pool_config_parses_pool_section(tmp_path: Path) -> None:
    from truenas_infra.modules.pool import load_pool_config

    yaml_file = tmp_path / "storage.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            pool:
              name: tank
              topology:
                type: RAIDZ1
                disks:
                  - nvme0n1
                  - nvme1n1
                  - nvme2n1
              ashift: 12
              autotrim: true

            defaults:
              compression: lz4
            """
        ).strip()
    )

    cfg = load_pool_config(yaml_file)

    assert cfg.name == "tank"
    assert cfg.topology_type == "RAIDZ1"
    assert cfg.disks == ("nvme0n1", "nvme1n1", "nvme2n1")
    assert cfg.ashift == 12
    assert cfg.autotrim is True


# ─── resolve_disk_identifiers ────────────────────────────────────────────────


def test_resolve_disk_identifiers_validates_and_returns_devnames() -> None:
    from truenas_infra.modules.pool import resolve_disk_identifiers

    disks = [
        {"devname": "nvme0n1", "identifier": "{serial_lunid}AAA_111", "pool": None},
        {"devname": "nvme1n1", "identifier": "{serial_lunid}BBB_222", "pool": None},
        {"devname": "mmcblk0", "identifier": "{serial}0x442f5e32", "pool": None},
    ]
    cli = _mk_cli([disks])

    validated = resolve_disk_identifiers(cli, devnames=("nvme0n1", "nvme1n1"))

    # pool.create wants devnames, not identifiers.
    assert validated == ["nvme0n1", "nvme1n1"]


def test_resolve_disk_identifiers_raises_if_missing() -> None:
    from truenas_infra.modules.pool import resolve_disk_identifiers

    disks = [{"devname": "nvme0n1", "identifier": "{serial_lunid}AAA_111", "pool": None}]
    cli = _mk_cli([disks])

    with pytest.raises(RuntimeError, match="nvme9n9"):
        resolve_disk_identifiers(cli, devnames=("nvme0n1", "nvme9n9"))


def test_resolve_disk_identifiers_refuses_if_already_in_pool() -> None:
    from truenas_infra.modules.pool import resolve_disk_identifiers

    # Disk is already part of an existing pool — refuse to re-use it.
    disks = [
        {"devname": "nvme0n1", "identifier": "{serial_lunid}AAA", "pool": "something"},
        {"devname": "nvme1n1", "identifier": "{serial_lunid}BBB", "pool": None},
    ]
    cli = _mk_cli([disks])

    with pytest.raises(RuntimeError, match="already in pool"):
        resolve_disk_identifiers(cli, devnames=("nvme0n1", "nvme1n1"))


# ─── ensure_pool ─────────────────────────────────────────────────────────────


def _pool_spec() -> "PoolConfig":  # type: ignore[name-defined]
    from truenas_infra.modules.pool import PoolConfig
    return PoolConfig(
        name="tank",
        topology_type="RAIDZ1",
        disks=("nvme0n1", "nvme1n1", "nvme2n1"),
        ashift=12,
        autotrim=True,
    )


def _disk_list() -> list[dict]:
    return [
        {"devname": "nvme0n1", "identifier": "{serial_lunid}A", "pool": None},
        {"devname": "nvme1n1", "identifier": "{serial_lunid}B", "pool": None},
        {"devname": "nvme2n1", "identifier": "{serial_lunid}C", "pool": None},
    ]


def test_ensure_pool_noop_when_exists() -> None:
    from truenas_infra.modules.pool import CONFIRM_TOKEN, ensure_pool

    existing = [{"name": "tank", "status": "ONLINE"}]
    cli = _mk_cli([existing])

    diff = ensure_pool(cli, _pool_spec(), apply=True, confirm_token=CONFIRM_TOKEN)

    assert diff.changed is False
    assert diff.action == "noop"
    # Only queried; no disk.query or pool.create.
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["pool.query"]


def test_ensure_pool_refuses_without_confirm_token() -> None:
    from truenas_infra.modules.pool import ensure_pool

    cli = _mk_cli([[]])  # no existing pool

    with pytest.raises(RuntimeError, match="confirm"):
        ensure_pool(cli, _pool_spec(), apply=True, confirm_token="")


def test_ensure_pool_refuses_with_wrong_token() -> None:
    from truenas_infra.modules.pool import ensure_pool

    cli = _mk_cli([[]])

    with pytest.raises(RuntimeError, match="confirm"):
        ensure_pool(cli, _pool_spec(), apply=True, confirm_token="WRONG")


def test_ensure_pool_creates_when_missing() -> None:
    from truenas_infra.modules.pool import CONFIRM_TOKEN, ensure_pool

    cli = _mk_cli([
        [],                                               # pool.query (initial) — empty
        _disk_list(),                                     # disk.query
        {"id": 42, "name": "tank", "status": "ONLINE"},   # pool.create result
        [{"name": "tank", "status": "ONLINE"}],           # pool.query (post-check) — exists
    ])

    diff = ensure_pool(cli, _pool_spec(), apply=True, confirm_token=CONFIRM_TOKEN)

    assert diff.changed is True
    assert diff.action == "create"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["pool.query", "disk.query", "pool.create", "pool.query"]
    create = next(c for c in cli.call.call_args_list if c.args[0] == "pool.create")
    payload = create.args[1]
    assert payload["name"] == "tank"
    assert payload["topology"]["data"] == [
        {
            "type": "RAIDZ1",
            "disks": ["nvme0n1", "nvme1n1", "nvme2n1"],
        }
    ]


def test_ensure_pool_raises_if_post_check_missing() -> None:
    from truenas_infra.modules.pool import CONFIRM_TOKEN, ensure_pool

    cli = _mk_cli([
        [],                                               # pool.query — empty
        _disk_list(),                                     # disk.query
        {"id": 42, "name": "tank"},                        # pool.create "success"
        [],                                                # pool.query post-check — STILL empty
    ])

    with pytest.raises(RuntimeError, match="pool.create returned but pool"):
        ensure_pool(
            cli, _pool_spec(), apply=True, confirm_token=CONFIRM_TOKEN,
            post_check_timeout=0,
        )


def test_ensure_pool_dry_run_no_create() -> None:
    from truenas_infra.modules.pool import CONFIRM_TOKEN, ensure_pool

    cli = _mk_cli([[], _disk_list()])

    diff = ensure_pool(cli, _pool_spec(), apply=False, confirm_token=CONFIRM_TOKEN)

    assert diff.changed is True
    assert diff.action == "create"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["pool.query", "disk.query"]
    assert "pool.create" not in call_names


# ─── run() orchestration ─────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, *, apply: bool = False, confirm_token: str = "") -> None:
        self.apply = apply
        self.confirm_token = confirm_token
        import structlog
        self.log = structlog.get_logger("test")


def _write_storage_yaml(path: Path, disks: tuple[str, ...] = ("nvme0n1", "nvme1n1", "nvme2n1")) -> None:
    disk_lines = "\n".join(f"      - {d}" for d in disks)
    path.write_text(
        "pool:\n"
        "  name: tank\n"
        "  topology:\n"
        "    type: RAIDZ1\n"
        "    disks:\n"
        f"{disk_lines}\n"
        "  ashift: 12\n"
        "  autotrim: true\n"
    )


def test_run_noop_when_pool_exists(tmp_path: Path) -> None:
    from truenas_infra.modules.pool import run

    cfg_path = tmp_path / "storage.yaml"
    _write_storage_yaml(cfg_path)

    # autotrim already matches config (`true`), so ensure_autotrim is a noop.
    pool = {"name": "tank", "status": "ONLINE", "autotrim": {"value": "on"}}
    cli = _mk_cli([[pool], [pool]])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_path)

    assert rc == 0
    call_names = [c.args[0] for c in cli.call.call_args_list]
    # 1st query = ensure_pool existence check; 2nd = ensure_autotrim reconcile.
    assert call_names == ["pool.query", "pool.query"]


def test_run_creates_pool_with_confirm(tmp_path: Path) -> None:
    from truenas_infra.modules.pool import CONFIRM_TOKEN, run

    cfg_path = tmp_path / "storage.yaml"
    _write_storage_yaml(cfg_path)

    cli = _mk_cli([
        [],                                                # pool.query (initial)
        _disk_list(),                                      # disk.query
        {"id": 42, "name": "tank", "status": "ONLINE"},    # pool.create
        [{"name": "tank", "status": "ONLINE"}],            # pool.query (post-check)
        # ensure_autotrim reconcile query — pool already autotrim-on → noop.
        [{"name": "tank", "status": "ONLINE", "autotrim": {"value": "on"}}],
    ])

    rc = run(cli, _Ctx(apply=True, confirm_token=CONFIRM_TOKEN), only=None, config_path=cfg_path)

    assert rc == 0
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["pool.query", "disk.query", "pool.create", "pool.query", "pool.query"]


def test_run_refuses_without_confirm(tmp_path: Path) -> None:
    from truenas_infra.modules.pool import run

    cfg_path = tmp_path / "storage.yaml"
    _write_storage_yaml(cfg_path)
    cli = _mk_cli([[]])

    rc = run(cli, _Ctx(apply=True, confirm_token=""), only=None, config_path=cfg_path)

    # Non-zero rc — refused
    assert rc != 0
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "pool.create" not in call_names


# ─── ensure_autotrim ─────────────────────────────────────────────────────────


def test_ensure_autotrim_disables_when_on() -> None:
    from truenas_infra.modules.pool import PoolConfig, ensure_autotrim

    cfg = PoolConfig(name="tank", topology_type="RAIDZ1", disks=("nvme0n1",), autotrim=False)
    pool = {"id": 1, "name": "tank", "autotrim": {"value": "on"}}
    cli = _mk_cli([[pool], {**pool, "autotrim": {"value": "off"}}])

    diff = ensure_autotrim(cli, cfg, apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "pool.update")
    assert update.args[1] == 1
    assert update.args[2] == {"autotrim": "OFF"}


def test_ensure_autotrim_noop_when_already_off() -> None:
    from truenas_infra.modules.pool import PoolConfig, ensure_autotrim

    cfg = PoolConfig(name="tank", topology_type="RAIDZ1", disks=("nvme0n1",), autotrim=False)
    pool = {"id": 1, "name": "tank", "autotrim": {"value": "off"}}
    cli = _mk_cli([[pool]])

    diff = ensure_autotrim(cli, cfg, apply=True)

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "pool.update" not in names


def test_ensure_autotrim_noop_when_pool_absent() -> None:
    """No pool yet (fresh rebuild) → nothing to reconcile; creation sets defaults."""
    from truenas_infra.modules.pool import PoolConfig, ensure_autotrim

    cfg = PoolConfig(name="tank", topology_type="RAIDZ1", disks=("nvme0n1",), autotrim=False)
    cli = _mk_cli([[]])

    diff = ensure_autotrim(cli, cfg, apply=True)

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "pool.update" not in names


def test_ensure_autotrim_dry_run_no_write() -> None:
    from truenas_infra.modules.pool import PoolConfig, ensure_autotrim

    cfg = PoolConfig(name="tank", topology_type="RAIDZ1", disks=("nvme0n1",), autotrim=False)
    pool = {"id": 1, "name": "tank", "autotrim": {"value": "on"}}
    cli = _mk_cli([[pool]])

    diff = ensure_autotrim(cli, cfg, apply=False)

    assert diff.changed is True
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "pool.update" not in names
