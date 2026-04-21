"""Phase: shares — NFS (prd/dev) + SMB (home) with per-VLAN binding.

See docs/plans/zesty-drifting-castle.md §Phase 7.

Planned:

  NFS service `bindip = [10.10.10.10, 10.10.15.10]`
    (service does NOT listen on VLAN 5 or VLAN 20 IPs)
    * Share tank/kube/prd/longhorn  → networks=[10.10.10.0/24]  (prd cluster)
    * Share tank/kube/dev/longhorn  → networks=[10.10.15.0/24]  (dev cluster)
    (velero datasets are backed by MinIO containers — not NFS-shared directly)

  SMB service `bindip = [10.10.20.10]`
    * Share `general` → tank/shared/general, hostsallow=[10.10.20.0/24]

Note: TrueNAS 25.10 has no per-share bind-IP, only per-share ACL. Service-wide
bind + per-share CIDR ACL together give us the VLAN isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NfsServiceSpec:
    enable: bool = True
    bindip: tuple[str, ...] = ()


@dataclass(frozen=True)
class NfsShareSpec:
    path: str
    networks: tuple[str, ...] = ()
    comment: str = ""
    maproot_user: str = ""
    maproot_group: str = ""


@dataclass(frozen=True)
class SmbServiceSpec:
    enable: bool = True
    bindip: tuple[str, ...] = ()
    workgroup: str = "HOME"
    server_string: str = ""


@dataclass(frozen=True)
class SmbShareSpec:
    """SMB share spec.

    TrueNAS 25.10 removed per-share `hostsallow`/`guestok` fields. Host-level
    ACL is enforced by the service-level `bindip` (SMB only listens on
    `10.10.20.10`), so any client that can reach SMB is already on VLAN 20.
    Additional ACL is a MikroTik firewall concern, not ours.
    """
    name: str
    path: str
    purpose: str = "DEFAULT_SHARE"
    browsable: bool = True
    comment: str = ""


@dataclass(frozen=True)
class SharesConfig:
    nfs: NfsServiceSpec = field(default_factory=NfsServiceSpec)
    nfs_shares: tuple[NfsShareSpec, ...] = ()
    smb: SmbServiceSpec = field(default_factory=SmbServiceSpec)
    smb_shares: tuple[SmbShareSpec, ...] = ()


def load_shares_config(path: Path) -> SharesConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    nfs_raw = raw.get("nfs") or {}
    nfs_svc = nfs_raw.get("service") or {}
    nfs = NfsServiceSpec(
        enable=bool(nfs_svc.get("enable", True)),
        bindip=tuple(nfs_svc.get("bindip") or ()),
    )
    nfs_shares = tuple(
        NfsShareSpec(
            path=s["path"],
            networks=tuple(s.get("networks") or ()),
            comment=s.get("comment", s.get("name", "")),
            maproot_user=s.get("maproot_user", ""),
            maproot_group=s.get("maproot_group", ""),
        )
        for s in (nfs_raw.get("shares") or [])
    )

    smb_raw = raw.get("smb") or {}
    smb_svc = smb_raw.get("service") or {}
    smb = SmbServiceSpec(
        enable=bool(smb_svc.get("enable", True)),
        bindip=tuple(smb_svc.get("bindip") or ()),
        workgroup=smb_svc.get("workgroup", "HOME"),
        server_string=smb_svc.get("server_string", ""),
    )
    smb_shares = tuple(
        SmbShareSpec(
            name=s["name"],
            path=s["path"],
            purpose=(s.get("purpose") or "DEFAULT_SHARE").upper(),
            browsable=bool(s.get("browsable", True)),
            comment=s.get("comment", ""),
        )
        for s in (smb_raw.get("shares") or [])
    )

    return SharesConfig(nfs=nfs, nfs_shares=nfs_shares, smb=smb, smb_shares=smb_shares)


# ─── ensure_nfs_service / ensure_smb_service (generic service pattern) ───────


def _ensure_service(
    cli: Any,
    *,
    service_name: str,           # "nfs" or "cifs"
    config_endpoint: str,        # "nfs" or "smb"
    enable: bool,
    config_changes_probe: dict[str, Any],
    apply: bool,
) -> Diff:
    """Shared logic for NFS/SMB service-level config + service enable/start."""
    live_config = cli.call(f"{config_endpoint}.config")
    live_service = cli.call("service.query", [["service", "=", service_name]])

    changes: dict[str, Any] = {}
    for k, v in config_changes_probe.items():
        current = live_config.get(k)
        if isinstance(current, list) and isinstance(v, list):
            if sorted(current) != sorted(v):
                changes[k] = v
        elif current != v:
            changes[k] = v

    need_svc_update = bool(live_service) and live_service[0]["enable"] != enable
    need_start = enable and (not live_service or live_service[0]["state"] != "RUNNING")
    need_stop = not enable and live_service and live_service[0]["state"] == "RUNNING"

    if not changes and not need_svc_update and not need_start and not need_stop:
        return Diff.noop({"config": live_config, "service": live_service})

    if apply:
        if changes:
            cli.call(f"{config_endpoint}.update", changes)
        if need_svc_update:
            cli.call("service.update", live_service[0]["id"], {"enable": enable})
        if need_start:
            cli.call("service.start", service_name)
        elif need_stop:
            cli.call("service.stop", service_name)
    return Diff.update(
        before={"config": live_config, "service": live_service},
        after={"changes": changes, "enable": enable},
    )


def ensure_nfs_service(cli: Any, *, spec: NfsServiceSpec, apply: bool) -> Diff:
    """Ensure NFS service config + enable state."""
    return _ensure_service(
        cli,
        service_name="nfs",
        config_endpoint="nfs",
        enable=spec.enable,
        config_changes_probe={"bindip": list(spec.bindip)},
        apply=apply,
    )


def ensure_smb_service(cli: Any, *, spec: SmbServiceSpec, apply: bool) -> Diff:
    """Ensure SMB service config + enable state."""
    probe: dict[str, Any] = {"bindip": list(spec.bindip)}
    if spec.workgroup:
        probe["workgroup"] = spec.workgroup
    return _ensure_service(
        cli,
        service_name="cifs",
        config_endpoint="smb",
        enable=spec.enable,
        config_changes_probe=probe,
        apply=apply,
    )


# ─── ensure_nfs_share ────────────────────────────────────────────────────────


def ensure_nfs_share(cli: Any, *, spec: NfsShareSpec, apply: bool) -> Diff:
    """Ensure an NFS share at `spec.path` with the given networks ACL."""
    existing = cli.call("sharing.nfs.query", [["path", "=", spec.path]])

    payload: dict[str, Any] = {
        "path": spec.path,
        "networks": list(spec.networks),
        "hosts": [],
        "comment": spec.comment,
        "enabled": True,
    }
    if spec.maproot_user:
        payload["maproot_user"] = spec.maproot_user
    if spec.maproot_group:
        payload["maproot_group"] = spec.maproot_group

    if not existing:
        if apply:
            created = cli.call("sharing.nfs.create", payload)
            return Diff.create(created)
        return Diff.create(payload)

    live = existing[0]
    changes: dict[str, Any] = {}
    if sorted(live.get("networks") or []) != sorted(spec.networks):
        changes["networks"] = list(spec.networks)
    if spec.comment and live.get("comment", "") != spec.comment:
        changes["comment"] = spec.comment
    if spec.maproot_user and live.get("maproot_user", "") != spec.maproot_user:
        changes["maproot_user"] = spec.maproot_user
    if spec.maproot_group and live.get("maproot_group", "") != spec.maproot_group:
        changes["maproot_group"] = spec.maproot_group

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("sharing.nfs.update", live["id"], changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── ensure_smb_share ────────────────────────────────────────────────────────


def ensure_smb_share(cli: Any, *, spec: SmbShareSpec, apply: bool) -> Diff:
    existing = cli.call("sharing.smb.query", [["name", "=", spec.name]])

    payload: dict[str, Any] = {
        "name": spec.name,
        "path": spec.path,
        "purpose": spec.purpose,
        "browsable": spec.browsable,
        "comment": spec.comment,
        "enabled": True,
    }

    if not existing:
        if apply:
            created = cli.call("sharing.smb.create", payload)
            return Diff.create(created)
        return Diff.create(payload)

    live = existing[0]
    changes: dict[str, Any] = {}
    if live.get("path") != spec.path:
        changes["path"] = spec.path
    if live.get("purpose", "DEFAULT_SHARE") != spec.purpose:
        changes["purpose"] = spec.purpose
    if bool(live.get("browsable")) != spec.browsable:
        changes["browsable"] = spec.browsable
    if spec.comment and live.get("comment", "") != spec.comment:
        changes["comment"] = spec.comment

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("sharing.smb.update", live["id"], changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/shares.yaml")


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
) -> int:
    log = ctx.log.bind(phase="shares")
    cfg = load_shares_config(config_path or DEFAULT_CONFIG_PATH)

    # NFS service first, then shares.
    diff = ensure_nfs_service(cli, spec=cfg.nfs, apply=ctx.apply)
    log.info("nfs_service_ensured", action=diff.action, changed=diff.changed,
             bindip=list(cfg.nfs.bindip))
    for share in cfg.nfs_shares:
        diff = ensure_nfs_share(cli, spec=share, apply=ctx.apply)
        log.info("nfs_share_ensured", path=share.path, networks=list(share.networks),
                 action=diff.action, changed=diff.changed)

    # SMB service, then shares.
    diff = ensure_smb_service(cli, spec=cfg.smb, apply=ctx.apply)
    log.info("smb_service_ensured", action=diff.action, changed=diff.changed,
             bindip=list(cfg.smb.bindip))
    for share in cfg.smb_shares:
        diff = ensure_smb_share(cli, spec=share, apply=ctx.apply)
        log.info("smb_share_ensured", name=share.name, path=share.path,
                 action=diff.action, changed=diff.changed)

    return 0
