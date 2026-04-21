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

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NutSpec:
    enable: bool = True
    identifier: str = "ups"
    description: str = ""
    driver: str = ""             # "<driver>$<model>" e.g. "usbhid-ups$Smart-UPS (USB)"
    port: str = "auto"
    mode: str = "MASTER"         # MASTER | SLAVE
    remoteport: int = 3493
    shutdown: str = "BATT"       # BATT | LOWBATT
    shutdowntimer: int = 30      # seconds
    monuser: str = "upsmon"


def load_nut_config(path: Path) -> NutSpec:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nut = raw.get("nut") or {}
    return NutSpec(
        enable=bool(nut.get("enable", True)),
        identifier=nut.get("identifier", "ups"),
        description=nut.get("description", ""),
        driver=nut.get("driver", ""),
        port=str(nut.get("port", "auto")),
        mode=str(nut.get("mode", "MASTER")).upper(),
        remoteport=int(nut.get("remoteport", 3493)),
        shutdown=str(nut.get("shutdown", "BATT")).upper(),
        shutdowntimer=int(nut.get("shutdowntimer", 30)),
        monuser=nut.get("monuser", "upsmon"),
    )


# ─── ensure_ups_config ───────────────────────────────────────────────────────


_MANAGED_UPS_FIELDS = (
    "identifier", "description", "driver", "port",
    "mode", "remoteport", "shutdown", "shutdowntimer", "monuser",
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
        "shutdown": spec.shutdown,
        "shutdowntimer": spec.shutdowntimer,
        "monuser": spec.monuser,
    }

    changes: dict[str, Any] = {}
    for k, v in desired.items():
        if live.get(k) != v:
            changes[k] = v

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("ups.update", changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


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

    diff = ensure_ups_service(cli, enable=cfg.enable, apply=ctx.apply)
    log.info(
        "ups_service_ensured",
        enable=cfg.enable, action=diff.action, changed=diff.changed,
    )

    return 0
