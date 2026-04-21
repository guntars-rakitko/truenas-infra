"""Phase: tunables — kernel boot args + (future) sysctl tunables.

Kernel boot args are set via `system.advanced.update({"kernel_extra_options": "..."})`.
They take effect on next reboot.

**Why this phase exists**: on the Beelink ME Mini 2 with Samsung PM981/PM9A1-
series NVMe drives, aggressive PCIe ASPM / NVMe APST causes the controllers
to drop off the bus under sustained write load. The drives are fine — the
power-management is too aggressive. Linux recommends the three params we
apply here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


@dataclass(frozen=True)
class TunablesConfig:
    kernel_extra_options: tuple[str, ...] = ()
    timezone: str = ""
    ntp_servers: tuple[str, ...] = ()


def load_tunables_config(path: Path) -> TunablesConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    kernel = raw.get("kernel") or {}
    system = raw.get("system") or {}
    return TunablesConfig(
        kernel_extra_options=tuple(kernel.get("extra_options") or ()),
        timezone=system.get("timezone", ""),
        ntp_servers=tuple(system.get("ntp_servers") or ()),
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
