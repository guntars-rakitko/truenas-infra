"""Phase: tunables — kernel boot args + TrueNAS tunables (ZFS / sysctl / udev).

Two kinds of config:
  * `kernel_extra_options` — set via `system.advanced.update`; reboot-only.
  * `tunables:` — ZFS module params, sysctls, and udev rules, reconciled via
    the `tunable.*` API by `ensure_tunables`. ZFS/sysctl apply live; udev rules
    fire on the next device add (`udevadm trigger` / reboot re-applies them).

**Why this phase exists**: on the Beelink ME Mini the shared 3.3V M.2 rail sags
under peak *simultaneous* NVMe current and drops a drive off the PCIe bus. The
drives are fine. The kernel args disable aggressive PCIe ASPM / NVMe APST; the
`tunables:` entries cap and spread peak rail current (ZFS write-concurrency +
an NVMe PS2 power-state udev cap). See CLAUDE.md § NVMe 3.3V-rail mitigations.
These are probability-reducers against an under-provisioned rail, not a cure.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


@dataclass(frozen=True)
class TunableSpec:
    """A single TrueNAS tunable (System > Advanced), applied via `tunable.*`.

    `type` is one of ZFS (kernel module parameter, e.g. `zfs_txg_timeout`),
    SYSCTL (a sysctl name), or UDEV (`var` is a udev rules-file name, `.rules`
    appended automatically; `value` is the file contents).
    """

    type: str
    var: str
    value: str
    comment: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class TunablesConfig:
    kernel_extra_options: tuple[str, ...] = ()
    timezone: str = ""
    ntp_servers: tuple[str, ...] = ()
    tunables: tuple[TunableSpec, ...] = ()


def load_tunables_config(path: Path) -> TunablesConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    kernel = raw.get("kernel") or {}
    system = raw.get("system") or {}
    return TunablesConfig(
        kernel_extra_options=tuple(kernel.get("extra_options") or ()),
        timezone=system.get("timezone", ""),
        ntp_servers=tuple(system.get("ntp_servers") or ()),
        tunables=tuple(
            TunableSpec(
                type=str(t["type"]).upper(),
                var=str(t["var"]),
                value=str(t["value"]),
                comment=str(t.get("comment", "")),
                enabled=bool(t.get("enabled", True)),
            )
            for t in (raw.get("tunables") or [])
        ),
    )


def ensure_kernel_extra_options(
    cli: Any, *, options: tuple[str, ...], apply: bool
) -> Diff:
    """Ensure the kernel cmdline contains all `options`.

    TrueNAS stores this as a single space-separated string; we normalise both
    sides to a set-equality comparison so order on disk doesn't trigger an
    unnecessary write.
    """
    live = cli.call("system.advanced.config")
    current = (live.get("kernel_extra_options") or "").split()
    desired = list(options)

    if sorted(current) == sorted(desired):
        return Diff.noop(live)

    new_value = " ".join(desired)
    if apply:
        updated = cli.call("system.advanced.update", {"kernel_extra_options": new_value})
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, "kernel_extra_options": new_value})


# ─── ensure_tunables ─────────────────────────────────────────────────────────


def ensure_tunables(cli: Any, *, specs: tuple[TunableSpec, ...], apply: bool) -> Diff:
    """Reconcile TrueNAS tunables (ZFS module params, sysctls, udev rules).

    Matches live tunables by ``(type, var)``. Creates missing ones and updates
    any whose value/comment/enabled drifted. Does **not** delete tunables it
    doesn't manage — only the declared set is reconciled, so hand-added
    tunables survive. Idempotent.

    ZFS/SYSCTL params apply live on create/update; UDEV rules are written to
    ``/etc/udev/rules.d`` and fire on the next device add (a ``udevadm trigger``
    or reboot re-applies them to already-present devices).
    """
    existing = cli.call("tunable.query")
    by_key = {(t["type"], t["var"]): t for t in existing}

    created: list[str] = []
    updated: list[str] = []
    for spec in specs:
        cur = by_key.get((spec.type, spec.var))
        if cur is None:
            created.append(spec.var)
            if apply:
                cli.call(
                    "tunable.create",
                    {
                        "type": spec.type,
                        "var": spec.var,
                        "value": spec.value,
                        "comment": spec.comment,
                        "enabled": spec.enabled,
                    },
                )
        elif (
            cur.get("value") != spec.value
            or cur.get("comment") != spec.comment
            or bool(cur.get("enabled", True)) != spec.enabled
        ):
            updated.append(spec.var)
            if apply:
                cli.call(
                    "tunable.update",
                    cur["id"],
                    {
                        "value": spec.value,
                        "comment": spec.comment,
                        "enabled": spec.enabled,
                    },
                )

    if not created and not updated:
        return Diff.noop(existing)
    return Diff.update(before=existing, after={"created": created, "updated": updated})


# ─── ensure_timezone ─────────────────────────────────────────────────────────


def ensure_timezone(cli: Any, *, timezone: str, apply: bool) -> Diff:
    """Ensure system timezone matches `timezone` (e.g. 'UTC')."""
    live = cli.call("system.general.config")
    if live.get("timezone") == timezone:
        return Diff.noop(live)
    if apply:
        updated = cli.call("system.general.update", {"timezone": timezone})
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, "timezone": timezone})


# ─── ensure_ntp_servers ──────────────────────────────────────────────────────


def ensure_ntp_servers(cli: Any, *, addresses: tuple[str, ...], apply: bool) -> Diff:
    """Ensure the configured NTP servers exactly match `addresses`.

    Creates missing ones, deletes stale ones. Idempotent.
    """
    existing = cli.call("system.ntpserver.query")
    existing_by_addr = {s["address"]: s for s in existing}

    to_create = [a for a in addresses if a not in existing_by_addr]
    to_delete = [s for a, s in existing_by_addr.items() if a not in addresses]

    if not to_create and not to_delete:
        return Diff.noop(existing)

    if apply:
        for addr in to_create:
            cli.call("system.ntpserver.create", {"address": addr})
        for s in to_delete:
            cli.call("system.ntpserver.delete", s["id"])
    return Diff.update(
        before=existing,
        after={"created": to_create, "deleted": [s["address"] for s in to_delete]},
    )


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/tunables.yaml")


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
) -> int:
    log = ctx.log.bind(phase="tunables")
    cfg = load_tunables_config(config_path or DEFAULT_CONFIG_PATH)

    diff = ensure_kernel_extra_options(
        cli, options=cfg.kernel_extra_options, apply=ctx.apply,
    )
    log.info(
        "kernel_extra_options_ensured",
        options=list(cfg.kernel_extra_options),
        action=diff.action,
        changed=diff.changed,
    )
    if diff.changed and ctx.apply:
        log.warning("reboot_required", reason="kernel_extra_options takes effect after reboot")

    # ZFS module params / sysctls / udev rules (apply live; no reboot needed)
    if cfg.tunables:
        diff = ensure_tunables(cli, specs=cfg.tunables, apply=ctx.apply)
        log.info(
            "tunables_ensured",
            count=len(cfg.tunables),
            action=diff.action,
            changed=diff.changed,
        )

    # Timezone
    if cfg.timezone:
        diff = ensure_timezone(cli, timezone=cfg.timezone, apply=ctx.apply)
        log.info("timezone_ensured", timezone=cfg.timezone,
                 action=diff.action, changed=diff.changed)

    # NTP servers
    if cfg.ntp_servers:
        diff = ensure_ntp_servers(cli, addresses=cfg.ntp_servers, apply=ctx.apply)
        log.info("ntp_servers_ensured", addresses=list(cfg.ntp_servers),
                 action=diff.action, changed=diff.changed)

    return 0
