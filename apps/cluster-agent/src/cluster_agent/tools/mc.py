"""mc tool — minio-client CLI wrapper for MinIO (NAS) + B2 (off-site).

Aliases are configured at container startup (Task 23 deploy step):
  - nas-prd / nas-dev → MinIO instances on the NAS (s3-{prd,dev}.w1.lv)
  - b2-eu             → Backblaze B2 EU off-site bucket

Read tools (mc_ls, mc_stat) used by Mode G (backup verification) in P7.
Write tool (mc_cp) used by the scheduler in P0+ to back up state.db
nightly to nas-prd/cluster-agent.

Output parsed as JSON lines: `mc --json` emits one JSON object per line
(not a single JSON array), so we split on newlines and parse each.
"""
from __future__ import annotations
import json
import subprocess
from typing import Any

from .audit import audit


class McError(RuntimeError):
    """Raised when an mc CLI call returns non-zero."""


@audit(tool="mc_ls")
def mc_ls(path: str, *, recursive: bool = False) -> list[dict[str, Any]]:
    """List objects at path (e.g. 'nas-prd/mssql-backups/').

    `--json` emits one JSON object per line; we parse each line.
    Trailing blank lines are skipped.
    """
    cmd = ["mc", "ls", "--json"]
    if recursive:
        cmd.append("--recursive")
    cmd.append(path)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise McError(f"mc ls failed: {r.stderr.strip()}")
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


@audit(tool="mc_stat")
def mc_stat(path: str) -> dict[str, Any]:
    """Stat a single object — returns size, modification time, etag, etc."""
    r = subprocess.run(
        ["mc", "stat", "--json", path],
        capture_output=True, text=True, timeout=15,
    )
    if r.returncode != 0:
        raise McError(f"mc stat failed: {r.stderr.strip()}")
    return json.loads(r.stdout)


@audit(tool="mc_cp")
def mc_cp(src: str, dst: str) -> None:
    """Copy one object. Used by the scheduler to back up state.db."""
    r = subprocess.run(
        ["mc", "cp", "--json", src, dst],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise McError(f"mc cp failed: {r.stderr.strip()}")
