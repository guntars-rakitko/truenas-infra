#!/usr/bin/env python3
"""Fleet-wide AMT configuration audit.

Queries every node in apps/amtctl/nodes.yaml for the fields we're
considering standardising, and prints a diff matrix so we can see
which nodes drift from the pack.

Read-only. Safe to run against production.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AMTCTL_APP = REPO_ROOT / "apps" / "amtctl"
SECRETS_FILE = AMTCTL_APP / "secrets.sops.yaml"
NODES_FILE = AMTCTL_APP / "nodes.yaml"

sys.path.insert(0, str(AMTCTL_APP))
from amt import AMTClient, AMTError  # type: ignore  # noqa: E402

import yaml  # noqa: E402

CIM = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2"
AMT = "http://intel.com/wbem/wscim/1/amt-schema/1"
IPS = "http://intel.com/wbem/wscim/1/ips-schema/1"

# (class_short, uri, [fields_to_pick], instance_filter_fn or None)
# instance_filter_fn: for classes with multiple instances, pick one.
# Returns the dict of the matched instance or None.
AUDIT_FIELDS: list[tuple[str, str, list[str], object]] = [
    # "clock" section injected by _query_node via a direct method call
    # below — not enumerable via _enum_pull.
    ("clock", "<live-clock>", ["LiveAmtUtc"], None),
    ("general", f"{AMT}/AMT_GeneralSettings", [
        "HostName", "DomainName", "HostOSFQDN", "SharedFQDN",
        "WsmanOnlyMode", "DDNSUpdateEnabled",
        "PingResponseEnabled", "RmcpPingResponseEnabled",
        "NetworkInterfaceEnabled", "AMTNetworkEnabled",
        "IdleWakeTimeout", "PrivacyLevel",
    ], None),
    ("ethernet", f"{AMT}/AMT_EthernetPortSettings", [
        "DHCPEnabled", "IPAddress", "DefaultGateway", "PrimaryDNS",
        "SharedMAC", "SharedDynamicIP", "LinkPolicy", "IpSyncEnabled",
    ], None),
    ("redirection", f"{AMT}/AMT_RedirectionService", [
        "EnabledState", "ListenerEnabled",
    ], None),
    ("kvm", f"{IPS}/IPS_KVMRedirectionSettingData", [
        "OptInPolicy", "SessionTimeout", "Is5900PortEnabled",
        "DefaultScreen", "EnabledByMEBx",
    ], None),
    ("webui", f"{AMT}/AMT_WebUIService", [
        "EnabledState",
    ], None),
    ("audit_log", f"{AMT}/AMT_AuditLog", [
        "Datetime", "CurrentNumberOfRecords", "PercentageFree",
    ], None),
    ("time", f"{AMT}/AMT_TimeSynchronizationService", [
        "LocalTimeSyncEnabled", "TimeSource", "EnabledState",
    ], None),
    ("tls_8023", f"{AMT}/AMT_TLSSettingData", [
        "Enabled", "AcceptNonSecureConnections", "MutualAuthentication",
    ], lambda inst: inst.get("InstanceID", "").endswith("802.3 TLS Settings")),
    ("tls_lms", f"{AMT}/AMT_TLSSettingData", [
        "Enabled", "AcceptNonSecureConnections", "MutualAuthentication",
    ], lambda inst: inst.get("InstanceID", "").endswith("LMS TLS Settings")),
    ("control", f"{IPS}/IPS_HostBasedSetupService", [
        "CurrentControlMode", "AllowedControlModes",
    ], None),
]


def _load_creds() -> tuple[str, str]:
    raw = subprocess.check_output(["sops", "-d", str(SECRETS_FILE)], text=True)
    u = re.search(r"^AMTCTL_AMT_USER:\s*(.+)$", raw, re.MULTILINE)
    p = re.search(r"^AMTCTL_AMT_PASSWORD:\s*(.+)$", raw, re.MULTILINE)
    assert u and p, "missing AMT creds in SOPS"
    return u.group(1).strip(), p.group(1).strip().strip('"')


def _load_nodes() -> list[dict[str, str]]:
    with NODES_FILE.open() as f:
        doc = yaml.safe_load(f)
    return doc["nodes"]


async def _get_live_amt_time(client: AMTClient) -> str:
    """Call GetLowAccuracyTimeSynch to read AMT's actual current clock —
    more useful than AMT_AuditLog.Datetime (which is the last-record
    timestamp, not the live clock)."""
    import datetime as _dt
    uri = f"{AMT}/AMT_TimeSynchronizationService"
    try:
        r = await client._post(
            f"{uri}/GetLowAccuracyTimeSynch", uri,
            f'<g:GetLowAccuracyTimeSynch_INPUT xmlns:g="{uri}"/>',
        )
        m = re.search(r"<[a-z0-9]+:Ta0>(\d+)</", r)
        if m:
            ts = int(m.group(1))
            return _dt.datetime.fromtimestamp(ts, tz=_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:  # noqa: BLE001
        pass
    return "?"


async def _query_node(name: str, host: str, user: str, password: str
                      ) -> dict[str, dict[str, str]]:
    """Returns {section_name: {field: value}} for one node."""
    out: dict[str, dict[str, str]] = {}
    try:
        async with AMTClient(host, user, password) as c:
            # Live AMT clock (can't be inferred from Enumerate)
            out["clock"] = {"LiveAmtUtc": await _get_live_amt_time(c)}
            for section, uri, fields, inst_filter in AUDIT_FIELDS:
                if uri == "<live-clock>":
                    continue  # handled above, skip Enumerate
                try:
                    instances = await c._enum_pull(uri)
                except AMTError as e:
                    out[section] = {"__error__": f"{e.kind}: {e.detail[:40]}"}
                    continue
                if not instances:
                    out[section] = {"__error__": "no instances"}
                    continue
                if inst_filter:
                    inst = next((i for i in instances if inst_filter(i)), None)
                    if inst is None:
                        out[section] = {"__error__": "filter matched none"}
                        continue
                else:
                    inst = instances[0]
                out[section] = {f: inst.get(f, "—") for f in fields}
    except AMTError as e:
        out["__global_error__"] = {"err": f"{e.kind}: {e.detail[:60]}"}
    return out


async def run() -> None:
    user, password = _load_creds()
    nodes = _load_nodes()
    print(f"Fleet AMT audit — {len(nodes)} nodes")
    print("=" * 80)

    results: dict[str, dict[str, dict[str, str]]] = {}
    # Serialise on purpose — AMT is slow and parallel hits can trip timeouts
    for n in nodes:
        name = n["name"]
        host = n["host"]
        print(f"  querying {name} ({host}) …", flush=True)
        results[name] = await _query_node(name, host, user, password)

    # ─── Emit diff matrix, per (section, field) ───
    # Columns = node names (ordered)
    node_names = [n["name"] for n in nodes]
    print()
    print("=" * 80)
    print("Diff matrix (→ columns per node, ✓ = matches prd-01, ✗ = differs)")
    print("=" * 80)
    print()

    ref_node = node_names[0]
    for section, _uri, fields, _inst_filter in AUDIT_FIELDS:
        print(f"## {section}")
        header_cols = "  ".join(f"{n[-2:]:>5}" for n in node_names)
        print(f"  {'field':<35} {header_cols}")
        print(f"  {'-' * 35} {'-' * len(header_cols)}")
        for f in fields:
            row = []
            ref_val = results[ref_node].get(section, {}).get(f, "—")
            for nn in node_names:
                v = results[nn].get(section, {}).get(f, "—")
                if v == "—":
                    cell = "  -- "
                elif v == ref_val:
                    cell = f"{v[:5]:>5}" if len(v) <= 5 else f"{v[:4]:>4}…"
                else:
                    cell = f"[{v[:3]}]" if len(v) <= 5 else f"[{v[:3]}…]"
                row.append(f"{cell:>5}")
            cells = "  ".join(row)
            print(f"  {f:<35} {cells}")
        print()

    # ─── Also emit full per-node values for anything that differs ───
    print()
    print("=" * 80)
    print("Full values for fields that differ across fleet")
    print("=" * 80)
    any_drift = False
    for section, _uri, fields, _inst_filter in AUDIT_FIELDS:
        for f in fields:
            vals = {
                nn: results[nn].get(section, {}).get(f, "—")
                for nn in node_names
            }
            unique = set(v for v in vals.values() if v != "—")
            if len(unique) > 1:
                any_drift = True
                print(f"\n{section}.{f}:")
                for nn in node_names:
                    print(f"  {nn}: {vals[nn]}")
    if not any_drift:
        print("  (all fields consistent — fleet is uniform)")
    print()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
