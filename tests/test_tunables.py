"""Tests for modules/tunables.py — kernel boot args + sysctl tunables."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── ensure_kernel_extra_options ─────────────────────────────────────────────


def test_ensure_kernel_extra_options_sets_when_empty() -> None:
    from truenas_infra.modules.tunables import ensure_kernel_extra_options

    live = {"id": 1, "kernel_extra_options": ""}
    cli = _mk_cli([live, {**live, "kernel_extra_options": "a=1 b=2"}])

    diff = ensure_kernel_extra_options(cli, options=("a=1", "b=2"), apply=True)

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["system.advanced.config", "system.advanced.update"]
    update = next(c for c in cli.call.call_args_list if c.args[0] == "system.advanced.update")
    assert update.args[1] == {"kernel_extra_options": "a=1 b=2"}


def test_ensure_kernel_extra_options_noop_when_matches() -> None:
    from truenas_infra.modules.tunables import ensure_kernel_extra_options

    live = {"id": 1, "kernel_extra_options": "a=1 b=2"}
    cli = _mk_cli([live])

    diff = ensure_kernel_extra_options(cli, options=("a=1", "b=2"), apply=True)

    assert diff.changed is False
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "system.advanced.update" not in call_names


def test_ensure_kernel_extra_options_order_insensitive_for_comparison() -> None:
    """Order of options shouldn't matter for the match check."""
    from truenas_infra.modules.tunables import ensure_kernel_extra_options

    live = {"id": 1, "kernel_extra_options": "b=2 a=1"}
    cli = _mk_cli([live])

    diff = ensure_kernel_extra_options(cli, options=("a=1", "b=2"), apply=True)

    # Match regardless of order on the wire.
    assert diff.changed is False


def test_ensure_kernel_extra_options_dry_run_no_write() -> None:
    from truenas_infra.modules.tunables import ensure_kernel_extra_options

    live = {"id": 1, "kernel_extra_options": ""}
    cli = _mk_cli([live])

    diff = ensure_kernel_extra_options(cli, options=("a=1",), apply=False)

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "system.advanced.update" not in call_names


# ─── load_tunables_config + run ──────────────────────────────────────────────


def test_load_tunables_config_parses_kernel_options(tmp_path: Path) -> None:
    from truenas_infra.modules.tunables import load_tunables_config

    yaml_file = tmp_path / "tunables.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            kernel:
              extra_options:
                - nvme_core.default_ps_max_latency_us=0
                - pcie_aspm=off
                - pcie_port_pm=off
            """
        ).strip()
    )

    cfg = load_tunables_config(yaml_file)

    assert cfg.kernel_extra_options == (
        "nvme_core.default_ps_max_latency_us=0",
        "pcie_aspm=off",
        "pcie_port_pm=off",
    )


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        import structlog
        self.log = structlog.get_logger("test")


def test_run_applies_kernel_options(tmp_path: Path) -> None:
    from truenas_infra.modules.tunables import run

    cfg_path = tmp_path / "tunables.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            kernel:
              extra_options:
                - pcie_aspm=off
            """
        ).strip()
    )

    live = {"id": 1, "kernel_extra_options": ""}
    cli = _mk_cli([live, {**live, "kernel_extra_options": "pcie_aspm=off"}])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_path)

    assert rc == 0
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["system.advanced.config", "system.advanced.update"]


# ─── ensure_timezone ─────────────────────────────────────────────────────────


def test_ensure_timezone_updates_when_differs() -> None:
    from truenas_infra.modules.tunables import ensure_timezone

    live = {"id": 1, "timezone": "America/Los_Angeles"}
    cli = _mk_cli([live, {**live, "timezone": "UTC"}])
    diff = ensure_timezone(cli, timezone="UTC", apply=True)
    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "system.general.update")
    assert update.args[1] == {"timezone": "UTC"}


def test_ensure_timezone_noop_when_match() -> None:
    from truenas_infra.modules.tunables import ensure_timezone

    live = {"id": 1, "timezone": "UTC"}
    cli = _mk_cli([live])
    diff = ensure_timezone(cli, timezone="UTC", apply=True)
    assert diff.changed is False


# ─── ensure_ntp_servers ──────────────────────────────────────────────────────


def test_ensure_ntp_servers_replaces_defaults() -> None:
    """When live has 3 Debian pool servers and we want only ntp.w1.lv,
    create one and delete the three stale ones."""
    from truenas_infra.modules.tunables import ensure_ntp_servers

    existing = [
        {"id": 1, "address": "0.debian.pool.ntp.org"},
        {"id": 2, "address": "1.debian.pool.ntp.org"},
        {"id": 3, "address": "2.debian.pool.ntp.org"},
    ]
    cli = _mk_cli([
        existing,                 # ntpserver.query
        {"id": 4, "address": "ntp.w1.lv"},  # ntpserver.create
        True, True, True,          # three ntpserver.delete calls
    ])

    diff = ensure_ntp_servers(cli, addresses=("ntp.w1.lv",), apply=True)

    assert diff.changed is True
    names = [c.args[0] for c in cli.call.call_args_list]
    assert names.count("system.ntpserver.create") == 1
    assert names.count("system.ntpserver.delete") == 3


def test_ensure_ntp_servers_noop_when_match() -> None:
    from truenas_infra.modules.tunables import ensure_ntp_servers

    cli = _mk_cli([[{"id": 4, "address": "ntp.w1.lv"}]])
    diff = ensure_ntp_servers(cli, addresses=("ntp.w1.lv",), apply=True)
    assert diff.changed is False


# ─── ensure_tunables (ZFS module params + udev rules) ────────────────────────


def test_ensure_tunables_creates_missing() -> None:
    from truenas_infra.modules.tunables import TunableSpec, ensure_tunables

    cli = _mk_cli([[], {"id": 1}])  # tunable.query (empty), tunable.create
    specs = (TunableSpec(type="ZFS", var="zfs_txg_timeout", value="1", comment="x"),)

    diff = ensure_tunables(cli, specs=specs, apply=True)

    assert diff.changed is True
    names = [c.args[0] for c in cli.call.call_args_list]
    assert names == ["tunable.query", "tunable.create"]
    create = next(c for c in cli.call.call_args_list if c.args[0] == "tunable.create")
    assert create.args[1] == {
        "type": "ZFS",
        "var": "zfs_txg_timeout",
        "value": "1",
        "comment": "x",
        "enabled": True,
    }


def test_ensure_tunables_noop_when_match() -> None:
    from truenas_infra.modules.tunables import TunableSpec, ensure_tunables

    existing = [
        {"id": 1, "type": "ZFS", "var": "zfs_txg_timeout",
         "value": "1", "comment": "x", "enabled": True},
    ]
    cli = _mk_cli([existing])
    specs = (TunableSpec(type="ZFS", var="zfs_txg_timeout", value="1", comment="x"),)

    diff = ensure_tunables(cli, specs=specs, apply=True)

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "tunable.create" not in names
    assert "tunable.update" not in names


def test_ensure_tunables_updates_on_value_drift() -> None:
    from truenas_infra.modules.tunables import TunableSpec, ensure_tunables

    existing = [
        {"id": 7, "type": "ZFS", "var": "zfs_txg_timeout",
         "value": "5", "comment": "x", "enabled": True},
    ]
    cli = _mk_cli([existing, {"id": 7}])  # query, update
    specs = (TunableSpec(type="ZFS", var="zfs_txg_timeout", value="1", comment="x"),)

    diff = ensure_tunables(cli, specs=specs, apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "tunable.update")
    assert update.args[1] == 7  # id positional
    assert update.args[2] == {"value": "1", "comment": "x", "enabled": True}


def test_ensure_tunables_creates_udev_rule() -> None:
    from truenas_infra.modules.tunables import TunableSpec, ensure_tunables

    rule = 'ACTION=="add", SUBSYSTEM=="nvme", KERNEL=="nvme[0-9]", RUN+="/usr/sbin/nvme set-feature /dev/%k -f 0x02 -v 2"\n'
    cli = _mk_cli([[], {"id": 9}])
    specs = (TunableSpec(type="UDEV", var="90-nvme-ps2-cap", value=rule, comment="cap"),)

    diff = ensure_tunables(cli, specs=specs, apply=True)

    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "tunable.create")
    assert create.args[1]["type"] == "UDEV"
    assert create.args[1]["var"] == "90-nvme-ps2-cap"


def test_ensure_tunables_dry_run_no_write() -> None:
    from truenas_infra.modules.tunables import TunableSpec, ensure_tunables

    cli = _mk_cli([[]])  # only the query
    specs = (TunableSpec(type="ZFS", var="zfs_txg_timeout", value="1"),)

    diff = ensure_tunables(cli, specs=specs, apply=False)

    assert diff.changed is True
    names = [c.args[0] for c in cli.call.call_args_list]
    assert names == ["tunable.query"]  # no create/update in dry-run


def test_ensure_tunables_does_not_delete_unmanaged() -> None:
    """A live tunable we don't declare is left alone."""
    from truenas_infra.modules.tunables import TunableSpec, ensure_tunables

    existing = [
        {"id": 1, "type": "ZFS", "var": "zfs_txg_timeout",
         "value": "1", "comment": "x", "enabled": True},
        {"id": 2, "type": "SYSCTL", "var": "kernel.watchdog",
         "value": "0", "comment": "", "enabled": True},
    ]
    cli = _mk_cli([existing])
    specs = (TunableSpec(type="ZFS", var="zfs_txg_timeout", value="1", comment="x"),)

    diff = ensure_tunables(cli, specs=specs, apply=True)

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "tunable.delete" not in names


def test_load_tunables_config_parses_tunables(tmp_path: Path) -> None:
    from truenas_infra.modules.tunables import load_tunables_config

    yaml_file = tmp_path / "tunables.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            tunables:
              - type: ZFS
                var: zfs_txg_timeout
                value: "1"
                comment: "smaller txg"
              - type: UDEV
                var: 90-nvme-ps2-cap
                value: |
                  ACTION=="add", RUN+="/usr/sbin/nvme set-feature /dev/%k -f 0x02 -v 2"
            """
        ).strip()
    )

    cfg = load_tunables_config(yaml_file)

    assert len(cfg.tunables) == 2
    assert cfg.tunables[0].type == "ZFS"
    assert cfg.tunables[0].var == "zfs_txg_timeout"
    assert cfg.tunables[0].value == "1"
    assert cfg.tunables[1].type == "UDEV"
    assert "set-feature" in cfg.tunables[1].value
