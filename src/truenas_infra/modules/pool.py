"""Phase: pool — one-shot RAIDZ1 pool creation.

See docs/plans/zesty-drifting-castle.md §Phase 4.

**One-shot.** Destroys disks' previous contents. Gated behind
`--confirm=CREATE-TANK` flag; idempotent wrt pool existence (skips if
`tank` already exists).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PoolConfig:
    name: str
    topology_type: str       # "RAIDZ1" / "RAIDZ2" / "MIRROR" / etc.
    disks: tuple[str, ...]   # logical device names e.g. "nvme0n1"
    ashift: int = 12
    # Default OFF: on the Beelink ME Mini the 3.3V rail can't take the TRIM
    # current bursts autotrim triggers (see ensure_autotrim + storage.yaml).
    autotrim: bool = False


def load_pool_config(path: Path) -> PoolConfig:
    """Parse the `pool:` section of storage.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    p = raw.get("pool") or {}
    topology = p.get("topology") or {}

    return PoolConfig(
        name=p["name"],
        topology_type=topology.get("type", "RAIDZ1").upper(),
        disks=tuple(topology.get("disks") or ()),
        ashift=int(p.get("ashift", 12)),
        autotrim=bool(p.get("autotrim", False)),
    )


# ─── resolve_disk_identifiers ────────────────────────────────────────────────


def resolve_disk_identifiers(cli: Any, *, devnames: tuple[str, ...]) -> list[str]:
    """Validate the requested disks exist and are unclaimed; return devnames.

    `pool.create` accepts devnames (e.g. ``nvme0n1``) in the ``disks`` array —
    NOT the TrueNAS ``identifier`` string. This function queries ``disk.query``
    to prove each requested device exists and isn't already in a pool, then
    returns the validated devnames in order.

    Despite the legacy name, the return value is the devnames list.
    """
    disks = cli.call("disk.query")
    by_devname: dict[str, dict[str, Any]] = {d["devname"]: d for d in disks if d.get("devname")}

    validated: list[str] = []
    for name in devnames:
        d = by_devname.get(name)
        if d is None:
            raise RuntimeError(
                f"Disk {name!r} not found in disk.query. Available: "
                f"{sorted(by_devname.keys())}"
            )
        if d.get("pool"):
            raise RuntimeError(
                f"Disk {name!r} is already in pool {d.get('pool')!r}. "
                "Refusing to re-use."
            )
        validated.append(name)  # pool.create wants the devname itself
    return validated


# ─── ensure_pool ─────────────────────────────────────────────────────────────


# The literal token the operator must pass (via --confirm=...) to prove they
# mean to create the pool. Pool creation destroys any prior content on the
# chosen disks — this is the safety interlock.
CONFIRM_TOKEN = "CREATE-TANK"


def ensure_pool(
    cli: Any,
    spec: PoolConfig,
    *,
    apply: bool,
    confirm_token: str,
    post_check_timeout: float = 30.0,
) -> Diff:
    """Ensure the RAIDZ pool exists. One-shot, destructive, gated.

    If the pool already exists → `Diff.noop`. No check that the topology
    matches — we don't support recreating a pool.

    If it doesn't exist and `apply=True`:
      * `confirm_token` MUST match `CONFIRM_TOKEN`
      * Resolve devnames to TrueNAS disk identifiers (refuses if any disk is
        missing or already in another pool)
      * Call `pool.create` (a JOB — the client blocks until done)
    """
    existing = cli.call("pool.query", [["name", "=", spec.name]])
    if existing:
        return Diff.noop(existing[0])

    if apply and confirm_token != CONFIRM_TOKEN:
        raise RuntimeError(
            f"Pool creation refused. Pass --confirm={CONFIRM_TOKEN} to proceed. "
            "This operation will destroy any existing data on the configured disks."
        )

    identifiers = resolve_disk_identifiers(cli, devnames=spec.disks)

    payload: dict[str, Any] = {
        "name": spec.name,
        "topology": {
            "data": [
                {
                    "type": spec.topology_type,
                    "disks": identifiers,
                }
            ],
        },
    }

    if apply:
        created = cli.call("pool.create", payload)
        # Defensive post-check: confirm the pool actually exists now.
        # pool.create is a JOB — cli.call occasionally returns before
        # pool.query reflects the new pool (observed in TrueNAS 25.10). Poll
        # briefly before giving up.
        deadline = time.monotonic() + post_check_timeout
        check: list[dict[str, Any]] = []
        while True:
            check = cli.call("pool.query", [["name", "=", spec.name]])
            if check:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        if not check:
            raise RuntimeError(
                f"pool.create returned but pool {spec.name!r} does not exist "
                f"after {post_check_timeout}s. Check the NAS job log "
                "(core.get_jobs) for details."
            )
        return Diff.create(check[0])
    return Diff.create(payload)


# ─── ensure_autotrim ─────────────────────────────────────────────────────────


def ensure_autotrim(cli: Any, spec: PoolConfig, *, apply: bool) -> Diff:
    """Reconcile the pool's `autotrim` property (non-destructive).

    Unlike `ensure_pool` (one-shot create), this runs every phase apply so the
    property can't drift. autotrim was disabled 2026-07-22 as a Beelink ME Mini
    3.3V-rail mitigation: with it on, file deletes fire TRIM bursts that spike
    simultaneous NVMe current and drop a drive off the bus. `pool.update` is a
    plain property write — no data risk.
    """
    existing = cli.call("pool.query", [["name", "=", spec.name]])
    if not existing:
        # Pool not created yet — `ensure_pool` handles creation; nothing to reconcile.
        return Diff.noop(existing)
    pool = existing[0]
    desired = "ON" if spec.autotrim else "OFF"
    prop = pool.get("autotrim") or {}
    current = str(prop.get("value", "")).upper()
    if current == desired:
        return Diff.noop(pool)
    if apply:
        updated = cli.call("pool.update", pool["id"], {"autotrim": desired})
        return Diff.update(before=pool, after=updated)
    return Diff.update(before=pool, after={**pool, "autotrim": desired})


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/storage.yaml")


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
) -> int:
    """Phase 4: pool — one-shot RAIDZ pool creation.

    Returns 0 on success (incl. idempotent noop), non-zero on refusal.
    """
    log = ctx.log.bind(phase="pool")
    cfg = load_pool_config(config_path or DEFAULT_CONFIG_PATH)

    confirm_token = getattr(ctx, "confirm_token", "") or ""

    try:
        diff = ensure_pool(cli, cfg, apply=ctx.apply, confirm_token=confirm_token)
    except RuntimeError as e:
        log.error("pool_create_refused", reason=str(e))
        return 2

    log.info(
        "pool_ensured",
        name=cfg.name,
        topology=cfg.topology_type,
        disks=list(cfg.disks),
        action=diff.action,
        changed=diff.changed,
    )

    # Reconcile non-destructive pool properties (autotrim) on every apply.
    diff = ensure_autotrim(cli, cfg, apply=ctx.apply)
    log.info(
        "autotrim_ensured",
        pool=cfg.name,
        desired="ON" if cfg.autotrim else "OFF",
        action=diff.action,
        changed=diff.changed,
    )
    return 0
