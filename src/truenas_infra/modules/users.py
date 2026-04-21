"""Phase: users — local users, SSH keys, email alerts.

See docs/plans/zesty-drifting-castle.md §Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class UserSpec:
    username: str
    full_name: str = ""
    shell: str = "/usr/sbin/nologin"
    sudo: bool = False
    ssh_keys: tuple[str, ...] = ()
    password_env: str | None = None


@dataclass(frozen=True)
class EmailAlertsSpec:
    admin_email: str = ""
    from_email: str = ""


@dataclass(frozen=True)
class SshServiceSpec:
    enable: bool = True
    password_auth: bool = True  # phase 1 keeps it on; flip to False once keys are installed


@dataclass(frozen=True)
class UsersConfig:
    users: tuple[UserSpec, ...] = ()
    ssh: SshServiceSpec = field(default_factory=SshServiceSpec)
    email_alerts: EmailAlertsSpec = field(default_factory=EmailAlertsSpec)


def load_users_config(path: Path) -> UsersConfig:
    """Parse config/users.yaml into a typed, immutable config object."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    users = tuple(
        UserSpec(
            username=u["username"],
            full_name=u.get("full_name", ""),
            shell=u.get("shell", "/usr/sbin/nologin"),
            sudo=bool(u.get("sudo", False)),
            ssh_keys=tuple(u.get("ssh_keys") or ()),
            password_env=u.get("password_env"),
        )
        for u in (raw.get("users") or [])
    )

    ssh_raw = raw.get("ssh") or {}
    ssh = SshServiceSpec(
        enable=bool(ssh_raw.get("enable", True)),
        password_auth=bool(ssh_raw.get("password_auth", True)),
    )

    email_raw = raw.get("email_alerts") or {}
    email = EmailAlertsSpec(
        admin_email=email_raw.get("admin_email", ""),
        from_email=email_raw.get("from_email", ""),
    )

    return UsersConfig(users=users, ssh=ssh, email_alerts=email)


# ─── ensure_user ─────────────────────────────────────────────────────────────


def _user_create_payload(spec: UserSpec) -> dict[str, Any]:
    """Build the payload for user.create from a UserSpec."""
    return {
        "username": spec.username,
        "full_name": spec.full_name,
        "shell": spec.shell,
        "group_create": True,
        "home": "/var/empty",
        "password_disabled": True,
        "sshpubkey": "\n".join(spec.ssh_keys) if spec.ssh_keys else "",
    }


# Fields we actively manage on an existing user. Anything else (home, groups,
# uid, etc.) is left untouched so manual operator changes survive.
_MANAGED_FIELDS: tuple[str, ...] = (
    "full_name",
    "shell",
    "password_disabled",
    "sshpubkey",
)


def _desired_from_spec(spec: UserSpec) -> dict[str, Any]:
    return {
        "full_name": spec.full_name,
        "shell": spec.shell,
        "password_disabled": True,
        "sshpubkey": "\n".join(spec.ssh_keys) if spec.ssh_keys else None,
    }


def _diff_fields(existing: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields where existing disagrees with desired."""
    changes: dict[str, Any] = {}
    for key, desired_val in desired.items():
        current = existing.get(key)
        # TrueNAS returns sshpubkey as None when empty; desired is "" or None.
        # Normalise empty-string and None as equivalent.
        if (current in (None, "")) and (desired_val in (None, "")):
            continue
        if current != desired_val:
            changes[key] = desired_val
    return changes


def ensure_user(cli: Any, spec: UserSpec, *, apply: bool) -> Diff:
    """Ensure a local user matching `spec` exists. Idempotent.

    Returns a Diff describing the change (or noop). Compares only the fields
    this module manages; other fields on the user are left alone.
    """
    existing = cli.call("user.query", [["username", "=", spec.username]])

    if not existing:
        payload = _user_create_payload(spec)
        if apply:
            created = cli.call("user.create", payload)
            return Diff.create(created)
        return Diff.create(payload)

    user = existing[0]
    desired = _desired_from_spec(spec)
    changes = _diff_fields(user, desired)

    if not changes:
        return Diff.noop(user)

    if apply:
        updated = cli.call("user.update", user["id"], changes)
        return Diff.update(before=user, after=updated)
    projected = {**user, **changes}
    return Diff.update(before=user, after=projected)


# ─── ensure_ssh_service ──────────────────────────────────────────────────────


def ensure_ssh_service(cli: Any, spec: SshServiceSpec, *, apply: bool) -> Diff:
    """Ensure the SSH service matches the desired state.

    Covers two concerns:
      1. `ssh.config` — passwordauth setting
      2. `service.query` — service enabled + running state

    Does NOT touch `bindiface` yet; phase 2 (network) will set that once the
    mgmt interface name is known.
    """
    config = cli.call("ssh.config")
    service = cli.call("service.query", [["service", "=", "ssh"]])

    before = {
        "passwordauth": config.get("passwordauth"),
        "enable": service[0]["enable"] if service else False,
        "state": service[0]["state"] if service else "STOPPED",
    }
    desired = {
        "passwordauth": spec.password_auth,
        "enable": spec.enable,
        "state": "RUNNING" if spec.enable else "STOPPED",
    }

    config_changes: dict[str, Any] = {}
    if config.get("passwordauth") != spec.password_auth:
        config_changes["passwordauth"] = spec.password_auth

    need_service_update = service and service[0]["enable"] != spec.enable
    need_service_start = spec.enable and (not service or service[0]["state"] != "RUNNING")
    need_service_stop = not spec.enable and service and service[0]["state"] == "RUNNING"

    if not config_changes and not need_service_update and not need_service_start and not need_service_stop:
        return Diff.noop(before)

    if apply:
        if config_changes:
            cli.call("ssh.update", config_changes)
        if need_service_update:
            cli.call("service.update", service[0]["id"], {"enable": spec.enable})
        if need_service_start:
            cli.call("service.start", "ssh")
        elif need_service_stop:
            cli.call("service.stop", "ssh")
    return Diff.update(before=before, after=desired)


# ─── ensure_email_alerts ─────────────────────────────────────────────────────


def ensure_email_alerts(cli: Any, spec: EmailAlertsSpec, *, apply: bool) -> Diff:
    """Ensure basic mail config (fromemail) matches the spec.

    Scope: sets `fromemail` on mail.config when present. Full SMTP server +
    alertservice destinations are deferred until the user has a real SMTP
    endpoint configured. Skipped entirely if all fields are empty.
    """
    # Nothing to do if the spec has nothing to say.
    if not spec.from_email and not spec.admin_email:
        return Diff.noop({})

    live = cli.call("mail.config")

    changes: dict[str, Any] = {}
    if spec.from_email and live.get("fromemail") != spec.from_email:
        changes["fromemail"] = spec.from_email

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("mail.update", changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/users.yaml")


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
) -> int:
    """Phase 1: users, SSH, email alerts.

    Reads `config/users.yaml`, then calls ensure_user for each, plus
    ensure_ssh_service and ensure_email_alerts. Idempotent — safe to re-run.
    """
    log = ctx.log.bind(phase="users")
    cfg = load_users_config(config_path or DEFAULT_CONFIG_PATH)

    # Users.
    for user_spec in cfg.users:
        if only and user_spec.username != only:
            continue
        diff = ensure_user(cli, user_spec, apply=ctx.apply)
        log.info(
            "user_ensured",
            username=user_spec.username,
            action=diff.action,
            changed=diff.changed,
        )

    # SSH service.
    if not only or only == "ssh":
        diff = ensure_ssh_service(cli, cfg.ssh, apply=ctx.apply)
        log.info("ssh_service_ensured", action=diff.action, changed=diff.changed)

    # Email alerts.
    if not only or only == "email":
        diff = ensure_email_alerts(cli, cfg.email_alerts, apply=ctx.apply)
        log.info("email_alerts_ensured", action=diff.action, changed=diff.changed)

    return 0
