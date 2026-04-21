"""Tests for modules/datasets.py — phase 5 (dataset tree with quotas)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── load_datasets_config ────────────────────────────────────────────────────


def test_load_datasets_config_applies_defaults(tmp_path: Path) -> None:
    from truenas_infra.modules.datasets import load_datasets_config

    yaml_file = tmp_path / "storage.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            defaults:
              compression: lz4
              atime: off
              xattr: sa
              recordsize: 128K

            datasets:
              - name: tank/kube
              - name: tank/kube/longhorn-prd
                recordsize: 128K
              - name: tank/kube/velero-prd
                recordsize: 1M
              - name: tank/media
                quota: 4T
            """
        ).strip()
    )

    cfg = load_datasets_config(yaml_file)

    assert len(cfg.datasets) == 4
    ds = {d.name: d for d in cfg.datasets}

    # Defaults applied where not overridden.
    assert ds["tank/kube"].compression == "lz4"
    assert ds["tank/kube"].atime == "off"
    assert ds["tank/kube"].xattr == "sa"
    assert ds["tank/kube"].recordsize == "128K"
    assert ds["tank/kube"].quota is None

    # Per-dataset override of recordsize.
    assert ds["tank/kube/velero-prd"].recordsize == "1M"

    # Quota read through correctly.
    assert ds["tank/media"].quota == "4T"


def test_load_datasets_config_preserves_order(tmp_path: Path) -> None:
    """Order matters — parents must come before children when we create them."""
    from truenas_infra.modules.datasets import load_datasets_config

    yaml_file = tmp_path / "storage.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            datasets:
              - name: tank/a
              - name: tank/a/b
              - name: tank/a/b/c
            """
        ).strip()
    )

    cfg = load_datasets_config(yaml_file)

    names = [d.name for d in cfg.datasets]
    assert names == ["tank/a", "tank/a/b", "tank/a/b/c"]


# ─── ensure_dataset ──────────────────────────────────────────────────────────


def _spec(name: str = "tank/kube/longhorn-prd", **kw) -> "DatasetSpec":  # type: ignore[name-defined]
    from truenas_infra.modules.datasets import DatasetSpec
    return DatasetSpec(
        name=name,
        compression=kw.get("compression", "lz4"),
        atime=kw.get("atime", "off"),
        xattr=kw.get("xattr", "sa"),
        recordsize=kw.get("recordsize", "128K"),
        quota=kw.get("quota"),
    )


def _live_dataset(**overrides) -> dict:
    """Shape TrueNAS returns from pool.dataset.query — props are wrapped objects."""
    base = {
        "id": "tank/kube/longhorn-prd",
        "name": "tank/kube/longhorn-prd",
        "type": "FILESYSTEM",
        "compression": {"value": "lz4", "rawvalue": "lz4", "source": "LOCAL"},
        "atime": {"value": "off", "rawvalue": "off", "source": "LOCAL"},
        "xattr": {"value": "sa", "rawvalue": "sa", "source": "LOCAL"},
        "recordsize": {"value": "128K", "rawvalue": "131072", "source": "LOCAL"},
        "quota": {"value": "none", "rawvalue": "0", "source": "DEFAULT"},
    }
    base.update(overrides)
    return base


def test_ensure_dataset_creates_when_missing() -> None:
    from truenas_infra.modules.datasets import ensure_dataset

    cli = _mk_cli([
        [],  # pool.dataset.query → not found
        {"id": "tank/kube/longhorn-prd"},  # pool.dataset.create result
    ])

    diff = ensure_dataset(cli, _spec(), apply=True)

    assert diff.changed is True
    assert diff.action == "create"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["pool.dataset.query", "pool.dataset.create"]
    create = next(c for c in cli.call.call_args_list if c.args[0] == "pool.dataset.create")
    payload = create.args[1]
    assert payload["name"] == "tank/kube/longhorn-prd"
    assert payload["type"] == "FILESYSTEM"   # union discriminator required
    assert payload["compression"] == "LZ4"  # TrueNAS wants uppercase for these enums
    assert payload["atime"] == "OFF"
    assert payload["recordsize"] == "128K"
    # xattr is not accepted at create time on TrueNAS 25.10 — only via update.
    assert "xattr" not in payload


def test_ensure_dataset_noop_when_match() -> None:
    from truenas_infra.modules.datasets import ensure_dataset

    cli = _mk_cli([[_live_dataset()]])

    diff = ensure_dataset(cli, _spec(), apply=True)

    assert diff.changed is False
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert "pool.dataset.update" not in call_names
    assert "pool.dataset.create" not in call_names


def test_ensure_dataset_updates_when_recordsize_differs() -> None:
    from truenas_infra.modules.datasets import ensure_dataset

    live = _live_dataset(recordsize={"value": "128K", "rawvalue": "131072", "source": "LOCAL"})
    updated = {**live, "recordsize": {"value": "1M", "rawvalue": "1048576", "source": "LOCAL"}}
    cli = _mk_cli([[live], updated])

    spec = _spec(recordsize="1M")
    diff = ensure_dataset(cli, spec, apply=True)

    assert diff.changed is True
    assert diff.action == "update"
    update = next(c for c in cli.call.call_args_list if c.args[0] == "pool.dataset.update")
    assert update.args[1] == "tank/kube/longhorn-prd"
    assert update.args[2]["recordsize"] == "1M"


def test_ensure_dataset_dry_run_no_write() -> None:
    from truenas_infra.modules.datasets import ensure_dataset

    cli = _mk_cli([[]])
    diff = ensure_dataset(cli, _spec(), apply=False)

    assert diff.changed is True
    assert diff.action == "create"
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == ["pool.dataset.query"]


def test_ensure_dataset_quota_create() -> None:
    from truenas_infra.modules.datasets import ensure_dataset

    cli = _mk_cli([
        [],
        {"id": "tank/media"},
    ])

    spec = _spec(name="tank/media", quota="4T")
    diff = ensure_dataset(cli, spec, apply=True)

    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "pool.dataset.create")
    payload = create.args[1]
    # TrueNAS 25.10 wants quota in bytes (integer), not a human string.
    assert payload["quota"] == 4 * 1024**4


def test_parse_size_handles_common_suffixes() -> None:
    from truenas_infra.modules.datasets import _parse_size

    assert _parse_size("4T") == 4 * 1024**4
    assert _parse_size("1G") == 1024**3
    assert _parse_size("128M") == 128 * 1024**2
    assert _parse_size("512K") == 512 * 1024
    assert _parse_size("1024") == 1024
    assert _parse_size("4TB") == 4 * 1024**4  # binary even if suffix is TB
    assert _parse_size("1.5T") == int(1.5 * 1024**4)


def test_ensure_dataset_quota_change_triggers_update() -> None:
    from truenas_infra.modules.datasets import ensure_dataset

    # Live has no quota; spec wants 4T.
    live = _live_dataset(
        id="tank/media", name="tank/media",
        quota={"value": "none", "rawvalue": "0", "source": "DEFAULT"},
    )
    cli = _mk_cli([[live], {**live, "quota": {"value": "4T", "rawvalue": "4398046511104", "source": "LOCAL"}}])

    spec = _spec(name="tank/media", quota="4T")
    diff = ensure_dataset(cli, spec, apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "pool.dataset.update")
    assert update.args[2]["quota"] == 4 * 1024**4


# ─── run() orchestration ─────────────────────────────────────────────────────


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        import structlog
        self.log = structlog.get_logger("test")


def test_run_creates_each_dataset_in_order(tmp_path: Path) -> None:
    from truenas_infra.modules.datasets import run

    cfg_path = tmp_path / "storage.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            """
            datasets:
              - name: tank/kube
              - name: tank/kube/longhorn-prd
              - name: tank/media
                quota: 4T
            """
        ).strip()
    )

    # None exist yet — three creates.
    cli = _mk_cli([
        [], {"id": "tank/kube"},
        [], {"id": "tank/kube/longhorn-prd"},
        [], {"id": "tank/media"},
    ])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_path)

    assert rc == 0
    call_names = [c.args[0] for c in cli.call.call_args_list]
    assert call_names == [
        "pool.dataset.query", "pool.dataset.create",
        "pool.dataset.query", "pool.dataset.create",
        "pool.dataset.query", "pool.dataset.create",
    ]
    # Created in YAML order (parents first).
    creates = [c.args[1]["name"] for c in cli.call.call_args_list if c.args[0] == "pool.dataset.create"]
    assert creates == ["tank/kube", "tank/kube/longhorn-prd", "tank/media"]
