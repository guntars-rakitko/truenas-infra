#!/usr/bin/env python3
"""render-env.py — decrypt `apps/<name>/secrets.sops.yaml` to `apps/<name>/.env`.

The apps phase calls this per-app before registering the compose file.

Usage:
    scripts/render-env.py apps/minio-prd
    scripts/render-env.py apps/minio-prd --all   # render every apps/* with secrets

Writes `.env` files with chmod 600. Files are gitignored.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import yaml


def render_one(app_dir: Path) -> bool:
    """Render one app's secrets. Returns True if a .env was written."""
    src = app_dir / "secrets.sops.yaml"
    dst = app_dir / ".env"

    if not src.exists():
        print(f"  [skip] {app_dir}: no secrets.sops.yaml", file=sys.stderr)
        return False

    result = subprocess.run(
        ["sops", "decrypt", str(src)],
        check=True,
        capture_output=True,
        text=True,
    )
    decrypted = yaml.safe_load(result.stdout) or {}
    if not isinstance(decrypted, dict):
        raise SystemExit(f"{src}: expected a mapping at top level, got {type(decrypted).__name__}")

    lines = [f"{k}={v}" for k, v in decrypted.items()]
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dst.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    print(f"  [ok]  {dst}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_dir", nargs="?", type=Path, help="Path to apps/<name>/")
    parser.add_argument("--all", action="store_true", help="Render every apps/*")
    args = parser.parse_args()

    if not shutil.which("sops"):
        raise SystemExit("sops not in PATH — install sops first.")

    if args.all:
        base = Path(__file__).resolve().parent.parent / "apps"
        app_dirs = sorted(p for p in base.iterdir() if p.is_dir())
    else:
        if args.app_dir is None:
            parser.error("app_dir is required unless --all is passed.")
        app_dirs = [args.app_dir]

    for d in app_dirs:
        render_one(d)


if __name__ == "__main__":
    main()
