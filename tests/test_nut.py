"""Tests for modules/nut.py — phase 8 (built-in UPS/NUT service)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── load_nut_config ─────────────────────────────────────────────────────────


def test_load_nut_config_parses_all_fields(tmp_path: Path) -> None:
    from truenas_infra.modules.nut import load_nut_config

    yaml_file = tmp_path / "services.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            nut:
              enable: true
              identifier: apc1
              description: "APC Smart-UPS"
              driver: "usbhid-ups$Smart-UPS (USB)"
              port: auto
              mode: MASTER
              remoteport: 3493
              shutdown: BATT
              shutdowntimer: 30
              monuser: upsmon
            """
        ).strip()
    )

    cfg = load_nut_config(yaml_file)

    assert cfg.enable is True
    assert cfg.identifier == "apc1"
    assert cfg.description == "APC Smart-UPS"
    assert cfg.driver == "usbhid-ups$Smart-UPS (USB)"
    assert cfg.port == "auto"
    assert cfg.mode == "MASTER"
    assert cfg.remoteport == 3493
    assert cfg.shutdown == "BATT"
    assert cfg.shutdowntimer == 30
    assert cfg.monuser == "upsmon"


# ─── ensure_ups_config ───────────────────────────────────────────────────────


def test_ensure_ups_config_updates_when_empty() -> None:
    from truenas_infra.modules.nut import NutSpec, ensure_ups_config

    live = {
        "id": 1, "driver": "", "port": "", "identifier": "ups",
        "mode": "MASTER", "description": "",
        "remoteport": 3493, "shutdown": "BATT", "shutdowntimer": 30,
        "monuser": "upsmon",
    }
    cli = _mk_cli([live, {**live, "driver": "usbhid-ups$Smart-UPS (USB)"}])

    spec = NutSpec(
        enable=True, identifier="apc1", description="APC",
        driver="usbhid-ups$Smart-UPS (USB)", port="auto",
        mode="MASTER", remoteport=3493, shutdown="BATT",
        shutdowntimer=30, monuser="upsmon",
    )
    diff = ensure_ups_config(cli, spec=spec, apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "ups.update")
    payload = update.args[1]
    assert payload["driver"] == "usbhid-ups$Smart-UPS (USB)"
    assert payload["port"] == "auto"
    assert payload["identifier"] == "apc1"


def test_ensure_ups_config_noop_when_match() -> None:
    from truenas_infra.modules.nut import NutSpec, ensure_ups_config

    live = {
        "id": 1,
        "driver": "usbhid-ups$Smart-UPS (USB)",
        "port": "auto",
        "identifier": "apc1",
        "description": "APC",
        "mode": "MASTER",
        "remoteport": 3493,
        "shutdown": "BATT",
        "shutdowntimer": 30,
        "monuser": "upsmon",
    }
    cli = _mk_cli([live])

    spec = NutSpec(
        enable=True, identifier="apc1", description="APC",
        driver="usbhid-ups$Smart-UPS (USB)", port="auto",
        mode="MASTER", remoteport=3493, shutdown="BATT",
        shutdowntimer=30, monuser="upsmon",
    )
    diff = ensure_ups_config(cli, spec=spec, apply=True)
    assert diff.changed is False


# ─── ensure_ups_service ──────────────────────────────────────────────────────


def test_ensure_ups_service_enables_and_starts() -> None:
    from truenas_infra.modules.nut import ensure_ups_service

    live_service = [{"id": 14, "service": "ups", "enable": False, "state": "STOPPED"}]
    cli = _mk_cli([live_service, True, True])

    diff = ensure_ups_service(cli, enable=True, apply=True)

    assert diff.changed is True
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "service.update" in names
    assert "service.start" in names


def test_ensure_ups_service_noop_when_already_running() -> None:
    from truenas_infra.modules.nut import ensure_ups_service

    live_service = [{"id": 14, "service": "ups", "enable": True, "state": "RUNNING"}]
    cli = _mk_cli([live_service])
    diff = ensure_ups_service(cli, enable=True, apply=True)
    assert diff.changed is False


# ─── run() orchestration ─────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        import structlog
        self.log = structlog.get_logger("test")


def test_run_applies_nut_config_and_starts_service(tmp_path: Path) -> None:
    from truenas_infra.modules.nut import run

    cfg_path = tmp_path / "services.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            nut:
              enable: true
              identifier: apc1
              description: "APC"
              driver: "usbhid-ups$Smart-UPS (USB)"
              port: auto
              mode: MASTER
              remoteport: 3493
              shutdown: BATT
              shutdowntimer: 30
              monuser: upsmon
            """
        ).strip()
    )

    empty_live = {
        "id": 1, "driver": "", "port": "", "identifier": "ups",
        "description": "", "mode": "MASTER",
        "remoteport": 3493, "shutdown": "BATT", "shutdowntimer": 30, "monuser": "upsmon",
    }
    cli = _mk_cli([
        empty_live,                                                         # ups.config
        {**empty_live, "driver": "usbhid-ups$Smart-UPS (USB)"},             # ups.update
        [{"id": 14, "service": "ups", "enable": False, "state": "STOPPED"}],# service.query
        True,                                                                # service.update
        True,                                                                # service.start
    ])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_path)

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "ups.update" in names
    assert "service.start" in names
