"""Tests for modules/storage_tasks.py — phase 6 (SMART, scrub, snapshots)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── load_storage_tasks_config ───────────────────────────────────────────────


def test_load_storage_tasks_config_parses_all_sections(tmp_path: Path) -> None:
    from truenas_infra.modules.storage_tasks import load_storage_tasks_config

    yaml_file = tmp_path / "storage.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            pool:
              name: tank

            scrub:
              schedule: "0 4 * * 0"

            smart:
              short_test:
                schedule: "0 2 * * 0"
                disks: all
              long_test:
                schedule: "0 3 1-7 * 0"
                disks: all

            snapshots:
              - dataset: tank/kube/longhorn-prd
                schedule: "0 1 * * *"
                retention_days: 14
              - dataset: tank/media
                recursive: true
                schedule: "0 5 * * 0"
                retention_weeks: 4
            """
        ).strip()
    )

    cfg = load_storage_tasks_config(yaml_file)

    assert cfg.pool_name == "tank"
    assert cfg.scrub.schedule == "0 4 * * 0"
    assert cfg.smart_short.schedule == "0 2 * * 0"
    assert cfg.smart_long.schedule == "0 3 1-7 * 0"
    assert len(cfg.snapshots) == 2
    assert cfg.snapshots[0].dataset == "tank/kube/longhorn-prd"
    assert cfg.snapshots[0].schedule == "0 1 * * *"
    assert cfg.snapshots[0].lifetime_unit == "DAY"
    assert cfg.snapshots[0].lifetime_value == 14
    assert cfg.snapshots[0].recursive is False
    assert cfg.snapshots[1].recursive is True
    assert cfg.snapshots[1].lifetime_unit == "WEEK"
    assert cfg.snapshots[1].lifetime_value == 4


# ─── _parse_cron (helper) ────────────────────────────────────────────────────


def test_parse_cron_simple() -> None:
    from truenas_infra.modules.storage_tasks import _parse_cron

    assert _parse_cron("0 4 * * 0") == {
        "minute": "0", "hour": "4", "dom": "*", "month": "*", "dow": "0",
    }


def test_parse_cron_day_range() -> None:
    from truenas_infra.modules.storage_tasks import _parse_cron

    assert _parse_cron("0 3 1-7 * 0") == {
        "minute": "0", "hour": "3", "dom": "1-7", "month": "*", "dow": "0",
    }


def test_schedule_matches_ignores_zero_padding() -> None:
    """TrueNAS's own default scrub task stores minute "00"; "0" is the same."""
    from truenas_infra.modules.storage_tasks import _parse_cron, _schedule_matches

    live = {"minute": "00", "hour": "04", "dom": "*", "month": "*", "dow": "7"}
    assert _schedule_matches(live, _parse_cron("0 4 * * 7")) is True
    assert _schedule_matches(live, _parse_cron("0 0 * * 7")) is False


# ─── ensure_scrub_task ───────────────────────────────────────────────────────


def test_ensure_scrub_task_creates_when_missing() -> None:
    from truenas_infra.modules.storage_tasks import ScrubSpec, ensure_scrub_task

    cli = _mk_cli([
        [{"id": 1, "name": "tank"}],   # pool.query
        [],                              # pool.scrub.query — no scheduled scrub
        {"id": 99},                      # pool.scrub.create result
    ])

    diff = ensure_scrub_task(cli, spec=ScrubSpec(schedule="0 4 * * 0"), pool_name="tank", apply=True)

    assert diff.changed is True
    assert diff.action == "create"
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "pool.scrub.create" in names
    create = next(c for c in cli.call.call_args_list if c.args[0] == "pool.scrub.create")
    payload = create.args[1]
    assert payload["pool"] == 1
    assert payload["schedule"]["minute"] == "0"
    assert payload["schedule"]["hour"] == "4"
    assert payload["schedule"]["dow"] == "0"


def test_ensure_scrub_task_noop_when_match() -> None:
    from truenas_infra.modules.storage_tasks import ScrubSpec, ensure_scrub_task

    existing = {
        "id": 99,
        "pool": 1,
        "schedule": {"minute": "0", "hour": "4", "dom": "*", "month": "*", "dow": "0"},
    }
    cli = _mk_cli([
        [{"id": 1, "name": "tank"}],
        [existing],
    ])
    diff = ensure_scrub_task(cli, spec=ScrubSpec(schedule="0 4 * * 0"), pool_name="tank", apply=True)
    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "pool.scrub.create" not in names
    assert "pool.scrub.update" not in names


def test_ensure_scrub_task_updates_when_schedule_differs() -> None:
    from truenas_infra.modules.storage_tasks import ScrubSpec, ensure_scrub_task

    existing = {
        "id": 99,
        "pool": 1,
        "schedule": {"minute": "0", "hour": "3", "dom": "*", "month": "*", "dow": "0"},
    }
    cli = _mk_cli([
        [{"id": 1, "name": "tank"}],
        [existing],
        {**existing, "schedule": {**existing["schedule"], "hour": "4"}},
    ])
    diff = ensure_scrub_task(cli, spec=ScrubSpec(schedule="0 4 * * 0"), pool_name="tank", apply=True)
    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "pool.scrub.update")
    assert update.args[1] == 99
    assert update.args[2]["schedule"]["hour"] == "4"


# ─── ensure_smart_test ───────────────────────────────────────────────────────


def test_ensure_smart_test_creates_when_missing() -> None:
    from truenas_infra.modules.storage_tasks import SmartTestSpec, ensure_smart_test

    cli = _mk_cli([
        [],                 # smart.test.query
        {"id": 1},          # smart.test.create
    ])
    spec = SmartTestSpec(test_type="SHORT", schedule="0 2 * * 0")
    diff = ensure_smart_test(cli, spec=spec, apply=True)
    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "smart.test.create")
    payload = create.args[1]
    assert payload["type"] == "SHORT"
    assert payload["all_disks"] is True
    assert payload["schedule"]["hour"] == "2"


def test_ensure_smart_test_noop_when_match() -> None:
    from truenas_infra.modules.storage_tasks import SmartTestSpec, ensure_smart_test

    existing = {
        "id": 1,
        "type": "SHORT",
        "all_disks": True,
        "schedule": {"minute": "0", "hour": "2", "dom": "*", "month": "*", "dow": "0"},
    }
    cli = _mk_cli([[existing]])
    spec = SmartTestSpec(test_type="SHORT", schedule="0 2 * * 0")
    diff = ensure_smart_test(cli, spec=spec, apply=True)
    assert diff.changed is False


# ─── ensure_snapshot_task ────────────────────────────────────────────────────


def test_ensure_snapshot_task_creates_when_missing() -> None:
    from truenas_infra.modules.storage_tasks import SnapshotTaskSpec, ensure_snapshot_task

    cli = _mk_cli([
        [],                 # pool.snapshottask.query
        {"id": 1},          # pool.snapshottask.create
    ])
    spec = SnapshotTaskSpec(
        dataset="tank/kube/longhorn-prd",
        schedule="0 1 * * *",
        lifetime_value=14,
        lifetime_unit="DAY",
        recursive=False,
    )
    diff = ensure_snapshot_task(cli, spec=spec, apply=True)
    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "pool.snapshottask.create")
    payload = create.args[1]
    assert payload["dataset"] == "tank/kube/longhorn-prd"
    assert payload["lifetime_value"] == 14
    assert payload["lifetime_unit"] == "DAY"
    assert payload["recursive"] is False
    assert payload["enabled"] is True


def test_ensure_snapshot_task_noop_when_match() -> None:
    from truenas_infra.modules.storage_tasks import SnapshotTaskSpec, ensure_snapshot_task

    existing = {
        "id": 7,
        "dataset": "tank/kube/longhorn-prd",
        "recursive": False,
        "lifetime_value": 14,
        "lifetime_unit": "DAY",
        "schedule": {"minute": "0", "hour": "1", "dom": "*", "month": "*", "dow": "*"},
    }
    cli = _mk_cli([[existing]])
    spec = SnapshotTaskSpec(
        dataset="tank/kube/longhorn-prd",
        schedule="0 1 * * *",
        lifetime_value=14,
        lifetime_unit="DAY",
    )
    diff = ensure_snapshot_task(cli, spec=spec, apply=True)
    assert diff.changed is False


# ─── run() orchestration ─────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        import structlog
        self.log = structlog.get_logger("test")


def test_run_orchestrates_all_three(tmp_path: Path) -> None:
    from truenas_infra.modules.storage_tasks import run

    cfg_path = tmp_path / "storage.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            pool:
              name: tank

            scrub:
              schedule: "0 4 * * 0"

            smart:
              short_test:
                schedule: "0 2 * * 0"
              long_test:
                schedule: "0 3 1-7 * 0"

            snapshots:
              - dataset: tank/kube/longhorn-prd
                schedule: "0 1 * * *"
                retention_days: 14
            """
        ).strip()
    )

    cli = _mk_cli([
        # scrub: pool.query, scrub.query=[], scrub.create
        [{"id": 1, "name": "tank"}],
        [],
        {"id": 99},
        # smart short: smart.test.query=[], smart.test.create
        [],
        {"id": 10},
        # smart long: smart.test.query=[], smart.test.create
        [],
        {"id": 11},
        # snapshot: pool.snapshottask.query=[], pool.snapshottask.create
        [],
        {"id": 20},
    ])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_path)

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "pool.scrub.create" in names
    assert names.count("smart.test.create") == 2
    assert "pool.snapshottask.create" in names


# ─── exclude (recursive tasks) ───────────────────────────────────────────────


def test_load_parses_snapshot_exclude(tmp_path: Path) -> None:
    from truenas_infra.modules.storage_tasks import load_storage_tasks_config

    yaml_file = tmp_path / "storage.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            snapshots:
              - dataset: tank/system
                recursive: true
                exclude:
                  - tank/system/apps-config/wiki
                schedule: "30 2 * * *"
                retention_days: 7
            """
        ).strip()
    )
    cfg = load_storage_tasks_config(yaml_file)
    assert cfg.snapshots[0].exclude == ("tank/system/apps-config/wiki",)


def _system_spec(exclude: tuple[str, ...] = ("tank/system/apps-config/wiki",)):
    from truenas_infra.modules.storage_tasks import SnapshotTaskSpec

    return SnapshotTaskSpec(
        dataset="tank/system",
        schedule="30 2 * * *",
        lifetime_value=7,
        lifetime_unit="DAY",
        recursive=True,
        exclude=exclude,
    )


def _live_system(exclude: list[str]) -> dict:
    return {
        "id": 5,
        "dataset": "tank/system",
        "recursive": True,
        "exclude": exclude,
        "lifetime_value": 7,
        "lifetime_unit": "DAY",
        "schedule": {"minute": "30", "hour": "2", "dom": "*", "month": "*", "dow": "*"},
    }


def test_ensure_snapshot_task_sends_exclude_on_create() -> None:
    from truenas_infra.modules.storage_tasks import ensure_snapshot_task

    cli = _mk_cli([[], {"id": 5}])
    ensure_snapshot_task(cli, spec=_system_spec(), apply=True)
    create = next(c for c in cli.call.call_args_list if c.args[0] == "pool.snapshottask.create")
    assert create.args[1]["exclude"] == ["tank/system/apps-config/wiki"]


def test_ensure_snapshot_task_updates_when_exclude_differs() -> None:
    from truenas_infra.modules.storage_tasks import ensure_snapshot_task

    live = _live_system([])
    cli = _mk_cli([[live], {**live, "exclude": ["tank/system/apps-config/wiki"]}])
    diff = ensure_snapshot_task(cli, spec=_system_spec(), apply=True)
    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "pool.snapshottask.update")
    assert update.args[2]["exclude"] == ["tank/system/apps-config/wiki"]


def test_ensure_snapshot_task_noop_when_exclude_matches() -> None:
    from truenas_infra.modules.storage_tasks import ensure_snapshot_task

    cli = _mk_cli([[_live_system(["tank/system/apps-config/wiki"])]])
    diff = ensure_snapshot_task(cli, spec=_system_spec(), apply=True)
    assert diff.changed is False


# ─── The repo's own config/storage.yaml (#222) ───────────────────────────────
#
# ⚠ The whole storage-tasks block (scrub, SMART, snapshots) left
# config/storage.yaml in e3d5663 during the 2026-09-23 pool rebuild. The
# rebuild recreated `tank`, so the old tasks went with it, and `phase
# storage-tasks --apply` then had nothing to recreate: tank went two months
# with no snapshot at all. These tests fail if the block goes again.

REPO_STORAGE_YAML = Path(__file__).resolve().parent.parent / "config" / "storage.yaml"

# What #222 restores. Each must have a task, not just be covered by a parent's.
_SNAPSHOTTED = (
    "tank/kube/prd",
    "tank/kube/dev",
    "tank/media",
    "tank/shared/general",
    "tank/system",
)

# Datasets another tool creates, renames and DELETES with a non-recursive
# pool.dataset.delete: ZFS refuses to destroy a dataset that has snapshots, so
# a recursive task covering one of these breaks that tool. The wiki deploy
# swaps tank/system/apps-config/wiki/{site,site-next,site-prev} (wiki#295).
_TOOL_OWNED = ("tank/system/apps-config/wiki",)


def test_repo_config_declares_tank_snapshot_tasks() -> None:
    from truenas_infra.modules.storage_tasks import load_storage_tasks_config

    cfg = load_storage_tasks_config(REPO_STORAGE_YAML)
    declared = {s.dataset for s in cfg.snapshots}
    missing = [d for d in _SNAPSHOTTED if d not in declared]
    assert not missing, f"no snapshot task for {missing} (#222)"
    assert cfg.scrub is not None, "no scrub schedule declared for tank (#222)"


def test_repo_config_recursive_tasks_exclude_tool_owned_datasets() -> None:
    from truenas_infra.modules.storage_tasks import load_storage_tasks_config

    cfg = load_storage_tasks_config(REPO_STORAGE_YAML)
    for spec in cfg.snapshots:
        for owned in _TOOL_OWNED:
            covers = owned == spec.dataset or (
                spec.recursive and owned.startswith(spec.dataset + "/")
            )
            if covers:
                assert owned in spec.exclude, (
                    f"the {spec.dataset} task snapshots {owned}, whose tool deletes "
                    "datasets non-recursively; add it to that task's exclude"
                )
