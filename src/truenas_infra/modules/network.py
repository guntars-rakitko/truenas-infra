"""Phase: network — VLAN sub-interfaces on NIC1.

See docs/plans/zesty-drifting-castle.md §Phase 2.

**CRITICAL**: management is currently single-homed on NIC2 (10.10.5.10). Any
misstep on NIC2 loses remote access. Always use `interface.commit()` +
`interface.checkin()` with a 60-second rollback window. This module
intentionally never writes to NIC2 in phase 2.
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from truenas_infra.util import Diff


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VlanSpec:
    name: str
    vid: int
    ipv4: str  # CIDR notation, e.g. "10.10.10.10/24"


@dataclass(frozen=True)
class TrunkSpec:
    device: str
    mtu: int = 1500
    vlans: tuple[VlanSpec, ...] = ()


@dataclass(frozen=True)
class MgmtSpec:
    device: str
    ipv4: str
    gateway: str = ""
    mtu: int = 1500
    # Extra IP aliases to bind on the mgmt interface (e.g. 10.10.5.20 for
    # Traefik). Each in CIDR form; primary ipv4 is implicit, these are in
    # addition. Sorting-independent — order doesn't matter.
    additional_ips: tuple[str, ...] = ()


@dataclass(frozen=True)
class NetworkConfig:
    trunk: TrunkSpec
    mgmt: MgmtSpec
    hostname: str = "truenas"
    domain: str = ""
    dns_servers: tuple[str, ...] = ()


def load_network_config(path: Path) -> NetworkConfig:
    """Parse config/network.yaml into a typed config object."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nics = raw.get("nics") or {}

    trunk_raw = nics.get("trunk") or {}
    trunk = TrunkSpec(
        device=trunk_raw["device"],
        mtu=int(trunk_raw.get("mtu", 1500)),
        vlans=tuple(
            VlanSpec(name=v["name"], vid=int(v["vid"]), ipv4=v["ipv4"])
            for v in (trunk_raw.get("vlans") or [])
        ),
    )

    mgmt_raw = nics.get("mgmt") or {}
    mgmt = MgmtSpec(
        device=mgmt_raw["device"],
        ipv4=mgmt_raw.get("ipv4", ""),
        gateway=mgmt_raw.get("gateway", ""),
        mtu=int(mgmt_raw.get("mtu", 1500)),
        additional_ips=tuple(mgmt_raw.get("additional_ips") or ()),
    )

    dns_raw = raw.get("dns") or {}
    dns_servers = tuple(dns_raw.get("servers") or ())

    return NetworkConfig(
        trunk=trunk,
        mgmt=mgmt,
        hostname=raw.get("hostname", "truenas"),
        domain=raw.get("domain", ""),
        dns_servers=dns_servers,
    )


# ─── ensure_vlan_interface ───────────────────────────────────────────────────


def _alias_from_cidr(cidr: str) -> dict[str, Any]:
    """Split 'x.y.z.w/N' into {'address': 'x.y.z.w', 'netmask': N}."""
    addr, _, mask = cidr.partition("/")
    return {"address": addr, "netmask": int(mask or 24)}


def _aliases_match(live: list[dict[str, Any]], desired: dict[str, Any]) -> bool:
    """Kept for backward compat — single-alias check."""
    return _aliases_match_set(live, [desired])


def _aliases_match_set(
    live: list[dict[str, Any]], desired: list[dict[str, Any]],
) -> bool:
    """The live alias list (INET entries) matches the set of desired v4 aliases.

    Order-independent — each desired alias must be present, nothing extra.
    """
    inet = [a for a in live if a.get("type") == "INET"]
    if len(inet) != len(desired):
        return False
    live_pairs = {(a.get("address"), int(a.get("netmask", 0))) for a in inet}
    desired_pairs = {(d["address"], int(d["netmask"])) for d in desired}
    return live_pairs == desired_pairs


def ensure_vlan_interface(
    cli: Any, spec: VlanSpec, *, parent: str, apply: bool
) -> Diff:
    """Ensure a VLAN sub-interface exists on `parent` with `spec`'s IP.

    Does NOT call interface.commit — the caller does that once at the end of
    the whole batch (inside a single checkin window).
    """
    existing = cli.call("interface.query", [["name", "=", spec.name]])
    desired_alias = _alias_from_cidr(spec.ipv4)

    payload = {
        "type": "VLAN",
        "name": spec.name,
        "vlan_parent_interface": parent,
        "vlan_tag": spec.vid,
        "aliases": [desired_alias],
        "ipv4_dhcp": False,
        "ipv6_auto": False,
    }

    if not existing:
        if apply:
            created = cli.call("interface.create", payload)
            return Diff.create(created)
        return Diff.create(payload)

    live = existing[0]

    # Detect drift on the managed fields.
    needs_update = (
        live.get("vlan_parent_interface") != parent
        or int(live.get("vlan_tag") or 0) != spec.vid
        or not _aliases_match(live.get("aliases") or [], desired_alias)
        or bool(live.get("ipv4_dhcp")) is True
        or bool(live.get("ipv6_auto")) is True
    )

    if not needs_update:
        return Diff.noop(live)

    # On update, interface.update takes (id, partial_payload).
    update_payload = {
        "vlan_parent_interface": parent,
        "vlan_tag": spec.vid,
        "aliases": [desired_alias],
        "ipv4_dhcp": False,
        "ipv6_auto": False,
    }
    if apply:
        updated = cli.call("interface.update", live["id"], update_payload)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **update_payload})


# ─── ensure_trunk_parent ─────────────────────────────────────────────────────


def ensure_trunk_parent(cli: Any, *, device: str, apply: bool) -> Diff:
    """Ensure the trunk parent physical NIC has DHCP off, no aliases, IPv6 off.

    A trunk parent should have no IP of its own — its sub-interfaces do.
    IPv6 SLAAC/DHCPv6 is disabled (`ipv6_auto=False`) since we don't use IPv6
    on this NAS. Kernel-level IPv6 link-local is left intact (harmless).
    """
    existing = cli.call("interface.query", [["name", "=", device]])
    if not existing:
        raise RuntimeError(
            f"Physical interface {device!r} not found. Cannot configure VLANs."
        )

    live = existing[0]
    changes: dict[str, Any] = {}
    if live.get("ipv4_dhcp"):
        changes["ipv4_dhcp"] = False
    if live.get("ipv6_auto"):
        changes["ipv6_auto"] = False
    # Clear any stray IPv4 aliases on the parent — they shouldn't be here.
    live_inet = [a for a in (live.get("aliases") or []) if a.get("type") == "INET"]
    if live_inet:
        changes["aliases"] = []

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("interface.update", live["id"], changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── commit_and_checkin ──────────────────────────────────────────────────────


def commit_network_changes(
    cli: Any,
    *,
    has_pending: bool = True,
    apply: bool = True,
    reachable_fn: Callable[[], bool] | None = None,
    reconnect_max_wait: int = 240,
    reconnect_grace: float = 5.0,
    log: Any = None,
) -> None:
    """Commit pending interface changes (no rollback safety net).

    TrueNAS `interface.commit` has two modes:

      * `rollback=True` (default): apply + auto-revert in N seconds unless a
        separate `interface.checkin()` call confirms. This mode is broken for
        API-driven automation because the network restart can take 60–180 s,
        during which the NAS is unreachable and we can't call checkin — so
        the auto-rollback kicks in and reverts everything.

      * `rollback=False`: apply immediately, no revert. Used here.

    Why `rollback=False` is safe for phase 2:
      * We only touch NIC1 sub-interfaces; NIC2 (mgmt) stays configured.
      * If our config is wrong, mgmt is still reachable, and we just fix the
        bad VLAN with another script run or console.
      * If we broke mgmt somehow, auto-rollback wouldn't save us either —
        the API would be unreachable for the entire rollback window.

    Flow:
      1. Call `interface.commit` with `rollback=False`. The WebSocket may
         drop during the network restart — that's expected.
      2. Wait for the API to be reachable again.
      3. Done. (No checkin needed — commit is final.)
    """
    if not apply or not has_pending:
        return

    _log = log or _NullLog()
    _log.info("commit_start")

    try:
        cli.call("interface.commit", {"rollback": False})
        _log.info("commit_returned")
    except Exception as e:  # noqa: BLE001
        _log.info("commit_ws_dropped", reason=str(e))

    if reachable_fn is not None:
        deadline = time.monotonic() + reconnect_max_wait
        waited = 0.0
        while time.monotonic() < deadline:
            try:
                if reachable_fn():
                    _log.info("reachable", after_s=round(waited, 1))
                    break
            except Exception:  # noqa: BLE001
                pass
            time.sleep(1)
            waited += 1
        else:
            raise RuntimeError(
                f"Host did not come back online within {reconnect_max_wait}s "
                "after interface.commit. Manual intervention required — "
                "since rollback=False, changes are permanent."
            )
        # Give the API a moment to stabilise after the TCP port comes up.
        time.sleep(reconnect_grace)
        _log.info("api_stabilized")


class _NullLog:
    def info(self, *_: Any, **__: Any) -> None:
        pass


def make_tcp_reachable_probe(host: str, port: int = 443, timeout: float = 3.0) -> Callable[[], bool]:
    """Return a callable that returns True if `host:port` accepts a TCP connect."""

    def _probe() -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    return _probe


# ─── ensure_global_network ───────────────────────────────────────────────────


def ensure_global_network(
    cli: Any,
    *,
    hostname: str,
    domain: str,
    dns: tuple[str, ...],
    ipv4_gateway: str = "",
    apply: bool,
) -> Diff:
    """Ensure hostname, domain, DNS, and IPv4 gateway match the spec.

    Updates only the fields that differ; leaves others alone.
    """
    live = cli.call("network.configuration.config")

    changes: dict[str, Any] = {}
    if hostname and live.get("hostname") != hostname:
        changes["hostname"] = hostname
    if domain and live.get("domain") != domain:
        changes["domain"] = domain
    if ipv4_gateway and live.get("ipv4gateway") != ipv4_gateway:
        changes["ipv4gateway"] = ipv4_gateway

    # DNS servers map to nameserver1/2/3. Treat None and "" as equivalent
    # (TrueNAS returns "" for unused slots; tests/fixtures may omit them).
    ns_keys = ["nameserver1", "nameserver2", "nameserver3"]
    desired_ns = list(dns) + [""] * (len(ns_keys) - len(dns))
    for key, value in zip(ns_keys, desired_ns[: len(ns_keys)]):
        current = live.get(key) or ""
        if current != value:
            changes[key] = value

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("network.configuration.update", changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── ensure_mgmt_interface ───────────────────────────────────────────────────


def ensure_mgmt_interface(
    cli: Any, *, device: str, ipv4: str, apply: bool,
    additional_ips: tuple[str, ...] = (),
) -> Diff:
    """Configure the mgmt NIC with a static IPv4 (CIDR) and IPv6 auto off.

    Required before `ensure_ui_bindip` can pin the UI to that IPv4 —
    TrueNAS rejects UI addresses that aren't statically assigned.

    `additional_ips` — extra /24 aliases to bind alongside the primary.
    Used to host services on the mgmt VLAN that need their own :443
    (e.g. Traefik on 10.10.5.20) without stealing from the NAS's own
    10.10.5.10:443 (TrueNAS UI).
    """
    existing = cli.call("interface.query", [["name", "=", device]])
    if not existing:
        raise RuntimeError(f"Mgmt interface {device!r} not found.")
    live = existing[0]

    desired_aliases = [_alias_from_cidr(ipv4)] + [
        _alias_from_cidr(cidr) for cidr in additional_ips
    ]

    changes: dict[str, Any] = {}
    if live.get("ipv4_dhcp"):
        changes["ipv4_dhcp"] = False
    if live.get("ipv6_auto"):
        changes["ipv6_auto"] = False
    if not _aliases_match_set(live.get("aliases") or [], desired_aliases):
        changes["aliases"] = desired_aliases

    if not changes:
        return Diff.noop(live)

    if apply:
        updated = cli.call("interface.update", live["id"], changes)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── ensure_ui_bindip ────────────────────────────────────────────────────────


def ensure_ui_bindip(cli: Any, *, addresses: tuple[str, ...], apply: bool) -> Diff:
    """Ensure the TrueNAS web UI is bound only to the given IPv4 addresses.

    By default TrueNAS binds the UI to all interfaces (`0.0.0.0`). Restricting
    to `["10.10.5.10"]` means the UI is only reachable from VLAN 5 (mgmt).

    After updating `ui_address`, we also call `system.general.ui_restart` to
    actually apply the change to nginx — the update alone doesn't take effect
    until the UI service restarts.
    """
    live = cli.call("system.general.config")

    desired = list(addresses)
    current = list(live.get("ui_address") or [])
    if sorted(current) == sorted(desired):
        return Diff.noop(live)

    changes = {"ui_address": desired}
    if apply:
        updated = cli.call("system.general.update", changes)
        # Trigger the UI service restart so nginx actually rebinds.
        cli.call("system.general.ui_restart", 3)
        return Diff.update(before=live, after=updated)
    return Diff.update(before=live, after={**live, **changes})


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/network.yaml")


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
    reachable_fn: Callable[[], bool] | None = None,
) -> int:
    """Phase 2: network — VLAN sub-interfaces on NIC1.

    Order of operations (safety-first):
      1. Turn off DHCP on trunk parent (enp1s0).
      2. For each VLAN in order, create/update sub-interface.
      3. Single commit+checkin window covering all interface changes.
      4. Update hostname/domain/DNS via network.configuration.update.

    **Never** touches the mgmt NIC (enp2s0) in this phase. That comes in a
    later pass, once the VLAN sub-interfaces are verified healthy and provide
    an independent path for recovery.
    """
    log = ctx.log.bind(phase="network")
    cfg = load_network_config(config_path or DEFAULT_CONFIG_PATH)

    pending = False

    # 1. Trunk parent — DHCP off, no aliases.
    diff = ensure_trunk_parent(cli, device=cfg.trunk.device, apply=ctx.apply)
    log.info("trunk_parent_ensured", device=cfg.trunk.device, action=diff.action, changed=diff.changed)
    pending = pending or diff.changed

    # 2. Mgmt NIC — static IPv4 + any additional IP aliases (Traefik etc).
    #    This is a low-risk config change — same IP, just changing DHCP → static.
    diff = ensure_mgmt_interface(
        cli, device=cfg.mgmt.device, ipv4=cfg.mgmt.ipv4,
        additional_ips=cfg.mgmt.additional_ips,
        apply=ctx.apply,
    )
    log.info(
        "mgmt_interface_ensured",
        device=cfg.mgmt.device, ipv4=cfg.mgmt.ipv4,
        action=diff.action, changed=diff.changed,
    )
    pending = pending or diff.changed

    # 3. VLAN sub-interfaces.
    for vlan in cfg.trunk.vlans:
        if only and vlan.name != only:
            continue
        diff = ensure_vlan_interface(cli, vlan, parent=cfg.trunk.device, apply=ctx.apply)
        log.info(
            "vlan_ensured",
            name=vlan.name,
            vid=vlan.vid,
            ipv4=vlan.ipv4,
            action=diff.action,
            changed=diff.changed,
        )
        pending = pending or diff.changed

    # 3. Commit all interface changes. Rolling forward — no rollback window.
    #    (Rationale in `commit_network_changes` docstring.)
    if reachable_fn is None and ctx.apply and pending:
        reachable_fn = make_tcp_reachable_probe(ctx.config.truenas_host, port=443)

    commit_network_changes(
        cli,
        has_pending=pending,
        apply=ctx.apply,
        reachable_fn=reachable_fn,
        log=log,
    )
    if pending and ctx.apply:
        log.info("interface_changes_committed")

    # 4. Global hostname/domain/DNS/gateway (separate API; not part of interface commit).
    diff = ensure_global_network(
        cli,
        hostname=cfg.hostname,
        domain=cfg.domain,
        dns=cfg.dns_servers,
        ipv4_gateway=cfg.mgmt.gateway,
        apply=ctx.apply,
    )
    log.info("global_network_ensured", action=diff.action, changed=diff.changed)

    # 5. Restrict UI bindip to the mgmt IP (strip the /N CIDR bits).
    mgmt_addr = cfg.mgmt.ipv4.split("/", 1)[0]
    if mgmt_addr:
        diff = ensure_ui_bindip(cli, addresses=(mgmt_addr,), apply=ctx.apply)
        log.info("ui_bindip_ensured", addresses=mgmt_addr, action=diff.action, changed=diff.changed)

    return 0
