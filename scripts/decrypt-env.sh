#!/usr/bin/env bash
# decrypt-env.sh — render .env.sops to .env on disk (chmod 600).
#
# Use sparingly — manage.sh decrypts in-memory and `eval`s, which is safer
# because the secret never touches disk. This script is a convenience for
# one-off debugging where you want .env sitting in the repo root.
#
# Usage: scripts/decrypt-env.sh
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$SCRIPT_DIR/.env.sops"
DST="$SCRIPT_DIR/.env"

if [[ ! -f "$SRC" ]]; then
    echo "ERROR: $SRC not found." >&2
    exit 1
fi

if [[ -f "$DST" ]]; then
    echo "WARNING: $DST already exists. Overwrite? [y/N] " >&2
    read -r ans
    [[ "$ans" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

sops decrypt "$SRC" > "$DST"
chmod 600 "$DST"
echo "Wrote $DST (chmod 600)."
