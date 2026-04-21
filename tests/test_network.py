"""Tests for modules/network.py — phase 2 (VLAN sub-interfaces on NIC1)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── load_network_config ─────────────────────────────────────────────────────


def test_load_network_config_parses_full_file(tmp_path: Path) -> None:
    from truenas_infra.modules.network import load_network_config

    yaml_file = tmp_path / "network.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            nics:
              mgmt:
                device: enp2s0
                role: management
                vlan: null
                ipv4: 10.10.5.10/24
                gateway: 10.10.0.1
                mtu: 1500
              trunk:
                device: enp1s0
                role: data
                mtu: 1500
                vlans:
                  - vid: 10
                    name: vlan10
                    ipv4: 10.10.10.10/24
                  - vid: 15
                    name: vlan15
                    ipv4: 10.10.15.10/24
                  - vid: 20
                    name: vlan20
                    ipv4: 10.10.20.10/24

            dns:
              servers: [10.10.0.1]

            hostname: nas01
            domain: home.arpa
            """
        ).strip()
    )

    cfg = load_network_config(yaml_file)

    assert cfg.trunk.device == "enp1s0"
    assert cfg.trunk.mtu == 1500
    assert len(cfg.trunk.vlans) == 3
    assert cfg.trunk.vlans[0].name == "vlan10"
    assert cfg.trunk.vlans[0].vid == 10
    assert cfg.trunk.vlans[0].ipv4 == "10.10.10.10/24"

    assert cfg.mgmt.device == "enp2s0"
    assert cfg.mgmt.ipv4 == "10.10.5.10/24"
    assert cfg.mgmt.gateway == "10.10.0.1"

    assert cfg.hostname == "nas01"
    assert cfg.domain == "home.arpa"
    assert cfg.dns_servers == ("10.10.0.1",)


# ─── ensure_vlan_interface ───────────────────────────────────────────────────


def test_ensure_vlan_creates_when_missing() -> None:
    from truenas_infra.modules.network import VlanSpec, ensure_vlan_interface

    cli = _mk_cli([
        [],                                        # interface.query → no match
        {"id": "vlan10", "name": "vlan10"},        # interface.create → created
    ])
    spec = VlanSpec(name="vlan10", vid=10, ipv4="10.10.10.10/24")

    diff = ensure_vlan_interface(cli, spec, parent="enp1s0", apply=True)

    assert diff.changed is True
    assert diff.action == "create"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["interface.query", "interface.create"]
    create = next(c for c in cli.call.call_args_list if c.args[0] == "interface.create")
    payload = create.args[1]
    assert payload["type"] == "VLAN"
    assert payload["name"] == "vlan10"
    assert payload["vlan_parent_interface"] == "enp1s0"
    assert payload["vlan_tag"] == 10
    assert payload["aliases"] == [{"address": "10.10.10.10", "netmask": 24}]
    assert payload["ipv4_dhcp"] is False


def test_ensure_vlan_dry_run_no_write() -> None:
    from truenas_infra.modules.network import VlanSpec, ensure_vlan_interface

    cli = _mk_cli([[]])
    spec = VlanSpec(name="vlan10", vid=10, ipv4="10.10.10.10/24")

    diff = ensure_vlan_interface(cli, spec, parent="enp1s0", apply=False)

    assert diff.changed is True
    assert diff.action == "create"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["interface.query"]


def _existing_vlan(
    *,
    name: str = "vlan10",
    vid: int = 10,
    parent: str = "enp1s0",
    ipv4: str = "10.10.10.10",
    mask: int = 24,
) -> dict:
    return {
        "id": name,
        "name": name,
        "type": "VLAN",
        "vlan_parent_interface": parent,
        "vlan_tag": vid,
        "vlan_pcp": None,
        "aliases": [{"type": "INET", "address": ipv4, "netmask": mask}],
        "ipv4_dhcp": False,
        "ipv6_auto": False,
        "mtu": None,
    }


def test_ensure_vlan_noop_when_match() -> None:
    from truenas_infra.modules.network import VlanSpec, ensure_vlan_interface

    existing = _existing_vlan()
    cli = _mk_cli([[existing]])

    spec = VlanSpec(name="vlan10", vid=10, ipv4="10.10.10.10/24")
    diff = ensure_vlan_interface(cli, spec, parent="enp1s0", apply=True)

    assert diff.changed is False
    assert diff.action == "noop"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["interface.query"]


def test_ensure_vlan_updates_when_ip_differs() -> None:
    from truenas_infra.modules.network import VlanSpec, ensure_vlan_interface

    existing = _existing_vlan(ipv4="10.10.10.11")  # wrong IP in live state
    updated = {**existing, "aliases": [{"type": "INET", "address": "10.10.10.10", "netmask": 24}]}
    cli = _mk_cli([[existing], updated])

    spec = VlanSpec(name="vlan10", vid=10, ipv4="10.10.10.10/24")
    diff = ensure_vlan_interface(cli, spec, parent="enp1s0", apply=True)

    assert diff.changed is True
    assert diff.action == "update"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "interface.update" in call_names
    update = next(c for c in cli.call.call_args_list if c.args[0] == "interface.update")
    assert update.args[1] == "vlan10"
    # Payload should include the corrected alias.
    assert update.args[2]["aliases"] == [{"address": "10.10.10.10", "netmask": 24}]


# ─── ensure_trunk_parent (turn off DHCP on the trunk physical NIC) ───────────


def test_ensure_trunk_parent_turns_off_dhcp() -> None:
    from truenas_infra.modules.network import ensure_trunk_parent

    live = {"id": "enp1s0", "name": "enp1s0", "type": "PHYSICAL", "ipv4_dhcp": True, "aliases": []}
    updated = {**live, "ipv4_dhcp": False}
    cli = _mk_cli([[live], updated])

    diff = ensure_trunk_parent(cli, device="enp1s0", apply=True)

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "interface.update" in call_names
    update = next(c for c in cli.call.call_args_list if c.args[0] == "interface.update")
    assert update.args[1] == "enp1s0"
    assert update.args[2]["ipv4_dhcp"] is False


def test_ensure_trunk_parent_noop_when_already_off() -> None:
    from truenas_infra.modules.network import ensure_trunk_parent

    live = {
        "id": "enp1s0", "name": "enp1s0", "type": "PHYSICAL",
        "ipv4_dhcp": False, "ipv6_auto": False, "aliases": [],
    }
    cli = _mk_cli([[live]])

    diff = ensure_trunk_parent(cli, device="enp1s0", apply=True)

    assert diff.changed is False
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "interface.update" not in call_names


def test_ensure_trunk_parent_disables_ipv6_auto() -> None:
    from truenas_infra.modules.network import ensure_trunk_parent

    live = {
        "id": "enp1s0", "name": "enp1s0", "type": "PHYSICAL",
        "ipv4_dhcp": False, "ipv6_auto": True, "aliases": [],
    }
    updated = {**live, "ipv6_auto": False}
    cli = _mk_cli([[live], updated])

    diff = ensure_trunk_parent(cli, device="enp1s0", apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "interface.update")
    assert update.args[2]["ipv6_auto"] is False


def test_ensure_trunk_parent_raises_when_missing() -> None:
    from truenas_infra.modules.network import ensure_trunk_parent

    cli = _mk_cli([[]])  # query returns no match

    with pytest.raises(RuntimeError, match="enp1s0"):
        ensure_trunk_parent(cli, device="enp1s0", apply=True)


# ─── commit_network_changes ──────────────────────────────────────────────────


def test_commit_network_changes_skips_when_no_pending() -> None:
    from truenas_infra.modules.network import commit_network_changes

    cli = _mk_cli([])
    commit_network_changes(cli, has_pending=False, reachable_fn=lambda: True)
    assert cli.call.call_count == 0


def test_commit_network_changes_dry_run_skips() -> None:
    from truenas_infra.modules.network import commit_network_changes

    cli = _mk_cli([])
    commit_network_changes(cli, apply=False, reachable_fn=lambda: True)
    assert cli.call.call_count == 0


def test_commit_network_changes_calls_commit_with_rollback_false() -> None:
    from truenas_infra.modules.network import commit_network_changes

    cli = MagicMock()
    cli.call.return_value = None

    commit_network_changes(
        cli, has_pending=True, apply=True, reachable_fn=lambda: True, reconnect_grace=0,
    )

    call = cli.call.call_args_list[0]
    assert call.args[0] == "interface.commit"
    assert call.args[1] == {"rollback": False}


def test_commit_network_changes_tolerates_ws_drop() -> None:
    from truenas_infra.modules.network import commit_network_changes

    cli = MagicMock()
    cli.call.side_effect = RuntimeError("WebSocket closed")

    # Should NOT re-raise — commit is expected to drop the WS sometimes.
    commit_network_changes(
        cli, has_pending=True, apply=True, reachable_fn=lambda: True, reconnect_grace=0,
    )

    # No checkin should be called.
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["interface.commit"]


def test_commit_network_changes_raises_if_not_reachable() -> None:
    from truenas_infra.modules.network import commit_network_changes

    cli = MagicMock()
    cli.call.return_value = None

    with pytest.raises(RuntimeError, match="did not come back"):
        commit_network_changes(
            cli,
            has_pending=True,
            apply=True,
            reachable_fn=lambda: False,
            reconnect_max_wait=1,
            reconnect_grace=0,
        )


# ─── ensure_global_network ───────────────────────────────────────────────────


def test_ensure_global_network_updates_hostname_and_dns() -> None:
    from truenas_infra.modules.network import ensure_global_network

    live = {
        "id": 1,
        "hostname": "truenas",
        "domain": "local",
        "nameserver1": "",
        "nameserver2": "",
        "nameserver3": "",
    }
    cli = _mk_cli([live, {**live, "hostname": "nas-01", "domain": "w1.lv", "nameserver1": "10.10.0.1"}])

    diff = ensure_global_network(
        cli,
        hostname="nas-01",
        domain="w1.lv",
        dns=("10.10.0.1",),
        apply=True,
    )

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["network.configuration.config", "network.configuration.update"]
    update_call = next(c for c in cli.call.call_args_list if c.args[0] == "network.configuration.update")
    payload = update_call.args[1]
    assert payload["hostname"] == "nas-01"
    assert payload["domain"] == "w1.lv"
    assert payload["nameserver1"] == "10.10.0.1"


def test_ensure_global_network_noop_when_match() -> None:
    from truenas_infra.modules.network import ensure_global_network

    live = {
        "id": 1,
        "hostname": "nas-01",
        "domain": "w1.lv",
        "nameserver1": "10.10.0.1",
        "nameserver2": "",
        "nameserver3": "",
    }
    cli = _mk_cli([live])

    diff = ensure_global_network(cli, hostname="nas-01", domain="w1.lv", dns=("10.10.0.1",), apply=True)

    assert diff.changed is False
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "network.configuration.update" not in call_names


# ─── ensure_ui_bindip ────────────────────────────────────────────────────────


def test_ensure_ui_bindip_updates_when_unbound() -> None:
    from truenas_infra.modules.network import ensure_ui_bindip

    # Default: UI listens on all interfaces — ui_address is empty.
    live = {"id": 1, "ui_address": ["0.0.0.0"], "ui_v6address": ["::"]}
    cli = _mk_cli([
        live,
        {**live, "ui_address": ["10.10.5.10"]},
        None,  # system.general.ui_restart returns null
    ])

    diff = ensure_ui_bindip(cli, addresses=("10.10.5.10",), apply=True)

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["system.general.config", "system.general.update", "system.general.ui_restart"]
    update = next(c for c in cli.call.call_args_list if c.args[0] == "system.general.update")
    assert update.args[1]["ui_address"] == ["10.10.5.10"]


def test_ensure_ui_bindip_noop_when_match() -> None:
    from truenas_infra.modules.network import ensure_ui_bindip

    live = {"id": 1, "ui_address": ["10.10.5.10"], "ui_v6address": ["::"]}
    cli = _mk_cli([live])

    diff = ensure_ui_bindip(cli, addresses=("10.10.5.10",), apply=True)

    assert diff.changed is False
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "system.general.update" not in call_names


# ─── ensure_mgmt_interface ───────────────────────────────────────────────────


def test_ensure_mgmt_interface_sets_static() -> None:
    from truenas_infra.modules.network import ensure_mgmt_interface

    live = {
        "id": "enp2s0", "name": "enp2s0", "type": "PHYSICAL",
        "ipv4_dhcp": True, "ipv6_auto": True, "aliases": [],
    }
    updated = {**live, "ipv4_dhcp": False, "ipv6_auto": False,
               "aliases": [{"address": "10.10.5.10", "netmask": 24}]}
    cli = _mk_cli([[live], updated])

    diff = ensure_mgmt_interface(cli, device="enp2s0", ipv4="10.10.5.10/24", apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "interface.update")
    payload = update.args[2]
    assert payload["ipv4_dhcp"] is False
    assert payload["ipv6_auto"] is False
    assert payload["aliases"] == [{"address": "10.10.5.10", "netmask": 24}]


def test_ensure_mgmt_interface_noop_when_already_static() -> None:
    from truenas_infra.modules.network import ensure_mgmt_interface

    live = {
        "id": "enp2s0", "name": "enp2s0", "type": "PHYSICAL",
        "ipv4_dhcp": False, "ipv6_auto": False,
        "aliases": [{"type": "INET", "address": "10.10.5.10", "netmask": 24}],
    }
    cli = _mk_cli([[live]])

    diff = ensure_mgmt_interface(cli, device="enp2s0", ipv4="10.10.5.10/24", apply=True)

    assert diff.changed is False
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "interface.update" not in call_names


# ─── run() orchestration ─────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        import structlog
        self.log = structlog.get_logger("test")


def _write_network_yaml(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """
            nics:
              mgmt:
                device: enp2s0
                role: management
                ipv4: 10.10.5.10/24
                gateway: 10.10.5.1
                mtu: 1500
              trunk:
                device: enp1s0
                role: data
                mtu: 1500
                vlans:
                  - vid: 10
                    name: vlan10
                    ipv4: 10.10.10.10/24
                  - vid: 15
                    name: vlan15
                    ipv4: 10.10.15.10/24
                  - vid: 20
                    name: vlan20
                    ipv4: 10.10.20.10/24

            dns:
              servers: [10.10.0.1]

            hostname: nas-01
            domain: w1.lv
            """
        ).strip()
    )


def test_run_creates_vlans_and_commits(tmp_path: Path, monkeypatch) -> None:
    from truenas_infra.modules.network import run

    # Skip real reconnect_grace sleep.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *a, **kw: None)

    cfg_path = tmp_path / "network.yaml"
    _write_network_yaml(cfg_path)

    trunk_live = {"id": "enp1s0", "name": "enp1s0", "type": "PHYSICAL", "ipv4_dhcp": True, "aliases": []}
    trunk_updated = {**trunk_live, "ipv4_dhcp": False}
    net_global = {"id": 1, "hostname": "nas-01", "domain": "w1.lv",
                  "nameserver1": "10.10.0.1", "ipv4gateway": ""}

    # Mgmt NIC already static — no change there.
    mgmt_live = {
        "id": "enp2s0", "name": "enp2s0", "type": "PHYSICAL",
        "ipv4_dhcp": False, "ipv6_auto": False,
        "aliases": [{"type": "INET", "address": "10.10.5.10", "netmask": 24}],
    }
    ui_live = {"id": 1, "ui_address": ["10.10.5.10"], "ui_v6address": ["::"]}

    updated_global = {**net_global, "ipv4gateway": "10.10.5.1"}
    cli = _mk_cli([
        # ensure_trunk_parent
        [trunk_live],
        trunk_updated,
        # ensure_mgmt_interface (noop)
        [mgmt_live],
        # VLAN 10 / 15 / 20
        [], {"id": "vlan10", "name": "vlan10"},
        [], {"id": "vlan15", "name": "vlan15"},
        [], {"id": "vlan20", "name": "vlan20"},
        # commit (rollback=False, no checkin)
        None,
        # network.configuration.config + update (gateway differs)
        net_global,
        updated_global,
        # system.general.config (noop — already bound to 10.10.5.10)
        ui_live,
    ])

    rc = run(
        cli,
        _Ctx(apply=True),
        only=None,
        config_path=cfg_path,
        reachable_fn=lambda: True,
    )

    assert rc == 0
    main_calls = [c.args[0] for c in cli.call.call_args_list]
    assert main_calls == [
        "interface.query",            # trunk parent
        "interface.update",           # trunk parent — turn off dhcp
        "interface.query",            # mgmt (noop)
        "interface.query", "interface.create",  # vlan10
        "interface.query", "interface.create",  # vlan15
        "interface.query", "interface.create",  # vlan20
        "interface.commit",
        "network.configuration.config",
        "network.configuration.update",
        "system.general.config",
    ]
    assert "interface.checkin" not in main_calls


def test_run_skips_commit_when_nothing_changes(tmp_path: Path) -> None:
    from truenas_infra.modules.network import run

    cfg_path = tmp_path / "network.yaml"
    _write_network_yaml(cfg_path)

    trunk_live = {"id": "enp1s0", "name": "enp1s0", "type": "PHYSICAL", "ipv4_dhcp": False, "aliases": []}
    v10 = _existing_vlan(name="vlan10", vid=10, ipv4="10.10.10.10")
    v15 = _existing_vlan(name="vlan15", vid=15, ipv4="10.10.15.10")
    v20 = _existing_vlan(name="vlan20", vid=20, ipv4="10.10.20.10")
    # All-already-matches fixtures — second test.
    net_global = {"id": 1, "hostname": "nas-01", "domain": "w1.lv",
                  "nameserver1": "10.10.0.1", "ipv4gateway": "10.10.5.1"}

    mgmt_live = {
        "id": "enp2s0", "name": "enp2s0", "type": "PHYSICAL",
        "ipv4_dhcp": False, "ipv6_auto": False,
        "aliases": [{"type": "INET", "address": "10.10.5.10", "netmask": 24}],
    }
    ui_live = {"id": 1, "ui_address": ["10.10.5.10"], "ui_v6address": ["::"]}
    cli = _mk_cli([
        [trunk_live],
        [mgmt_live],
        [v10], [v15], [v20],
        net_global,
        ui_live,
    ])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_path, reachable_fn=lambda: True)

    assert rc == 0
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "interface.commit" not in call_names
    assert "interface.update" not in call_names
    assert "interface.create" not in call_names
