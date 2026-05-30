#!/usr/bin/env bash
# setup-ups-shutdown-hook.sh — install the NAS pre-halt UPS power-off hook.
# Idempotent: re-running re-uploads the script + refreshes the password file
# + re-asserts the shutdowncmd field. Run from the operator's laptop.
#
# Why this exists (bug #57): the UPS reaches the NAS over a DB-9 → RS-232 →
# USB adapter (/dev/ttyUSB0). TrueNAS's default `powerdown` killpower runs too
# late — after the kernel tears down the USB device — so `shutdown.return`
# never reaches the UPS and it keeps draining on a real outage. This installs
# scripts/nas-ups-shutdown.sh as upsmon's SHUTDOWNCMD so the power-off is
# armed up-front, while USB comms are still alive.
#
# Prereqs:
#   - SSH publickey access to truenas_admin@$TRUENAS_HOST (default nas.w1.lv)
#   - NOPASSWD sudo on the NAS for tee/chmod/mkdir/midclt (see truenas-infra
#     CLAUDE.md § SSH + sudo)
#   - TRUENAS_NUT_ADMINPWD exported from Doppler infrastructure/ops
#     (manage.sh does this; or: export TRUENAS_NUT_ADMINPWD=$(doppler secrets
#      get TRUENAS_NUT_ADMINPWD --project infrastructure --config ops --plain))
#
# ⚠️  This wires the live shutdown path. VALIDATE with Drill A immediately
#     after (whole-rack maintenance window) — see
#     wiki/docs/runbooks/ups-operations.md § Drill A. Until validated, leave
#     `powerdown: true` as the (failing-but-harmless) backup.
set -euo pipefail

HOST="${TRUENAS_HOST:-nas.w1.lv}"
SSH_TARGET="${HOST/#/truenas_admin@}"; [[ "$HOST" == *@* ]] && SSH_TARGET="$HOST"
DEST_DIR="/mnt/tank/system/nut"
DEST_SCRIPT="$DEST_DIR/nas-ups-shutdown.sh"
PWFILE="$DEST_DIR/.upsadmin-pw"
SRC_SCRIPT="$(cd "$(dirname "$0")" && pwd)/nas-ups-shutdown.sh"

if [[ ! -f "$SRC_SCRIPT" ]]; then echo "ERROR: $SRC_SCRIPT not found" >&2; exit 1; fi
if [[ -z "${TRUENAS_NUT_ADMINPWD:-}" ]]; then
    echo "ERROR: TRUENAS_NUT_ADMINPWD not set (export from Doppler infrastructure/ops)" >&2; exit 1
fi

echo "==> [1/4] Ensuring $DEST_DIR exists on $SSH_TARGET"
ssh "$SSH_TARGET" "sudo mkdir -p '$DEST_DIR'"

echo "==> [2/4] Uploading nas-ups-shutdown.sh → $DEST_SCRIPT (mode 700)"
ssh "$SSH_TARGET" "sudo tee '$DEST_SCRIPT' >/dev/null && sudo chmod 700 '$DEST_SCRIPT'" < "$SRC_SCRIPT"

echo "==> [3/4] Writing upsadmin password file → $PWFILE (mode 600, root-only)"
# Password flows over stdin (never on a command line); written root-only so the
# shutdown hook can read it when Doppler is unreachable during halt.
printf '%s' "$TRUENAS_NUT_ADMINPWD" | \
    ssh "$SSH_TARGET" "sudo tee '$PWFILE' >/dev/null && sudo chmod 600 '$PWFILE' && sudo chown root:root '$PWFILE'"

echo "==> [4/4] Pointing TrueNAS upsmon SHUTDOWNCMD at the hook"
ssh "$SSH_TARGET" "sudo midclt call ups.update '{\"shutdowncmd\": \"$DEST_SCRIPT\"}' >/dev/null"
ssh "$SSH_TARGET" "sudo midclt call service.control RESTART ups >/dev/null"

echo
echo "✓ Installed. Live shutdowncmd:"
ssh "$SSH_TARGET" "midclt call ups.config | python3 -c 'import sys,json;print(\"  shutdowncmd =\", json.load(sys.stdin)[\"shutdowncmd\"])'"
echo
echo "NEXT: validate end-to-end with Drill A (whole-rack maintenance window)."
echo "      Until then, the default powerdown killpower stays as backup."
