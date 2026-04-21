"""Tests for modules/users.py — phase 1 (users, SSH, email alerts)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ─── load_users_config ───────────────────────────────────────────────────────


def test_load_users_config_parses_simple_file(tmp_path: Path) -> None:
    from truenas_infra.modules.users import load_users_config

    yaml_file = tmp_path / "users.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            users:
              - username: svc-automation
                full_name: "Automation service account"
                shell: /usr/sbin/nologin
                sudo: false
                ssh_keys:
                  - "ssh-ed25519 AAAA... test@host"

            email_alerts:
              admin_email: "admin@example.com"
              from_email: "nas01@home.arpa"
            """
        ).strip()
    )

    cfg = load_users_config(yaml_file)

    assert len(cfg.users) == 1
    u = cfg.users[0]
    assert u.username == "svc-automation"
    assert u.full_name == "Automation service account"
    assert u.shell == "/usr/sbin/nologin"
    assert u.sudo is False
    assert u.ssh_keys == ("ssh-ed25519 AAAA... test@host",)

    assert cfg.email_alerts.admin_email == "admin@example.com"
    assert cfg.email_alerts.from_email == "nas01@home.arpa"


def test_load_users_config_empty_file(tmp_path: Path) -> None:
    from truenas_infra.modules.users import load_users_config

    yaml_file = tmp_path / "empty.yaml"
    yaml_file.write_text("")

    cfg = load_users_config(yaml_file)

    assert cfg.users == ()
    assert cfg.email_alerts.admin_email == ""


def test_load_users_config_parses_ssh_section(tmp_path: Path) -> None:
    from truenas_infra.modules.users import load_users_config

    yaml_file = tmp_path / "users.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            ssh:
              enable: true
              password_auth: false
            """
        ).strip()
    )

    cfg = load_users_config(yaml_file)

    assert cfg.ssh.enable is True
    assert cfg.ssh.password_auth is False


# ─── ensure_user ─────────────────────────────────────────────────────────────


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


def test_ensure_user_creates_when_missing() -> None:
    from truenas_infra.modules.users import UserSpec, ensure_user

    # user.query returns empty (user does not exist); user.create succeeds.
    cli = _mk_cli([
        [],
        {"id": 1001, "uid": 1001, "username": "svc-automation"},
    ])

    spec = UserSpec(username="svc-automation", full_name="Automation service")

    diff = ensure_user(cli, spec, apply=True)

    assert diff.changed is True
    assert diff.action == "create"
    # We must query before creating.
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names[0] == "user.query"
    assert "user.create" in call_names


def test_ensure_user_dry_run_does_not_create() -> None:
    from truenas_infra.modules.users import UserSpec, ensure_user

    cli = _mk_cli([[]])  # only the query; no writes expected

    spec = UserSpec(username="svc-automation")

    diff = ensure_user(cli, spec, apply=False)

    assert diff.changed is True
    assert diff.action == "create"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["user.query"]  # ONLY the query — no user.create


def _existing_user(
    *,
    username: str = "svc-automation",
    full_name: str = "Automation service account",
    shell: str = "/usr/sbin/nologin",
    sshpubkey: str | None = None,
    password_disabled: bool = True,
    id_: int = 72,
) -> dict:
    return {
        "id": id_,
        "uid": 3000,
        "username": username,
        "full_name": full_name,
        "shell": shell,
        "home": "/var/empty",
        "sshpubkey": sshpubkey,
        "password_disabled": password_disabled,
        "locked": False,
        "builtin": False,
        "immutable": False,
    }


def test_ensure_user_noop_when_match() -> None:
    from truenas_infra.modules.users import UserSpec, ensure_user

    existing = _existing_user(
        username="svc-automation",
        full_name="Automation service account",
        sshpubkey="ssh-ed25519 AAAA... test@host",
        password_disabled=True,
    )
    cli = _mk_cli([[existing]])

    spec = UserSpec(
        username="svc-automation",
        full_name="Automation service account",
        shell="/usr/sbin/nologin",
        ssh_keys=("ssh-ed25519 AAAA... test@host",),
    )

    diff = ensure_user(cli, spec, apply=True)

    assert diff.changed is False
    assert diff.action == "noop"
    # Only the query ran; no user.update.
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["user.query"]


def test_ensure_user_updates_when_sshpubkey_differs() -> None:
    from truenas_infra.modules.users import UserSpec, ensure_user

    existing = _existing_user(
        sshpubkey=None,            # no keys installed yet
        password_disabled=False,   # and password auth still on
    )
    updated = {**existing, "sshpubkey": "ssh-ed25519 AAAA... gunrak@laptop", "password_disabled": True}
    cli = _mk_cli([[existing], updated])

    spec = UserSpec(
        username="svc-automation",
        full_name="Automation service account",
        ssh_keys=("ssh-ed25519 AAAA... gunrak@laptop",),
    )

    diff = ensure_user(cli, spec, apply=True)

    assert diff.changed is True
    assert diff.action == "update"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names[0] == "user.query"
    assert "user.update" in call_names
    # user.update should target the existing user's id (72).
    update_call = next(c for c in cli.call.call_args_list if c.args[0] == "user.update")
    assert update_call.args[1] == 72


def test_ensure_user_update_dry_run_no_write() -> None:
    from truenas_infra.modules.users import UserSpec, ensure_user

    existing = _existing_user(sshpubkey=None)
    cli = _mk_cli([[existing]])

    spec = UserSpec(
        username="svc-automation",
        ssh_keys=("ssh-ed25519 AAAA... gunrak@laptop",),
    )

    diff = ensure_user(cli, spec, apply=False)

    assert diff.changed is True
    assert diff.action == "update"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["user.query"]


# ─── ensure_ssh_service ──────────────────────────────────────────────────────


def test_ensure_ssh_service_noop_when_match() -> None:
    from truenas_infra.modules.users import SshServiceSpec, ensure_ssh_service

    live_config = {"id": 1, "passwordauth": True, "tcpport": 22}
    live_service = [{"id": 11, "service": "ssh", "enable": True, "state": "RUNNING"}]
    cli = _mk_cli([live_config, live_service])

    spec = SshServiceSpec(enable=True, password_auth=True)
    diff = ensure_ssh_service(cli, spec, apply=True)

    assert diff.changed is False
    assert diff.action == "noop"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    # We queried; we did NOT write.
    assert "ssh.update" not in call_names
    assert "service.update" not in call_names


def test_ensure_ssh_service_disables_password_auth() -> None:
    from truenas_infra.modules.users import SshServiceSpec, ensure_ssh_service

    live_config = {"id": 1, "passwordauth": True, "tcpport": 22}
    live_service = [{"id": 11, "service": "ssh", "enable": True, "state": "RUNNING"}]
    updated_config = {**live_config, "passwordauth": False}
    cli = _mk_cli([live_config, live_service, updated_config])

    spec = SshServiceSpec(enable=True, password_auth=False)
    diff = ensure_ssh_service(cli, spec, apply=True)

    assert diff.changed is True
    assert diff.action == "update"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "ssh.update" in call_names
    ssh_update = next(c for c in cli.call.call_args_list if c.args[0] == "ssh.update")
    assert ssh_update.args[1]["passwordauth"] is False


def test_ensure_ssh_service_enables_disabled_service() -> None:
    from truenas_infra.modules.users import SshServiceSpec, ensure_ssh_service

    live_config = {"id": 1, "passwordauth": True}
    live_service = [{"id": 11, "service": "ssh", "enable": False, "state": "STOPPED"}]
    cli = _mk_cli([
        live_config,
        live_service,
        True,   # service.update response
        True,   # service.start response
    ])

    spec = SshServiceSpec(enable=True, password_auth=True)
    diff = ensure_ssh_service(cli, spec, apply=True)

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "service.update" in call_names
    assert "service.start" in call_names


def test_ensure_ssh_service_dry_run_no_writes() -> None:
    from truenas_infra.modules.users import SshServiceSpec, ensure_ssh_service

    live_config = {"id": 1, "passwordauth": True}
    live_service = [{"id": 11, "service": "ssh", "enable": False, "state": "STOPPED"}]
    cli = _mk_cli([live_config, live_service])

    spec = SshServiceSpec(enable=True, password_auth=False)
    diff = ensure_ssh_service(cli, spec, apply=False)

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    # Only reads; no writes.
    assert all(name in ("ssh.config", "service.query") for name in call_names)


# ─── ensure_email_alerts ─────────────────────────────────────────────────────


def test_ensure_email_alerts_noop_when_both_fields_empty() -> None:
    from truenas_infra.modules.users import EmailAlertsSpec, ensure_email_alerts

    cli = _mk_cli([])  # no calls expected — spec is empty, skip
    spec = EmailAlertsSpec(admin_email="", from_email="")

    diff = ensure_email_alerts(cli, spec, apply=True)

    assert diff.changed is False
    assert cli.call.call_count == 0


def test_ensure_email_alerts_updates_fromemail() -> None:
    from truenas_infra.modules.users import EmailAlertsSpec, ensure_email_alerts

    live = {"id": 1, "fromemail": "", "fromname": "", "outgoingserver": "", "port": 25}
    updated = {**live, "fromemail": "nas01@home.arpa"}
    cli = _mk_cli([live, updated])

    spec = EmailAlertsSpec(admin_email="", from_email="nas01@home.arpa")
    diff = ensure_email_alerts(cli, spec, apply=True)

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["mail.config", "mail.update"]
    update_call = next(c for c in cli.call.call_args_list if c.args[0] == "mail.update")
    assert update_call.args[-1]["fromemail"] == "nas01@home.arpa"


def test_ensure_email_alerts_noop_when_already_set() -> None:
    from truenas_infra.modules.users import EmailAlertsSpec, ensure_email_alerts

    live = {"id": 1, "fromemail": "nas01@home.arpa", "fromname": "", "outgoingserver": ""}
    cli = _mk_cli([live])

    spec = EmailAlertsSpec(from_email="nas01@home.arpa")
    diff = ensure_email_alerts(cli, spec, apply=True)

    assert diff.changed is False
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["mail.config"]  # query only


def test_ensure_email_alerts_dry_run_no_writes() -> None:
    from truenas_infra.modules.users import EmailAlertsSpec, ensure_email_alerts

    live = {"id": 1, "fromemail": "", "fromname": "", "outgoingserver": ""}
    cli = _mk_cli([live])

    spec = EmailAlertsSpec(from_email="nas01@home.arpa")
    diff = ensure_email_alerts(cli, spec, apply=False)

    assert diff.changed is True
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "mail.update" not in call_names


# ─── run() orchestration ─────────────────────────────────────────────────────


class _Ctx:
    """Minimal fake Context matching the shape cli.Context exposes."""

    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        import structlog
        self.log = structlog.get_logger("test")


def test_run_invokes_each_ensure_and_returns_zero(tmp_path: Path) -> None:
    from truenas_infra.modules.users import run

    cfg_path = tmp_path / "users.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            users:
              - username: svc-automation
                full_name: "Automation service account"
                shell: /usr/sbin/nologin
                ssh_keys:
                  - "ssh-ed25519 AAAA... test@host"

            ssh:
              enable: true
              password_auth: true

            email_alerts:
              from_email: "nas01@home.arpa"
            """
        ).strip()
    )

    # Responses — everything is already in desired state so each ensure_* is a noop.
    existing_user = _existing_user(sshpubkey="ssh-ed25519 AAAA... test@host")
    cli = _mk_cli([
        [existing_user],                                                                    # user.query
        {"id": 1, "passwordauth": True},                                                    # ssh.config
        [{"id": 11, "service": "ssh", "enable": True, "state": "RUNNING"}],                 # service.query
        {"id": 1, "fromemail": "nas01@home.arpa", "fromname": "", "outgoingserver": ""},    # mail.config
    ])

    ctx = _Ctx(apply=False)
    rc = run(cli, ctx, only=None, config_path=cfg_path)

    assert rc == 0
    # Reads happened; no writes (we're in noop / dry-run mode).
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "user.query" in call_names
    assert "ssh.config" in call_names
    assert "mail.config" in call_names
    assert "user.update" not in call_names
    assert "ssh.update" not in call_names
    assert "mail.update" not in call_names


def test_run_processes_multiple_users(tmp_path: Path) -> None:
    from truenas_infra.modules.users import run

    cfg_path = tmp_path / "users.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            users:
              - username: svc-automation
                full_name: "Automation service account"
              - username: upsclient
                full_name: "NUT read-only client"

            ssh:
              enable: true
              password_auth: true

            email_alerts: {}
            """
        ).strip()
    )

    existing_auto = _existing_user(username="svc-automation", full_name="Automation service account")
    existing_ups = _existing_user(username="upsclient", full_name="NUT read-only client", id_=73)

    cli = _mk_cli([
        [existing_auto],
        [existing_ups],
        {"id": 1, "passwordauth": True},
        [{"id": 11, "service": "ssh", "enable": True, "state": "RUNNING"}],
        # no mail.config call — email_alerts is empty so we skip
    ])

    ctx = _Ctx(apply=False)
    rc = run(cli, ctx, only=None, config_path=cfg_path)

    assert rc == 0
    # Exactly two user.query calls — one per user.
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names.count("user.query") == 2

