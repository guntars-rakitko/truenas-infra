"""Phase: datasets — nested dataset tree, quotas, and recordsize tuning.

See docs/plans/zesty-drifting-castle.md §Phase 5.

Planned tree (all defaults: `compression=lz4`, `atime=off`, `xattr=sa`).
Environment-first grouping — per-env quota/snapshots/NFS attach at the env
parent (`tank/kube/prd`, `tank/kube/dev`):

    tank/
    ├── kube/                             (parent)
    │   ├── prd/                          (parent — NFS export boundary for prd cluster)
    │   │   ├── longhorn   recordsize=128K
    │   │   └── velero     recordsize=1M
    │   └── dev/                          (parent — NFS export boundary for dev cluster)
    │       ├── longhorn   recordsize=128K
    │       └── velero     recordsize=1M
    ├── media/              quota=4T
    │   ├── plex/{config,media}
    │   └── torrent/{config,downloads}
    ├── shared/
    │   └── general         quota=1T
    └── system/
        ├── pxe/{config,assets}
        └── apps-config/{nut,...}

Idempotent. Re-running adjusts properties to match `config/storage.yaml` but
never destroys a dataset.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatasetSpec:
    name: str                     # e.g. "tank/kube/longhorn-prd"
    compression: str = "lz4"
    atime: str = "off"
    xattr: str = "sa"
    recordsize: str = "128K"
    quota: str | None = None      # e.g. "4T", or None


@dataclass(frozen=True)
class DatasetsConfig:
    datasets: tuple[DatasetSpec, ...] = ()


def _onoff(value: Any, default: str) -> str:
    """Normalise YAML `on/off` (which parses to True/False) back to ZFS strings."""
    if value is True:
        return "on"
    if value is False:
        return "off"
    if value is None:
        return default
    return str(value)


def load_datasets_config(path: Path) -> DatasetsConfig:
    """Parse the `datasets:` + `defaults:` sections of storage.yaml."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    defaults = raw.get("defaults") or {}

    out: list[DatasetSpec] = []
    for d in raw.get("datasets") or []:
        out.append(
            DatasetSpec(
                name=d["name"],
                compression=_onoff(
                    d.get("compression", defaults.get("compression")), "lz4"
                ),
                atime=_onoff(d.get("atime", defaults.get("atime")), "off"),
                xattr=_onoff(d.get("xattr", defaults.get("xattr")), "sa"),
                recordsize=str(
                    d.get("recordsize", defaults.get("recordsize", "128K"))
                ),
                quota=(str(d["quota"]) if "quota" in d and d["quota"] is not None else None),
            )
        )
    return DatasetsConfig(datasets=tuple(out))


# ─── ensure_dataset ──────────────────────────────────────────────────────────


# TrueNAS pool.dataset.create uses upper-case enum-like values for some fields
# (compression, atime, xattr); pool.dataset.update accepts the same.
_ENUM_FIELDS = {"compression", "atime", "xattr"}


def _upcase_enum(field: str, value: str) -> str:
    if field in _ENUM_FIELDS:
        return value.upper()
    return value


def _live_property(live: dict[str, Any], key: str) -> str:
    """Extract a property value from TrueNAS's wrapped {value, rawvalue, source} dict."""
    prop = live.get(key)
    if isinstance(prop, dict):
        return str(prop.get("value", ""))
    return str(prop or "")


def _live_raw(live: dict[str, Any], key: str) -> str:
    """Extract the raw (non-formatted) value — usually bytes as a string for sizes."""
    prop = live.get(key)
    if isinstance(prop, dict):
        return str(prop.get("rawvalue", ""))
    return str(prop or "")


_SIZE_SUFFIXES = {
    "K": 1024,
    "M": 1024**2,
    "G": 1024**3,
    "T": 1024**4,
    "P": 1024**5,
}


def _parse_size(value: str) -> int:
    """Parse a size string like '4T' / '128M' / '512KB' / '1024' to bytes.

    Always uses binary (1024-based) units regardless of B suffix, matching
    ZFS/TrueNAS conventions.
    """
    s = value.strip().upper().rstrip("B")
    # Find first letter suffix if any
    for i, ch in enumerate(s):
        if ch.isalpha():
            number = float(s[:i])
            mul = _SIZE_SUFFIXES.get(s[i:], 1)
            return int(number * mul)
    return int(float(s))


def _build_payload(spec: DatasetSpec, *, for_create: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "compression": _upcase_enum("compression", spec.compression),
        "atime": _upcase_enum("atime", spec.atime),
        "recordsize": spec.recordsize,
    }
    if for_create:
        # type is the union discriminator. xattr is not accepted in either
        # create or update on TrueNAS 25.10 — relying on the default (sa).
        payload["name"] = spec.name
        payload["type"] = "FILESYSTEM"
    if spec.quota is not None:
        payload["quota"] = _parse_size(spec.quota)
    return payload


def _diff_props(live: dict[str, Any], spec: DatasetSpec) -> dict[str, Any]:
    """Return only the fields where live disagrees with spec. Upper-cases enums."""
    changes: dict[str, Any] = {}
    # xattr is NOT managed — TrueNAS 25.10 rejects it in both create & update.
    managed = [
        ("compression", spec.compression),
        ("atime", spec.atime),
        ("recordsize", spec.recordsize),
    ]
    for key, desired in managed:
        desired_cmp = _upcase_enum(key, desired).lower()  # normalise case for compare
        current = _live_property(live, key).lower()
        if current != desired_cmp:
            changes[key] = _upcase_enum(key, desired)

    # Quota: compare in bytes (rawvalue) to avoid formatting round-trip issues.
    # spec None = "leave alone"; don't manage.
    if spec.quota is not None:
        desired_bytes = _parse_size(spec.quota)
        current_raw = _live_raw(live, "quota")
        try:
            current_bytes = int(current_raw) if current_raw not in ("", "none") else 0
        except (ValueError, TypeError):
            current_bytes = 0
        if current_bytes != desired_bytes:
            changes["quota"] = desired_bytes

    return changes


def ensure_dataset(cli: Any, spec: DatasetSpec, *, apply: bool) -> Diff:
    """Ensure a dataset matching `spec` exists with the right properties.

    Idempotent. Compares only managed fields; other props untouched.
    """
    existing = cli.call("pool.dataset.query", [["name", "=", spec.name]])

    if not existing:
        payload = _build_payload(spec, for_create=True)
        if apply:
            created = cli.call("pool.dataset.create", payload)
            return Diff.create(created)
        return Diff.create(payload)

    live = existing[0]
    changes = _diff_props(live, spec)
    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("pool.dataset.update", spec.name, changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/storage.yaml")


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
) -> int:
    """Phase 5: datasets — nested dataset tree with per-dataset props + quotas.

    Datasets are processed in the order they appear in storage.yaml, which
    already lists parents before children.
    """
    log = ctx.log.bind(phase="datasets")
    cfg = load_datasets_config(config_path or DEFAULT_CONFIG_PATH)

    for spec in cfg.datasets:
        if only and spec.name != only:
            continue
        diff = ensure_dataset(cli, spec, apply=ctx.apply)
        log.info(
            "dataset_ensured",
            name=spec.name,
            action=diff.action,
            changed=diff.changed,
            recordsize=spec.recordsize,
            quota=spec.quota or "none",
        )

    return 0
