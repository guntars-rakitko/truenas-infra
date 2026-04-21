"""Phase: storage-tasks — SMART, scrub, and snapshot schedules.

See docs/plans/zesty-drifting-castle.md §Phase 6.

Planned:
  * SMART tests: short weekly Sunday 02:00, long monthly 1st Sunday 03:00,
    all NVMe drives. (NOTE: TrueNAS 25.10 Community removed the smart.* API;
    orchestrator catches SmartApiUnavailable and skips with a warning.)
  * Scrub: weekly Sunday 04:00 (per approved storage spec).
  * Snapshot tasks: per-env recursive under `tank/kube/{prd,dev}` so
    longhorn+velero snapshot atomically. Plus media, shared, system trees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScrubSpec:
    schedule: str


@dataclass(frozen=True)
class SmartTestSpec:
    test_type: str      # "SHORT" or "LONG"
    schedule: str
    disks: str = "all"  # "all" or comma-separated devnames (future)


@dataclass(frozen=True)
class SnapshotTaskSpec:
    dataset: str
    schedule: str
    lifetime_value: int
    lifetime_unit: str       # "HOUR" | "DAY" | "WEEK" | "MONTH" | "YEAR"
    recursive: bool = False


@dataclass(frozen=True)
class StorageTasksConfig:
    pool_name: str = "tank"
    scrub: ScrubSpec | None = None
    smart_short: SmartTestSpec | None = None
    smart_long: SmartTestSpec | None = None
    snapshots: tuple[SnapshotTaskSpec, ...] = ()


def _parse_cron(expr: str) -> dict[str, str]:
    """Split a 5-field cron expression into TrueNAS-style fields."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"expected 5-field cron, got {expr!r}")
    minute, hour, dom, month, dow = parts
    return {"minute": minute, "hour": hour, "dom": dom, "month": month, "dow": dow}


def _snapshot_lifetime(raw: dict[str, Any]) -> tuple[int, str]:
    """Translate retention_days/weeks/months to TrueNAS (value, unit)."""
    for key, unit in (
        ("retention_hours", "HOUR"),
        ("retention_days", "DAY"),
        ("retention_weeks", "WEEK"),
        ("retention_months", "MONTH"),
        ("retention_years", "YEAR"),
    ):
        if key in raw and raw[key] is not None:
            return int(raw[key]), unit
    raise ValueError(f"snapshot spec {raw!r} has no retention_* field")


def load_storage_tasks_config(path: Path) -> StorageTasksConfig:
    """Parse `scrub:`, `smart:`, `snapshots:` from storage.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    pool = (raw.get("pool") or {}).get("name", "tank")

    scrub_raw = raw.get("scrub") or {}
    scrub = ScrubSpec(schedule=scrub_raw["schedule"]) if scrub_raw else None

    smart_raw = raw.get("smart") or {}
    short_raw = smart_raw.get("short_test") or {}
    long_raw = smart_raw.get("long_test") or {}
    smart_short = (
        SmartTestSpec(
            test_type="SHORT",
            schedule=short_raw["schedule"],
            disks=short_raw.get("disks", "all"),
        )
        if short_raw
        else None
    )
    smart_long = (
        SmartTestSpec(
            test_type="LONG",
            schedule=long_raw["schedule"],
            disks=long_raw.get("disks", "all"),
        )
        if long_raw
        else None
    )

    snapshots: list[SnapshotTaskSpec] = []
    for s in raw.get("snapshots") or []:
        lifetime_value, lifetime_unit = _snapshot_lifetime(s)
        snapshots.append(
            SnapshotTaskSpec(
                dataset=s["dataset"],
                schedule=s["schedule"],
                lifetime_value=lifetime_value,
                lifetime_unit=lifetime_unit,
                recursive=bool(s.get("recursive", False)),
            )
        )

    return StorageTasksConfig(
        pool_name=pool,
        scrub=scrub,
        smart_short=smart_short,
        smart_long=smart_long,
        snapshots=tuple(snapshots),
    )


# ─── ensure_scrub_task ───────────────────────────────────────────────────────


def _schedule_matches(live: dict[str, Any], desired: dict[str, str]) -> bool:
    """A TrueNAS schedule dict matches the desired 5-field schedule."""
    for k in ("minute", "hour", "dom", "month", "dow"):
        if str(live.get(k, "")) != str(desired[k]):
            return False
    return True


def ensure_scrub_task(
    cli: Any, *, spec: ScrubSpec, pool_name: str, apply: bool,
) -> Diff:
    """Ensure a pool.scrub task exists for `pool_name` with the given schedule."""
    pools = cli.call("pool.query", [["name", "=", pool_name]])
    if not pools:
        raise RuntimeError(f"pool {pool_name!r} not found — create it first")
    pool_id = pools[0]["id"]

    desired_schedule = _parse_cron(spec.schedule)

    existing = cli.call("pool.scrub.query", [["pool", "=", pool_id]])

    if not existing:
        payload = {
            "pool": pool_id,
            "schedule": desired_schedule,
            "description": f"Weekly scrub of {pool_name}",
            "enabled": True,
        }
        if apply:
            created = cli.call("pool.scrub.create", payload)
            return Diff.create(created)
        return Diff.create(payload)

    live = existing[0]
    if _schedule_matches(live.get("schedule") or {}, desired_schedule):
        return Diff.noop(live)

    if apply:
        updated = cli.call("pool.scrub.update", live["id"], {"schedule": desired_schedule})
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, "schedule": desired_schedule})


# ─── ensure_smart_test ───────────────────────────────────────────────────────


class SmartApiUnavailable(RuntimeError):
    """Raised when the TrueNAS build has no `smart.*` endpoints (Community 25.10+)."""


def ensure_smart_test(cli: Any, *, spec: SmartTestSpec, apply: bool) -> Diff:
    """Ensure a SMART test schedule (short or long) exists for all disks.

    Raises `SmartApiUnavailable` if the running TrueNAS doesn't expose the
    `smart.*` namespace. The orchestrator catches this and logs a warning
    without failing the phase.
    """
    desired_schedule = _parse_cron(spec.schedule)

    try:
        existing = cli.call("smart.test.query", [["type", "=", spec.test_type]])
    except Exception as e:  # noqa: BLE001
        if "Method does not exist" in str(e):
            raise SmartApiUnavailable(
                "smart.test.* endpoints not available on this TrueNAS build — skipping"
            ) from e
        raise

    def _same(live: dict[str, Any]) -> bool:
        if not _schedule_matches(live.get("schedule") or {}, desired_schedule):
            return False
        if spec.disks == "all" and not bool(live.get("all_disks")):
            return False
        return True

    match = next((l for l in existing if _same(l)), None)
    if match:
        return Diff.noop(match)

    payload = {
        "type": spec.test_type,
        "all_disks": spec.disks == "all",
        "disks": [],
        "schedule": desired_schedule,
        "desc": f"{spec.test_type} SMART test",
    }
    if existing:
        # An entry of this type exists but doesn't match → update.
        live = existing[0]
        if apply:
            updated = cli.call("smart.test.update", live["id"], payload)
            return Diff.update(before=live, after=updated)
        return Diff.update(before=live, after=payload)

    if apply:
        created = cli.call("smart.test.create", payload)
        return Diff.create(created)
    return Diff.create(payload)


# ─── ensure_snapshot_task ────────────────────────────────────────────────────


def ensure_snapshot_task(cli: Any, *, spec: SnapshotTaskSpec, apply: bool) -> Diff:
    """Ensure a snapshot task for a given dataset + schedule exists."""
    desired_schedule = _parse_cron(spec.schedule)

    existing = cli.call("pool.snapshottask.query", [["dataset", "=", spec.dataset]])

    def _same(live: dict[str, Any]) -> bool:
        if not _schedule_matches(live.get("schedule") or {}, desired_schedule):
            return False
        if int(live.get("lifetime_value", 0)) != spec.lifetime_value:
            return False
        if str(live.get("lifetime_unit", "")).upper() != spec.lifetime_unit:
            return False
        if bool(live.get("recursive")) != spec.recursive:
            return False
        return True

    match = next((l for l in existing if _same(l)), None)
    if match:
        return Diff.noop(match)

    payload = {
        "dataset": spec.dataset,
        "recursive": spec.recursive,
        "lifetime_value": spec.lifetime_value,
        "lifetime_unit": spec.lifetime_unit,
        "naming_schema": "auto-%Y-%m-%d_%H-%M",
        "schedule": desired_schedule,
        "enabled": True,
    }
    if existing:
        live = existing[0]
        if apply:
            updated = cli.call("pool.snapshottask.update", live["id"], payload)
            return Diff.update(before=live, after=updated)
        return Diff.update(before=live, after=payload)

    if apply:
        created = cli.call("pool.snapshottask.create", payload)
        return Diff.create(created)
    return Diff.create(payload)


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/storage.yaml")


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
) -> int:
    """Phase 6: storage-tasks — SMART + scrub + snapshots."""
    log = ctx.log.bind(phase="storage-tasks")
    cfg = load_storage_tasks_config(config_path or DEFAULT_CONFIG_PATH)

    # Scrub (pool-level)
    if cfg.scrub and (not only or only == "scrub"):
        diff = ensure_scrub_task(
            cli, spec=cfg.scrub, pool_name=cfg.pool_name, apply=ctx.apply,
        )
        log.info(
            "scrub_ensured",
            pool=cfg.pool_name, schedule=cfg.scrub.schedule,
            action=diff.action, changed=diff.changed,
        )

    # SMART tests (short + long) — gracefully skip if API unavailable
    for spec in (cfg.smart_short, cfg.smart_long):
        if spec is None:
            continue
        if only and only != f"smart-{spec.test_type.lower()}":
            continue
        try:
            diff = ensure_smart_test(cli, spec=spec, apply=ctx.apply)
            log.info(
                "smart_test_ensured",
                type=spec.test_type, schedule=spec.schedule,
                action=diff.action, changed=diff.changed,
            )
        except SmartApiUnavailable as e:
            log.warning(
                "smart_test_skipped",
                type=spec.test_type, reason=str(e),
            )

    # Snapshot tasks
    for snap in cfg.snapshots:
        if only and only != snap.dataset:
            continue
        diff = ensure_snapshot_task(cli, spec=snap, apply=ctx.apply)
        log.info(
            "snapshot_task_ensured",
            dataset=snap.dataset, schedule=snap.schedule,
            lifetime=f"{snap.lifetime_value} {snap.lifetime_unit}",
            recursive=snap.recursive,
            action=diff.action, changed=diff.changed,
        )

    return 0
