"""Phase: users — local users, SSH keys, email alerts.

See docs/plans/zesty-drifting-castle.md §Phase 1.
"""

from __future__ import annotations

import os
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
    """TrueNAS `mail.config` desired state (and the operator's alert inbox).

    Maps to `mail.update` fields:
      fromemail    ↔ from_email
      fromname     ↔ from_name
      outgoingserver ↔ smtp_host
      port         ↔ smtp_port
      security     ↔ smtp_security   ("PLAIN" | "SSL" | "TLS")
      smtp         ↔ smtp_auth       (bool — "SMTP Authentication" checkbox)
      user         ↔ env[smtp_user_env]
      pass         ↔ env[smtp_password_env]

    The username + password are **never** in YAML — they come from environment
    variables that manage.sh exports from Doppler (default Doppler keys for the
    cluster's AWS SES sender are `SHARED_SES_W1_SMTP_USERNAME` +
    `SHARED_SES_W1_SMTP_PASSWORD`). This is the same SES identity Alertmanager
    + Flux use in the kube clusters — single rotation point in Doppler covers
    NAS-side and cluster-side mail.

    `admin_email` is the AlertService destination — TrueNAS reads the `email`
    field from local-admin user records and routes system alerts (+ the
    "Send Test Mail" button's target) to whichever admin user has one set.
    `ensure_admin_recipient_email` (below) syncs `admin_email` onto the user
    identified by `admin_username` (default `truenas_admin`, the chart-default
    25.10 admin account documented in CLAUDE.md).
    """

    admin_email: str = ""
    admin_username: str = "truenas_admin"
    from_email: str = ""
    from_name: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: str = "TLS"
    smtp_auth: bool = False
    smtp_user_env: str = ""
    smtp_password_env: str = ""


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
    smtp_raw = email_raw.get("smtp") or {}
    email = EmailAlertsSpec(
        admin_email=email_raw.get("admin_email", ""),
        admin_username=email_raw.get("admin_username", "truenas_admin"),
        from_email=email_raw.get("from_email", ""),
        from_name=email_raw.get("from_name", ""),
        smtp_host=smtp_raw.get("host", ""),
        smtp_port=int(smtp_raw.get("port", 587)),
        smtp_security=str(smtp_raw.get("security", "TLS")).upper(),
        smtp_auth=bool(smtp_raw.get("auth", False)),
        smtp_user_env=smtp_raw.get("user_env", ""),
        smtp_password_env=smtp_raw.get("password_env", ""),
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
    """Ensure `mail.config` matches the spec (From + SMTP server + auth).

    Manages every field the TrueNAS UI's Email Options panel exposes when
    `Send Mail Method=SMTP`:
      fromemail, fromname, outgoingserver, port, security, smtp,
      user (from env), pass (from env)

    Strategy for the auth pair (`user` / `pass`):
      * `user` is plaintext in `mail.config`; we diff it like any other field.
      * `pass` is **never** echoed back by `mail.config` (TrueNAS returns ""
        or the literal redacted form depending on version), so a clean diff is
        impossible. We follow the same pattern as `nut.py`'s monpwd: set the
        password **only on the first apply** (live `pass` empty) OR when we're
        already writing other auth-related drift in the same call. To rotate
        the password, edit Doppler then clear the live password in the UI
        (Email Options → Password → blank, Save) and re-run; the empty live
        value triggers a fresh write from the env var.

    Skip behavior: if every from_* / smtp_host is empty, this is a no-op — for
    fresh installs that haven't decided on a sender yet.
    """
    # Nothing to do if the spec has nothing to say.
    if not (spec.from_email or spec.admin_email or spec.smtp_host):
        return Diff.noop({})

    live = cli.call("mail.config")

    changes: dict[str, Any] = {}
    if spec.from_email and live.get("fromemail") != spec.from_email:
        changes["fromemail"] = spec.from_email
    if spec.from_name and live.get("fromname") != spec.from_name:
        changes["fromname"] = spec.from_name

    if spec.smtp_host:
        if live.get("outgoingserver") != spec.smtp_host:
            changes["outgoingserver"] = spec.smtp_host
        if int(live.get("port") or 0) != spec.smtp_port:
            changes["port"] = spec.smtp_port
        if str(live.get("security") or "").upper() != spec.smtp_security:
            changes["security"] = spec.smtp_security
        if bool(live.get("smtp")) != spec.smtp_auth:
            changes["smtp"] = spec.smtp_auth

        if spec.smtp_auth:
            smtp_user = os.environ.get(spec.smtp_user_env, "") if spec.smtp_user_env else ""
            smtp_pass = os.environ.get(spec.smtp_password_env, "") if spec.smtp_password_env else ""

            if smtp_user and live.get("user") != smtp_user:
                changes["user"] = smtp_user

            # Password handled separately: mail.config returns it redacted
            # (or empty pre-set) so we can't diff. Same idiom as nut.py monpwd —
            # set from env only when live shows empty (first-time bootstrap),
            # OR when we're already writing other auth-related drift in the
            # same call (e.g. rotating the access key, server change). To
            # rotate just the password without changing the user: clear it in
            # the UI (Email Options → Password → blank → Save) and re-run,
            # the empty live triggers a fresh write from Doppler.
            auth_drift_keys = {"outgoingserver", "port", "security", "smtp", "user"}
            writing_auth_drift = bool(changes.keys() & auth_drift_keys)
            live_pass_empty = not live.get("pass")
            if smtp_pass and (live_pass_empty or writing_auth_drift):
                changes["pass"] = smtp_pass

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("mail.update", changes)
        return Diff.update(before=live, after=updated)
    # Redact secrets in the projected diff so dry-run output doesn't leak.
    projected_changes = dict(changes)
    if "pass" in projected_changes:
        projected_changes["pass"] = "***"
    return Diff.update(before=live, after={**live, **projected_changes})


# ─── ensure_admin_recipient_email ────────────────────────────────────────────


def ensure_admin_recipient_email(
    cli: Any, *, admin_username: str, admin_email: str, apply: bool,
) -> Diff:
    """Set the local-admin user's `email` field — TrueNAS's AlertService and
    'Send Test Mail' button both refuse to deliver until at least one local
    administrator has a non-empty `email` set. The dialog text is literal:
    "No e-mail address is set for root user or any other local administrator."

    Skip behavior: no-op when admin_email is empty (keep YAML's TODO state
    benign for fresh installs).

    Missing-user behavior: returns a noop with a warning marker rather than
    raising — the chart-default admin username can vary across TrueNAS
    versions, and we don't want phase users to hard-fail on a fresh box.
    Operator override via `email_alerts.admin_username` in YAML.
    """
    if not admin_email:
        return Diff.noop({})

    existing = cli.call("user.query", [["username", "=", admin_username]])
    if not existing:
        return Diff.noop({
            "skipped": True,
            "reason": f"admin user {admin_username!r} not found",
        })

    user = existing[0]
    if user.get("email") == admin_email:
        return Diff.noop(user)

    if apply:
        updated = cli.call("user.update", user["id"], {"email": admin_email})
        return Diff.update(before=user, after=updated)
    return Diff.update(before=user, after={**user, "email": admin_email})


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

        # AlertService recipient — set the admin user's email so TrueNAS's
        # 'Send Test Mail' button + system alert routing have a destination.
        diff = ensure_admin_recipient_email(
            cli,
            admin_username=cfg.email_alerts.admin_username,
            admin_email=cfg.email_alerts.admin_email,
            apply=ctx.apply,
        )
        log.info(
            "admin_recipient_email_ensured",
            admin_username=cfg.email_alerts.admin_username,
            action=diff.action,
            changed=diff.changed,
        )

    return 0
