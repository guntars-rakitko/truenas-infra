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
              rmonitor: true
              shutdown: LOWBATT
              shutdowntimer: 60
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
    assert cfg.rmonitor is True
    assert cfg.shutdown == "LOWBATT"
    assert cfg.shutdowntimer == 60
    assert cfg.monuser == "upsmon"


def test_load_nut_config_rmonitor_defaults_false(tmp_path: Path) -> None:
    """Older YAML configs without `rmonitor:` parse with rmonitor=False (safe default)."""
    from truenas_infra.modules.nut import load_nut_config

    yaml_file = tmp_path / "services.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            nut:
              enable: true
              identifier: apc1
              driver: "usbhid-ups$Smart-UPS (USB)"
              port: auto
              mode: MASTER
              monuser: upsmon
            """
        ).strip()
    )

    cfg = load_nut_config(yaml_file)
    assert cfg.rmonitor is False


# ─── ensure_ups_config ───────────────────────────────────────────────────────


def test_ensure_ups_config_updates_when_empty() -> None:
    from truenas_infra.modules.nut import NutSpec, ensure_ups_config

    live = {
        "id": 1, "driver": "", "port": "", "identifier": "ups",
        "mode": "MASTER", "description": "",
        "remoteport": 3493, "rmonitor": False,
        "shutdown": "BATT", "shutdowntimer": 30,
        "monuser": "upsmon",
    }
    cli = _mk_cli([live, {**live, "driver": "usbhid-ups$Smart-UPS (USB)"}])

    spec = NutSpec(
        enable=True, identifier="apc1", description="APC",
        driver="usbhid-ups$Smart-UPS (USB)", port="auto",
        mode="MASTER", remoteport=3493, rmonitor=True,
        shutdown="LOWBATT", shutdowntimer=60, monuser="upsmon",
    )
    diff = ensure_ups_config(cli, spec=spec, apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "ups.update")
    payload = update.args[1]
    assert payload["driver"] == "usbhid-ups$Smart-UPS (USB)"
    assert payload["port"] == "auto"
    assert payload["identifier"] == "apc1"
    assert payload["rmonitor"] is True
    assert payload["shutdown"] == "LOWBATT"
    assert payload["shutdowntimer"] == 60


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
        "rmonitor": True,
        "shutdown": "LOWBATT",
        "shutdowntimer": 60,
        "powerdown": True,
        "monuser": "upsmon",
    }
    cli = _mk_cli([live])

    spec = NutSpec(
        enable=True, identifier="apc1", description="APC",
        driver="usbhid-ups$Smart-UPS (USB)", port="auto",
        mode="MASTER", remoteport=3493, rmonitor=True,
        shutdown="LOWBATT", shutdowntimer=60, powerdown=True, monuser="upsmon",
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
              rmonitor: true
              shutdown: LOWBATT
              shutdowntimer: 60
              monuser: upsmon
            """
        ).strip()
    )

    empty_live = {
        "id": 1, "driver": "", "port": "", "identifier": "ups",
        "description": "", "mode": "MASTER",
        "remoteport": 3493, "rmonitor": False,
        "shutdown": "BATT", "shutdowntimer": 30, "powerdown": False,
        "monuser": "upsmon", "extrausers": "",
    }
    cli = _mk_cli([
        empty_live,                                                         # ups.config (for ensure_ups_config)
        {**empty_live, "driver": "usbhid-ups$Smart-UPS (USB)"},             # ups.update
        empty_live,                                                         # ups.config (for ensure_extra_users)
        [{"id": 14, "service": "ups", "enable": False, "state": "STOPPED"}],# service.query
        True,                                                                # service.update
        True,                                                                # service.start
    ])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_path)

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "ups.update" in names
    assert "service.start" in names


# ─── extra_users tests (B2 — codify UPS HID thresholds + extra_users) ────────


def test_load_extra_users_reads_password_from_env(tmp_path: Path,
                                                   monkeypatch) -> None:
    from truenas_infra.modules.nut import load_nut_config

    monkeypatch.setenv("TRUENAS_NUT_ADMINPWD", "supersecret32chars")

    yaml_file = tmp_path / "services.yaml"
    yaml_file.write_text(textwrap.dedent("""
        nut:
          enable: true
          identifier: apc1
          driver: "usbhid-ups$Smart-UPS (USB)"
          port: auto
          mode: MASTER
          monuser: upsmon
          extra_users:
            - name: upsadmin
              password_env: TRUENAS_NUT_ADMINPWD
              actions: [SET]
              instcmds: [ALL]
    """).strip())

    cfg = load_nut_config(yaml_file)
    assert len(cfg.extra_users) == 1
    u = cfg.extra_users[0]
    assert u.name == "upsadmin"
    assert u.password == "supersecret32chars"
    assert u.actions == ("SET",)
    assert u.instcmds == ("ALL",)


def test_load_extra_users_missing_env_yields_empty_password(tmp_path: Path,
                                                             monkeypatch) -> None:
    """Missing env → empty password. Caller will skip these in ensure_extra_users."""
    from truenas_infra.modules.nut import load_nut_config

    monkeypatch.delenv("TRUENAS_NUT_ADMINPWD", raising=False)

    yaml_file = tmp_path / "services.yaml"
    yaml_file.write_text(textwrap.dedent("""
        nut:
          enable: true
          identifier: apc1
          driver: "x$y"
          port: auto
          mode: MASTER
          monuser: upsmon
          extra_users:
            - name: upsadmin
              password_env: TRUENAS_NUT_ADMINPWD
              actions: [SET]
              instcmds: [ALL]
    """).strip())

    cfg = load_nut_config(yaml_file)
    assert cfg.extra_users[0].password == ""


def test_render_extrausers_format() -> None:
    from truenas_infra.modules.nut import ExtraUserSpec, _render_extrausers

    users = (
        ExtraUserSpec(name="upsadmin", password="secret",
                      actions=("SET",), instcmds=("ALL",)),
    )
    out = _render_extrausers(users)
    assert out == (
        "[upsadmin]\n"
        "    password = secret\n"
        "    actions = SET\n"
        "    instcmds = ALL"
    )


def test_ensure_extra_users_noop_when_matching() -> None:
    from truenas_infra.modules.nut import (
        ExtraUserSpec, NutSpec, _render_extrausers, ensure_extra_users,
    )

    user = ExtraUserSpec(name="upsadmin", password="secret",
                         actions=("SET",), instcmds=("ALL",))
    spec = NutSpec(extra_users=(user,))
    rendered = _render_extrausers((user,))

    cli = _mk_cli([{"extrausers": rendered}])
    diff = ensure_extra_users(cli, spec=spec, apply=False)
    assert diff.changed is False
    assert diff.action == "noop"


def test_ensure_extra_users_diff_when_live_empty() -> None:
    from truenas_infra.modules.nut import (
        ExtraUserSpec, NutSpec, ensure_extra_users,
    )

    user = ExtraUserSpec(name="upsadmin", password="secret",
                         actions=("SET",), instcmds=("ALL",))
    spec = NutSpec(extra_users=(user,))

    cli = _mk_cli([
        {"extrausers": ""},   # ups.config read
        {"extrausers": "..."}, # ups.update return
    ])
    diff = ensure_extra_users(cli, spec=spec, apply=True)
    assert diff.changed is True
    # password redacted in diff output
    assert "secret" not in str(diff.after)
    assert "<redacted>" in str(diff.after)


def test_ensure_extra_users_skips_users_with_empty_password() -> None:
    """Users with empty password (missing env) must NOT be written."""
    from truenas_infra.modules.nut import (
        ExtraUserSpec, NutSpec, ensure_extra_users,
    )

    user = ExtraUserSpec(name="upsadmin", password="",
                         actions=("SET",), instcmds=("ALL",))
    spec = NutSpec(extra_users=(user,))

    cli = _mk_cli([{"extrausers": ""}])  # noop expected — no upsadmin renders
    diff = ensure_extra_users(cli, spec=spec, apply=True)
    # Both desired AND live are effectively empty → noop
    assert diff.action == "noop"
    # ups.update was NOT called
    update_calls = [c for c in cli.call.call_args_list
                    if c.args[0] == "ups.update"]
    assert len(update_calls) == 0


# ─── HID threshold drift detection tests ─────────────────────────────────────


def test_check_ups_hid_thresholds_no_drift() -> None:
    from truenas_infra.modules.nut import (
        UpsThresholdsSpec, check_ups_hid_thresholds,
    )
    spec = UpsThresholdsSpec(ups_delay_shutdown=300, ups_delay_start=60)
    reader = lambda: {"ups.delay.shutdown": "300", "ups.delay.start": "60"}
    drifts = check_ups_hid_thresholds(spec, _upsc_reader=reader)
    assert drifts == []


def test_check_ups_hid_thresholds_detects_drift() -> None:
    from truenas_infra.modules.nut import (
        UpsThresholdsSpec, check_ups_hid_thresholds,
    )
    spec = UpsThresholdsSpec(ups_delay_shutdown=300)
    reader = lambda: {"ups.delay.shutdown": "20"}
    drifts = check_ups_hid_thresholds(spec, _upsc_reader=reader)
    assert len(drifts) == 1
    assert drifts[0].hid_var == "ups.delay.shutdown"
    assert drifts[0].desired == 300
    assert drifts[0].live == 20


def test_check_ups_hid_thresholds_skips_none_fields() -> None:
    """Fields set to None in spec are skipped (not declared in YAML)."""
    from truenas_infra.modules.nut import (
        UpsThresholdsSpec, check_ups_hid_thresholds,
    )
    spec = UpsThresholdsSpec()  # all None
    reader = lambda: {"ups.delay.shutdown": "20"}  # would drift if checked
    drifts = check_ups_hid_thresholds(spec, _upsc_reader=reader)
    assert drifts == []


def test_check_ups_hid_thresholds_handles_unreadable_value() -> None:
    """Live value missing (e.g. UPS doesn't expose it) → live=None, drift reported."""
    from truenas_infra.modules.nut import (
        UpsThresholdsSpec, check_ups_hid_thresholds,
    )
    spec = UpsThresholdsSpec(ups_test_interval=2419200)
    reader = lambda: {}  # var not exposed by this UPS
    drifts = check_ups_hid_thresholds(spec, _upsc_reader=reader)
    assert len(drifts) == 1
    assert drifts[0].live is None
