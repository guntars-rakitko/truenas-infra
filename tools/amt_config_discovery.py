#!/usr/bin/env python3
"""AMT configuration-surface discovery.

Connects to one node's AMT ME firmware and enumerates every known
"configuration" WS-MAN class (as opposed to monitoring / status
classes, which amtctl already covers in amt.py). For each class
+ instance, prints every property with its current value so we
can decide what's worth wrapping in a `provision.py` tool.

Read-only. Safe to run against production.

Usage:
    python tools/amt_config_discovery.py 10.10.5.11
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
AMTCTL_APP = REPO_ROOT / "apps" / "amtctl"
SECRETS_FILE = AMTCTL_APP / "secrets.sops.yaml"

# Make amt.py importable. It lives under apps/amtctl, which isn't on
# sys.path by default.
sys.path.insert(0, str(AMTCTL_APP))

from amt import AMTClient, AMTError  # type: ignore  # noqa: E402

# ─── WS-MAN class URIs ──────────────────────────────────────────────────

CIM = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2"
AMT = "http://intel.com/wbem/wscim/1/amt-schema/1"
IPS = "http://intel.com/wbem/wscim/1/ips-schema/1"

# Ordered list of (category, short-name, full-URI) tuples. The short
# name is what shows in the report; full URI is what actually gets
# queried.
TARGETS: list[tuple[str, str, str]] = [
    # ─── Core identity + network ───
    ("Identity", "AMT_GeneralSettings", f"{AMT}/AMT_GeneralSettings"),
    ("Identity", "CIM_ComputerSystem", f"{CIM}/CIM_ComputerSystem"),
    ("Network", "AMT_EthernetPortSettings", f"{AMT}/AMT_EthernetPortSettings"),
    ("Network", "CIM_IPProtocolEndpoint", f"{CIM}/CIM_IPProtocolEndpoint"),
    ("Network", "AMT_EnvironmentDetectionSettingData",
     f"{AMT}/AMT_EnvironmentDetectionSettingData"),

    # ─── Remote-access / CIRA ───
    ("Remote access", "AMT_UserInitiatedConnectionService",
     f"{AMT}/AMT_UserInitiatedConnectionService"),
    ("Remote access", "AMT_RemoteAccessPolicyRule",
     f"{AMT}/AMT_RemoteAccessPolicyRule"),
    ("Remote access", "AMT_RemoteAccessPolicyAppliesToMPS",
     f"{AMT}/AMT_RemoteAccessPolicyAppliesToMPS"),
    ("Remote access", "AMT_MPSUsernamePassword",
     f"{AMT}/AMT_MPSUsernamePassword"),

    # ─── KVM / SOL / IDER redirection ───
    ("Redirection", "AMT_RedirectionService", f"{AMT}/AMT_RedirectionService"),
    ("Redirection", "IPS_KVMRedirectionSettingData",
     f"{IPS}/IPS_KVMRedirectionSettingData"),
    ("Redirection", "CIM_KVMRedirectionSAP", f"{CIM}/CIM_KVMRedirectionSAP"),

    # ─── Power policies ───
    ("Power", "CIM_PowerManagementService", f"{CIM}/CIM_PowerManagementService"),
    ("Power", "AMT_BootCapabilities", f"{AMT}/AMT_BootCapabilities"),
    ("Power", "CIM_BootConfigSetting", f"{CIM}/CIM_BootConfigSetting"),
    ("Power", "CIM_BootSourceSetting", f"{CIM}/CIM_BootSourceSetting"),

    # ─── Time ───
    ("Time", "AMT_TimeSynchronizationService",
     f"{AMT}/AMT_TimeSynchronizationService"),

    # ─── Security / setup ───
    ("Setup", "AMT_SetupAndConfigurationService",
     f"{AMT}/AMT_SetupAndConfigurationService"),
    ("Setup", "IPS_HostBasedSetupService", f"{IPS}/IPS_HostBasedSetupService"),
    ("Setup", "AMT_AuthorizationService", f"{AMT}/AMT_AuthorizationService"),

    # ─── Web UI + certs ───
    ("Security", "AMT_WebUIService", f"{AMT}/AMT_WebUIService"),
    ("Security", "AMT_TLSSettingData", f"{AMT}/AMT_TLSSettingData"),
    ("Security", "AMT_PublicKeyCertificate", f"{AMT}/AMT_PublicKeyCertificate"),

    # ─── Logging ───
    ("Logging", "AMT_MessageLog", f"{AMT}/AMT_MessageLog"),
    ("Logging", "AMT_AuditLog", f"{AMT}/AMT_AuditLog"),
]


def _load_creds() -> tuple[str, str]:
    """sops-decrypt the amtctl secrets and extract user + password."""
    if not SECRETS_FILE.exists():
        sys.exit(f"Secrets file not found: {SECRETS_FILE}")
    try:
        raw = subprocess.check_output(
            ["sops", "-d", str(SECRETS_FILE)], text=True
        )
    except subprocess.CalledProcessError as e:
        sys.exit(f"sops decrypt failed: {e}")
    u = re.search(r"^AMTCTL_AMT_USER:\s*(.+)$", raw, re.MULTILINE)
    p = re.search(r"^AMTCTL_AMT_PASSWORD:\s*(.+)$", raw, re.MULTILINE)
    if not u or not p:
        sys.exit("AMT creds not found in SOPS file")
    return u.group(1).strip(), p.group(1).strip().strip('"')


async def _dump_class(client: AMTClient, uri: str) -> list[dict[str, str]] | str:
    """Enumerate a class. Returns list-of-dicts on success, error-string
    on AMT fault so the report can still render the other classes."""
    try:
        return await client._enum_pull(uri)
    except AMTError as e:
        return f"ERROR: {e.kind}: {e.detail}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"


def _fmt_instance(inst: dict[str, str]) -> list[str]:
    """Format one instance dict as indented property lines."""
    lines: list[str] = []
    # Sort by key for stable output
    width = max((len(k) for k in inst), default=0)
    for k in sorted(inst):
        v = inst[k]
        # Collapse long base64 / hex blobs to their length for readability
        if len(v) > 120:
            v = f"<{len(v)} chars: {v[:60]}…>"
        lines.append(f"      {k:<{width}} = {v}")
    return lines


async def run(host: str) -> None:
    user, password = _load_creds()
    print(f"AMT config-surface discovery")
    print(f"Target : {host}")
    print(f"User   : {user}")
    print("=" * 72)
    print()

    async with AMTClient(host, user, password) as c:
        current_category = ""
        for category, name, uri in TARGETS:
            if category != current_category:
                print()
                print(f"## {category}")
                print()
                current_category = category

            result = await _dump_class(c, uri)
            if isinstance(result, str):
                print(f"### {name}")
                print(f"  {result}")
                print()
                continue

            instances = result
            if not instances:
                print(f"### {name}  (no instances)")
                print()
                continue

            print(f"### {name}  ({len(instances)} instance"
                  f"{'s' if len(instances) != 1 else ''})")
            for i, inst in enumerate(instances, 1):
                if len(instances) > 1:
                    print(f"  instance #{i}:")
                for line in _fmt_instance(inst):
                    print(line)
            print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("host", help="AMT IP (e.g. 10.10.5.11)")
    args = ap.parse_args()
    asyncio.run(run(args.host))


if __name__ == "__main__":
    main()
