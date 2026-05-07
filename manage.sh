#!/usr/bin/env bash
# manage.sh — truenas-infra phase dispatcher.
#
# Thin bash wrapper: fetches credentials from Doppler infrastructure/ops,
# ensures the Python venv, and hands control to the Python CLI
# (`truenas-infra` / `python -m truenas_infra.cli`). No .env file on disk.
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
for cmd in python3 doppler; do
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

# ─── Secrets — Doppler infrastructure/ops ────────────────────────────────────
# Per-key fetch (avoids bulk env-format pitfalls with multi-line values like
# KUBECONFIG_DEV that we don't need here). 5-6 single-line keys total —
# under 1 second of Doppler API time.
_DOPPLER_KEYS=(
    TRUENAS_HOST
    TRUENAS_API_KEY
    TRUENAS_VERIFY_SSL
    TRUENAS_NUT_MONPWD
    SHARED_CLOUDFLARE_API_TOKEN
)

for _k in "${_DOPPLER_KEYS[@]}"; do
    if ! _v=$(doppler secrets get "$_k" --project infrastructure --config ops \
                --plain --silent 2>&1); then
        # Some optional keys (TRUENAS_ROOT_PASSWORD) may not be set yet; only
        # the env-var-required block below complains about the truly required ones.
        if [[ "$_k" == TRUENAS_HOST || "$_k" == TRUENAS_API_KEY ]]; then
            echo -e "${RED}ERROR: Failed to fetch '$_k' from Doppler:${NC}" >&2
            echo "$_v" >&2
            echo -e "${YELLOW}Check 'doppler whoami' and rerun 'doppler login' if needed.${NC}" >&2
            exit 1
        fi
        continue
    fi
    export "$_k=$_v"
done
unset _DOPPLER_KEYS _k _v

# Alias Doppler-prefixed key to the bare name Python code expects.
# CLOUDFLARE_API_TOKEN is the conventional env var name for CloudFlare's
# SDKs/CLIs; the Doppler key is SHARED_ prefixed for cross-purpose tracking.
[[ -n "${SHARED_CLOUDFLARE_API_TOKEN:-}" ]] && \
    export CLOUDFLARE_API_TOKEN="$SHARED_CLOUDFLARE_API_TOKEN"

# ─── Required env vars ───────────────────────────────────────────────────────
for var in TRUENAS_HOST TRUENAS_API_KEY; do
    if [[ -z "${!var:-}" ]]; then
        echo -e "${RED}ERROR: Required env var '$var' is not set.${NC}"
        echo -e "${YELLOW}Check Doppler infrastructure/ops config; run 'doppler whoami'.${NC}"
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
