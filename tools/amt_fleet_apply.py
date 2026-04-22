#!/usr/bin/env python3
"""Apply canonical AMT state to every node in apps/amtctl/nodes.yaml.

Reads apps/amtctl/canonical.yaml + apps/amtctl/nodes.yaml. For each
node:

  1. Put AMT_GeneralSettings (canonical fleet-wide fields + per-node
     HostName derived from the node name).
  2. Invoke AMT_TimeSynchronizationService.SetHighAccuracyTimeSynch
     to force AMT clock to current UTC.

Puts are read-modify-write: only fields that differ from canonical
are re-sent. No-op if everything matches.

Usage:
    python tools/amt_fleet_apply.py --dry-run       # preview all changes
    python tools/amt_fleet_apply.py --dry-run --node kub-dev-03
    python tools/amt_fleet_apply.py --apply --node kub-dev-03
    python tools/amt_fleet_apply.py --apply         # all 6 nodes
"""

from __future__ import annotations

import argparse
import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
AMTCTL_APP = REPO_ROOT / "apps" / "amtctl"
SECRETS_FILE = AMTCTL_APP / "secrets.sops.yaml"
NODES_FILE = AMTCTL_APP / "nodes.yaml"
CANONICAL_FILE = AMTCTL_APP / "canonical.yaml"

sys.path.insert(0, str(AMTCTL_APP))
from amt import AMTClient, AMTError  # type: ignore  # noqa: E402

import yaml  # noqa: E402

# WS-MAN URIs for the classes we manage
AMT_NS = "http://intel.com/wbem/wscim/1/amt-schema/1"


def _load_creds() -> tuple[str, str]:
    raw = subprocess.check_output(["sops", "-d", str(SECRETS_FILE)], text=True)
    u = re.search(r"^AMTCTL_AMT_USER:\s*(.+)$", raw, re.MULTILINE)
    p = re.search(r"^AMTCTL_AMT_PASSWORD:\s*(.+)$", raw, re.MULTILINE)
    assert u and p, "missing AMT creds in SOPS"
    return u.group(1).strip(), p.group(1).strip().strip('"')


def _load_nodes() -> list[dict[str, str]]:
    with NODES_FILE.open() as f:
        return yaml.safe_load(f)["nodes"]


def _load_canonical() -> dict[str, Any]:
    with CANONICAL_FILE.open() as f:
        return yaml.safe_load(f)


def _build_class_overlay(cls: dict[str, Any], node_name: str
                          ) -> dict[str, str]:
    """Return the overlay dict for one class spec, injecting per-node
    HostName if the spec opts in."""
    overlay = {k: str(v) for k, v in cls["fields"].items()}
    if cls.get("per_node_hostname"):
        overlay["HostName"] = node_name
    return overlay


async def _apply_one_class(
    c: AMTClient, cls: dict[str, Any], node_name: str, apply: bool,
) -> dict[str, Any]:
    """Run one Put step. Returns a step report dict."""
    name = cls["name"]
    uri = cls["uri"]
    overlay = _build_class_overlay(cls, node_name)
    step: dict[str, Any] = {"name": f"{name}.Put"}

    if apply:
        try:
            diff = await c.put_singleton(uri, name, overlay)
            step["status"] = "OK" if diff else "NOOP"
            step["diff"] = diff
        except AMTError as e:
            step["status"] = "FAIL"
            step["error"] = f"{e.kind}: {e.detail}"
    else:
        # Dry-run: Get current values, show what would change
        try:
            current_raw = await c._post(
                "http://schemas.xmlsoap.org/ws/2004/09/transfer/Get",
                uri, "",
            )
        except AMTError as e:
            step["status"] = "FAIL (get)"
            step["error"] = f"{e.kind}: {e.detail}"
            return step
        m = re.search(
            rf"<[a-z0-9]+:{name}[^>]*>(.*?)</[a-z0-9]+:{name}>",
            current_raw, re.DOTALL,
        )
        diff: dict[str, str] = {}
        if m:
            inner = m.group(1)
            for field, new_val in overlay.items():
                mm = re.search(rf"<[a-z0-9]+:{field}>([^<]*)</", inner)
                old_val = mm.group(1) if mm else "<absent>"
                if old_val != str(new_val):
                    diff[field] = f"{old_val!r} → {new_val!r}"
        step["status"] = (
            "DRY-RUN (would-change)" if diff else "DRY-RUN (noop)"
        )
        step["diff"] = diff
    return step


async def _apply_node(
    node: dict[str, str],
    canonical: dict[str, Any],
    user: str,
    password: str,
    apply: bool,
) -> dict[str, Any]:
    """Returns a report dict for one node."""
    name = node["name"]
    host = node["host"]
    report: dict[str, Any] = {"name": name, "host": host, "steps": []}

    try:
        async with AMTClient(host, user, password) as c:
            # ─── Per-class Puts ───
            for cls in canonical.get("classes", []):
                step = await _apply_one_class(c, cls, name, apply)
                report["steps"].append(step)

            # ─── Time sync (method invocation) ───
            if canonical.get("time_sync"):
                if apply:
                    try:
                        result = await c.sync_time()
                        report["steps"].append({
                            "name": "AMT_TimeSynchronizationService.SetHighAccuracyTimeSynch",
                            "status": "OK" if result["return_value"] == 0 else f"FAIL rv={result['return_value']}",
                            "diff": f"drift was {result['drift_seconds']}s",
                        })
                    except AMTError as e:
                        report["steps"].append({
                            "name": "AMT_TimeSynchronizationService.SetHighAccuracyTimeSynch",
                            "status": "FAIL",
                            "error": f"{e.kind}: {e.detail}",
                        })
                else:
                    report["steps"].append({
                        "name": "AMT_TimeSynchronizationService.SetHighAccuracyTimeSynch",
                        "status": "DRY-RUN (would invoke; forces AMT clock to manager UTC)",
                    })

    except AMTError as e:
        report["global_error"] = f"{e.kind}: {e.detail}"

    return report


def _print_report(report: dict[str, Any]) -> None:
    print(f"\n── {report['name']}  ({report['host']}) ──")
    if "global_error" in report:
        print(f"  GLOBAL ERROR: {report['global_error']}")
        return
    for step in report["steps"]:
        print(f"  {step['name']}: {step['status']}")
        if "error" in step:
            print(f"    error: {step['error']}")
        if "diff" in step:
            diff = step["diff"]
            if isinstance(diff, dict) and diff:
                for field, change in diff.items():
                    print(f"    {field}: {change}")
            elif isinstance(diff, str):
                print(f"    {diff}")


async def run(apply: bool, only_node: str | None) -> int:
    user, password = _load_creds()
    nodes = _load_nodes()
    canonical = _load_canonical()

    if only_node:
        nodes = [n for n in nodes if n["name"] == only_node]
        if not nodes:
            print(f"No such node: {only_node}", file=sys.stderr)
            return 2

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"AMT fleet apply — {mode} — {len(nodes)} node"
          f"{'s' if len(nodes) != 1 else ''}")
    print("=" * 72)

    any_fail = False
    for node in nodes:
        report = await _apply_node(node, canonical, user, password, apply)
        _print_report(report)
        if "global_error" in report:
            any_fail = True
        for step in report.get("steps", []):
            if step["status"].startswith("FAIL"):
                any_fail = True

    print()
    print("=" * 72)
    print(f"  {'FAIL' if any_fail else 'OK'}: {mode} complete")
    return 1 if any_fail else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="Show what would change; don't write.")
    g.add_argument("--apply", action="store_true",
                   help="Apply canonical state.")
    ap.add_argument("--node", help="Only act on this node (default: all 6).")
    args = ap.parse_args()

    rc = asyncio.run(run(apply=args.apply, only_node=args.node))
    sys.exit(rc)


if __name__ == "__main__":
    main()
