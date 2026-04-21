#!/usr/bin/env bash
# manage.sh — truenas-infra phase dispatcher.
#
# Thin bash wrapper: decrypts .env, ensures the Python venv, and hands control
# to the Python CLI (`truenas-infra` / `python -m truenas_infra.cli`).
#
# Usage:
#     ./manage.sh                         # interactive menu
#     ./manage.sh phase preflight         # run a single phase (dry-run by default)
#     ./manage.sh phase network --apply   # actually change state
#     ./manage.sh list                    # list all phases
#
set -euo pipefail

# ─── Resolve script directory ────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── Colors ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# ─── Dependency check ────────────────────────────────────────────────────────
for cmd in python3 sops; do
    if ! command -v "$cmd" &>/dev/null; then
        echo -e "${RED}ERROR: Required command '$cmd' not found.${NC}"
        exit 1
    fi
done

# uv is preferred (faster, bundles Python versions); fall back to python -m venv + pip.
# uv's official install location is ~/.local/bin (per `curl | sh` installer),
# which isn't on PATH in every shell — look there explicitly.
if ! command -v uv &>/dev/null; then
    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [[ -x "$candidate" ]]; then
            PATH="$(dirname "$candidate"):$PATH"
            break
        fi
    done
fi

if command -v uv &>/dev/null; then
    PKG_MGR="uv"
else
    PKG_MGR="pip"
fi

# ─── .env decrypt ────────────────────────────────────────────────────────────
# Parse dotenv lines manually so shell-special chars ($, `, etc.) in values
# are treated as literal text — TrueNAS API keys frequently contain $.
load_dotenv_stream() {
    local line key value
    while IFS= read -r line || [[ -n "$line" ]]; do
        # Strip trailing \r (CRLF-safe)
        line="${line%$'\r'}"
        # Skip empty lines and comments
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
        # Split on first '=' only
        key="${line%%=*}"
        value="${line#*=}"
        # Strip leading/trailing whitespace from the key
        key="${key#"${key%%[![:space:]]*}"}"
        key="${key%"${key##*[![:space:]]}"}"
        # Strip surrounding single or double quotes from the value
        if [[ "$value" =~ ^\"(.*)\"$ ]]; then
            value="${BASH_REMATCH[1]}"
        elif [[ "$value" =~ ^\'(.*)\'$ ]]; then
            value="${BASH_REMATCH[1]}"
        fi
        [[ -z "$key" ]] && continue
        export "$key=$value"
    done
}

if [[ -f "$SCRIPT_DIR/.env" ]]; then
    load_dotenv_stream < "$SCRIPT_DIR/.env"
elif [[ -f "$SCRIPT_DIR/.env.sops" ]]; then
    echo -e "${CYAN}No .env found, decrypting .env.sops...${NC}"
    load_dotenv_stream < <(sops decrypt "$SCRIPT_DIR/.env.sops")
else
    echo -e "${RED}ERROR: No .env or .env.sops found in $SCRIPT_DIR${NC}"
    echo -e "${YELLOW}See bootstrap/01-bootstrap-notes.md — you need to create an API key first.${NC}"
    exit 1
fi

# ─── Required env vars ───────────────────────────────────────────────────────
for var in TRUENAS_HOST TRUENAS_API_KEY; do
    if [[ -z "${!var:-}" ]]; then
        echo -e "${RED}ERROR: Required env var '$var' is not set in .env.${NC}"
        exit 1
    fi
done

# ─── Ensure venv / dependencies ──────────────────────────────────────────────
VENV_DIR="$SCRIPT_DIR/.venv"

ensure_venv() {
    # Consider the venv healthy only if both bin/python and the package are present.
    if [[ -x "$VENV_DIR/bin/python" ]] && \
       "$VENV_DIR/bin/python" -c "import truenas_infra" &>/dev/null; then
        return 0
    fi

    # Wipe any partial/broken venv before rebuilding.
    if [[ -d "$VENV_DIR" ]]; then
        echo -e "${YELLOW}Removing partial venv at $VENV_DIR...${NC}"
        rm -rf "$VENV_DIR"
    fi

    echo -e "${CYAN}Creating Python venv and installing dependencies...${NC}"
    if [[ "$PKG_MGR" == "uv" ]]; then
        # uv will download Python 3.11 if the system doesn't have it.
        # UV_LINK_MODE=copy — use real copies, not hardlinks. Hardlinks from
        # uv's cache get wiped by cache cleanup and leave the venv with
        # only .pyc files (no .py), which breaks imports. Copy mode is a
        # touch slower but stable.
        UV_LINK_MODE=copy uv venv --python 3.11 "$VENV_DIR"
        UV_LINK_MODE=copy uv pip install --python "$VENV_DIR/bin/python" -e ".[dev]"
    else
        python3 -m venv "$VENV_DIR"
        "$VENV_DIR/bin/pip" install --quiet --upgrade pip
        "$VENV_DIR/bin/pip" install --quiet -e ".[dev]"
    fi
}

ensure_venv

# ─── Hand off to Python CLI ──────────────────────────────────────────────────
# All phase logic, menus, dry-run, and rollback safety live in truenas_infra.cli.
exec "$VENV_DIR/bin/python" -m truenas_infra.cli "$@"
