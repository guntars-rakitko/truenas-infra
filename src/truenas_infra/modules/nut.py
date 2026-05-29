"""Phase: nut — built-in UPS/NUT service (1x APC Smart-UPS).

See docs/plans/zesty-drifting-castle.md §Phase 8.

TrueNAS NUT is singleton-configured via `ups.config` / `ups.update`:
single master UPS with configurable driver, port, monitor-user etc.
Multi-UPS setups are not in scope.

**Not a container** — TrueNAS runs NUT natively.

Reachability (VLAN 5 management only) is not a `ups.config` field in 25.10.
We rely on:
  1. NAS network config — mgmt only listens on 10.10.5.10 for other svcs
  2. mikrotik-infra firewall — drop 3493/tcp crossing VLANs 10/15/20 → 5

Kube nodes (NUT clients) reach `10.10.5.10:3493` from their mgmt NIC (on
VLAN 5, same subnet — no routing needed).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtraUserSpec:
    """One additional NUT user beyond the primary `monuser`.

    Renders into a `[name]` block in upsd.users (via TrueNAS
    `ups.config.extrausers`). Password resolved from env at load time —
    never persisted in YAML.
    """
    name: str
    password: str          # from env (password_env in YAML) — empty if env missing
    actions: tuple[str, ...] = ()    # e.g. ("SET",)
    instcmds: tuple[str, ...] = ()   # e.g. ("ALL",) or ("test.battery.start.quick",)


@dataclass(frozen=True)
class UpsThresholdsSpec:
    """UPS firmware HID thresholds — stored IN the UPS hardware.

    Drift-detection only; enforcement stays manual to keep the operator
    in the loop for UPS firmware writes (which can brick a unit if a
    bad value is set). See `wiki/docs/runbooks/ups-operations.md`.

    Fields are `None` when not declared in YAML → skipped in the drift
    check.
    """
    ups_delay_shutdown: int | None = None      # seconds
    ups_delay_start: int | None = None         # seconds
    battery_runtime_low: int | None = None     # seconds
    ups_test_interval: int | None = None       # seconds


@dataclass(frozen=True)
class NutSpec:
    enable: bool = True
    identifier: str = "ups"
    description: str = ""
    driver: str = ""             # "<driver>$<model>" e.g. "usbhid-ups$Smart-UPS (USB)"
    port: str = "auto"
    mode: str = "MASTER"         # MASTER | SLAVE
    remoteport: int = 3493
    rmonitor: bool = False       # Allow remote NUT clients to connect to upsd. Required
                                 # for the in-cluster nut-exporter (10.10.5.10:3493) plus
                                 # any K8s-side `upsmon` slaves. Without it upsd binds only
                                 # to 127.0.0.1 and remote queries fail with "Access denied".
                                 # Maps to TrueNAS `ups.config.rmonitor` (Remote Monitor
                                 # checkbox in the UI). Enabled live 2026-05-16.
    shutdown: str = "BATT"       # BATT | LOWBATT
    shutdowntimer: int = 30      # seconds
    powerdown: bool = False      # If true, NAS issues `shutdown.return` to UPS during its
                                 # own poweroff. Required for clean cluster shutdown chain
                                 # so UPS actually cuts power after cluster is off.
                                 # See services.yaml § nut.powerdown for full rationale.
    monuser: str = "upsmon"
    monpwd: str = ""             # NOT from YAML — read from TRUENAS_NUT_MONPWD env
    extra_users: tuple[ExtraUserSpec, ...] = ()
    ups_thresholds: UpsThresholdsSpec = field(default_factory=UpsThresholdsSpec)


def load_nut_config(path: Path) -> NutSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nut = raw.get("nut") or {}
    # monpwd is a secret — never persisted in YAML, only read from env.
    # manage.sh exports TRUENAS_NUT_MONPWD from Doppler. Without it, upsd
    # exits silently after start (no log output) because it can't bind the
    # upsmon user, leaving the service in STOPPED state despite a successful
    # `service.control START`. Discovered 2026-05-15 on the Beelink rebuild.

    # extra_users: each YAML entry declares password_env (env var name);
    # resolved here. Entries with missing env get empty password — they'll
    # be skipped in ensure_extra_users with a warning, so missing creds
    # don't poison upsd.users with a hash of an empty string.
    raw_extra = nut.get("extra_users") or []
    extra_users = tuple(
        ExtraUserSpec(
            name=u["name"],
            password=os.environ.get(u.get("password_env", ""), ""),
            actions=tuple(u.get("actions", [])),
            instcmds=tuple(u.get("instcmds", [])),
        )
        for u in raw_extra
    )

    raw_thresholds = nut.get("ups_thresholds") or {}
    ups_thresholds = UpsThresholdsSpec(
        ups_delay_shutdown=raw_thresholds.get("ups_delay_shutdown"),
        ups_delay_start=raw_thresholds.get("ups_delay_start"),
        battery_runtime_low=raw_thresholds.get("battery_runtime_low"),
        ups_test_interval=raw_thresholds.get("ups_test_interval"),
    )

    return NutSpec(
        enable=bool(nut.get("enable", True)),
        identifier=nut.get("identifier", "ups"),
        description=nut.get("description", ""),
        driver=nut.get("driver", ""),
        port=str(nut.get("port", "auto")),
        mode=str(nut.get("mode", "MASTER")).upper(),
        remoteport=int(nut.get("remoteport", 3493)),
        rmonitor=bool(nut.get("rmonitor", False)),
        shutdown=str(nut.get("shutdown", "BATT")).upper(),
        shutdowntimer=int(nut.get("shutdowntimer", 30)),
        powerdown=bool(nut.get("powerdown", False)),
        monuser=nut.get("monuser", "upsmon"),
        monpwd=os.environ.get("TRUENAS_NUT_MONPWD", ""),
        extra_users=extra_users,
        ups_thresholds=ups_thresholds,
    )


# ─── ensure_ups_config ───────────────────────────────────────────────────────


_MANAGED_UPS_FIELDS = (
    "identifier", "description", "driver", "port",
    "mode", "remoteport", "rmonitor", "shutdown", "shutdowntimer",
    "powerdown", "monuser",
)


def ensure_ups_config(cli: Any, *, spec: NutSpec, apply: bool) -> Diff:
    """Ensure `ups.config` matches `spec`."""
    live = cli.call("ups.config")

    desired = {
        "identifier": spec.identifier,
        "description": spec.description,
        "driver": spec.driver,
        "port": spec.port,
        "mode": spec.mode,
        "remoteport": spec.remoteport,
        "rmonitor": spec.rmonitor,
        "shutdown": spec.shutdown,
        "shutdowntimer": spec.shutdowntimer,
        "powerdown": spec.powerdown,
        "monuser": spec.monuser,
    }

    changes: dict[str, Any] = {}
    for k, v in desired.items():
        if live.get(k) != v:
            changes[k] = v

    # monpwd handled separately: ups.config always returns "" (the value
    # is never echoed back for security). So we can't detect a drift.
    # Strategy: set monpwd from env ONLY when live shows empty (first-time
    # bootstrap on a fresh install). After that, the password is treated
    # as immutable from this phase's perspective. To rotate, manually
    # clear monpwd via the UI (or `ups.update monpwd=""`) then re-run
    # phase nut — the empty value triggers re-set from Doppler.
    if spec.monpwd and not live.get("monpwd"):
        changes["monpwd"] = spec.monpwd

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("ups.update", changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── ensure_extra_users ──────────────────────────────────────────────────────


def _render_extrausers(users: tuple[ExtraUserSpec, ...]) -> str:
    """Render upsd.users content for the `[name]` blocks.

    Output goes into `ups.config.extrausers` and TrueNAS appends it to
    `/etc/nut/upsd.users` at NUT service start. Format mirrors
    upstream upsd.users syntax (4-space indent, one directive per line).
    """
    blocks: list[str] = []
    for u in users:
        lines = [f"[{u.name}]", f"    password = {u.password}"]
        for action in u.actions:
            lines.append(f"    actions = {action}")
        for instcmd in u.instcmds:
            lines.append(f"    instcmds = {instcmd}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def ensure_extra_users(cli: Any, *, spec: NutSpec, apply: bool) -> Diff:
    """Ensure `ups.config.extrausers` matches the rendered spec.

    Users with empty password (missing env var) are SKIPPED — emitting
    a hash of an empty string would leave the NUT user authenticatable
    with an empty password. The caller's log should surface this.

    A change here REQUIRES restarting the `ups` service for upsd to
    re-read the file. The caller (`run`) handles that orchestration.
    """
    # Filter out users with missing passwords — safer than committing
    # a hash of an empty string.
    valid_users = tuple(u for u in spec.extra_users if u.password)

    desired_extrausers = _render_extrausers(valid_users)
    live = cli.call("ups.config")
    live_extrausers = live.get("extrausers", "") or ""

    # Quick equality check on rendered string. If the operator manually
    # rotated a password via UI, this will diff back to YAML-sourced
    # value — intentional, Doppler is source of truth.
    if live_extrausers == desired_extrausers:
        return Diff.noop(_redact_extrausers(live_extrausers))

    if apply:
        cli.call("ups.update", {"extrausers": desired_extrausers})
        # Service restart is the caller's responsibility (one restart
        # batch all NUT changes together) — see `run()`.
    return Diff.update(
        before=_redact_extrausers(live_extrausers),
        after=_redact_extrausers(desired_extrausers),
    )


def _redact_extrausers(content: str) -> str:
    """Replace `password = <value>` lines with `password = <redacted>`
    for safe logging. The cleartext password stays in upsd.users on
    disk (by NUT design); we just don't put it in our log stream."""
    redacted_lines = []
    for line in (content or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("password = "):
            indent = line[: len(line) - len(stripped)]
            redacted_lines.append(f"{indent}password = <redacted>")
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)


# ─── check_ups_hid_thresholds (drift detection only) ─────────────────────────


_HID_VAR_TO_SPEC = {
    "ups.delay.shutdown": "ups_delay_shutdown",
    "ups.delay.start": "ups_delay_start",
    "battery.runtime.low": "battery_runtime_low",
    "ups.test.interval": "ups_test_interval",
}


@dataclass
class HidThresholdDrift:
    """One drifted HID variable: live vs desired."""
    hid_var: str
    desired: int
    live: int | None        # None = could not read (e.g. var not exposed)


def check_ups_hid_thresholds(
    spec: UpsThresholdsSpec,
    *,
    ssh_host: str | None = None,
    _upsc_reader: Any = None,
) -> list[HidThresholdDrift]:
    """Compare desired HID thresholds against the live UPS values.

    READ-ONLY by design. Returns the list of drift; the caller logs it.
    Enforcement stays manual to keep the operator in the loop for any
    UPS firmware write (which can brick the unit if a bad value is set).

    `_upsc_reader` is for tests: a callable that returns a dict of
    `{hid_var: str_value}`. In production, defaults to SSH+`upsc`.
    """
    desired_pairs = [
        (hid_var, getattr(spec, attr))
        for hid_var, attr in _HID_VAR_TO_SPEC.items()
        if getattr(spec, attr) is not None
    ]
    if not desired_pairs:
        return []

    if _upsc_reader is not None:
        live = _upsc_reader()
    elif ssh_host:
        live = _read_upsc(ssh_host)
    else:
        # No reader configured — caller running in offline/dry-run mode.
        # Return empty (no drift detected, no false positives).
        return []

    drifts: list[HidThresholdDrift] = []
    for hid_var, desired in desired_pairs:
        raw = live.get(hid_var)
        try:
            live_val = int(raw) if raw is not None else None
        except (ValueError, TypeError):
            live_val = None
        if live_val != desired:
            drifts.append(HidThresholdDrift(
                hid_var=hid_var, desired=desired, live=live_val,
            ))
    return drifts


def _read_upsc(ssh_host: str, ups: str = "apc1") -> dict[str, str]:
    """SSH to NAS, run `upsc apc1@localhost`, parse to dict.

    No auth needed for read — NUT exposes all vars to any local
    upsd connection by design. Connection is via the operator's
    SSH publickey (BatchMode=yes — fails fast if key missing).
    """
    try:
        result = subprocess.run(
            ["ssh", "-oBatchMode=yes", ssh_host,
             f"upsc {ups}@localhost"],
            capture_output=True, text=True, check=True, timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError):
        # Caller treats empty dict as "could not read" — no drift reported.
        return {}

    parsed = {}
    for line in result.stdout.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            parsed[key.strip()] = val.strip()
    return parsed


# ─── ensure_ups_service ──────────────────────────────────────────────────────


def ensure_ups_service(cli: Any, *, enable: bool, apply: bool) -> Diff:
    """Ensure the `ups` service is in the desired enable/running state."""
    live = cli.call("service.query", [["service", "=", "ups"]])
    if not live:
        raise RuntimeError("ups service not found in TrueNAS — unexpected")
    svc = live[0]

    need_update = svc["enable"] != enable
    need_start = enable and svc["state"] != "RUNNING"
    need_stop = not enable and svc["state"] == "RUNNING"

    if not need_update and not need_start and not need_stop:
        return Diff.noop(svc)

    if apply:
        if need_update:
            cli.call("service.update", svc["id"], {"enable": enable})
        if need_start:
            cli.call("service.start", "ups")
        elif need_stop:
            cli.call("service.stop", "ups")
    return Diff.update(before=svc, after={**svc, "enable": enable,
                                          "state": "RUNNING" if enable else "STOPPED"})


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/services.yaml")


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
) -> int:
    log = ctx.log.bind(phase="nut")
    cfg = load_nut_config(config_path or DEFAULT_CONFIG_PATH)

    diff = ensure_ups_config(cli, spec=cfg, apply=ctx.apply)
    log.info(
        "ups_config_ensured",
        identifier=cfg.identifier, driver=cfg.driver, port=cfg.port,
        action=diff.action, changed=diff.changed,
    )

    # Skipped users — emit a warning so missing creds aren't silent.
    missing_pwd = [u.name for u in cfg.extra_users if not u.password]
    if missing_pwd:
        log.warning("extra_users_missing_password", users=missing_pwd,
                    hint="set the corresponding password_env in Doppler ops")

    extra_diff = ensure_extra_users(cli, spec=cfg, apply=ctx.apply)
    log.info(
        "ups_extra_users_ensured",
        users=[u.name for u in cfg.extra_users if u.password],
        action=extra_diff.action, changed=extra_diff.changed,
    )

    diff = ensure_ups_service(cli, enable=cfg.enable, apply=ctx.apply)
    log.info(
        "ups_service_ensured",
        enable=cfg.enable, action=diff.action, changed=diff.changed,
    )

    # If extrausers changed AND we just applied, the running upsd has
    # the OLD upsd.users in memory. Restart picks up the new users.
    # (ups.update doesn't auto-restart in TrueNAS 25.10 for extrausers.)
    if extra_diff.changed and ctx.apply:
        cli.call("service.control", "RESTART", "ups")
        log.info("ups_service_restarted", reason="extrausers_changed")

    # HID-threshold drift detection (read-only, never enforces).
    ssh_host = os.environ.get("TRUENAS_HOST", "")  # e.g. "truenas_admin@10.10.5.10"
    drifts = check_ups_hid_thresholds(cfg.ups_thresholds, ssh_host=ssh_host)
    if drifts:
        for d in drifts:
            log.warning(
                "ups_hid_threshold_drift",
                hid_var=d.hid_var, desired=d.desired, live=d.live,
                fix=f"sudo upsrw -s {d.hid_var}={d.desired} -u upsadmin "
                    f"-p \"$TRUENAS_NUT_ADMINPWD\" {cfg.identifier}@localhost",
            )
    else:
        log.info("ups_hid_thresholds_in_sync",
                 declared=len(_HID_VAR_TO_SPEC),
                 checked=sum(1 for v in vars(cfg.ups_thresholds).values()
                             if v is not None))

    return 0
