#!/bin/sh
# nas-ups-shutdown.sh — arm the UPS power-off during NAS shutdown (bug #57).
#
# Registered as a TrueNAS **Init/Shutdown Script** (type=SCRIPT, when=SHUTDOWN)
# by scripts/setup-ups-shutdown-hook.sh — NOT as upsmon's SHUTDOWNCMD. It runs
# DURING TrueNAS poweroff while the box can still reach the UPS, and ONLY arms
# the UPS kill-power (TrueNAS / the Path-B orchestrator already handle the host
# poweroff). The UPS then cuts power after `ups.delay.shutdown` (90s) and
# re-energizes its outputs when mains is present (`ups.delay.start`, 60s).
#
# KILL-POWER MECHANISM (revised 2026-06-02, kube-infra #611):
#   The lever is the apcsmart DRIVER kill-power: `upsdrvctl shutdown`, which
#   issues the method selected by `sdtype` in ups.conf. We set **sdtype=5**
#   (hard hibernate `@`) so the UPS cuts + auto-returns REGARDLESS of line
#   state — including ON MAINS (validated live 2026-06-02). The driver must
#   release the USB serial device first, so we `upsdrvctl stop` then
#   `upsdrvctl shutdown` (the transient instance re-claims /dev/ttyUSB0 and
#   sends the `@`). NUT issue #2587.
#
#   ⚠ We deliberately do NOT use `upscmd shutdown.return` here. That INSTCMD
#   is a documented no-op when the UPS is on mains (and was observed to no-op
#   on battery too on this unit) — yet it returns rc=0 ("accepted"), which
#   previously made this hook log a FALSE `armed=1` and skip the real
#   kill-power. The driver/sdtype `@` path is the only validated lever.
#
# Diagnosable WITHOUT journal access: appends a timestamped trace to LOG
# (world-readable) so we can confirm what fired after a drill. NOTE: an
# `issued` result means the command was SENT, not that the UPS will cut — the
# driver is released by then so the cut is not verifiable from here; confirm
# the cut/power-cycle via a drill (see wiki ups-operations.md).
set -u
DIR="/mnt/tank/system/nut"
LOG="$DIR/last-shutdown.log"

log() {
    echo "[$(date '+%F %T')] $*" >> "$LOG" 2>/dev/null
    logger -t nas-ups-shutdown "$*" 2>/dev/null || true
}

log "=== shutdown hook start ==="

# GUARD: only arm the UPS on a genuine power-fail shutdown. upsmon sets the
# POWERDOWNFLAG file ONLY during an FSD/low-battery shutdown; a routine reboot
# or admin poweroff leaves it unset. `upsmon -K` exits 0 iff the flag is set.
# Without this guard a normal reboot would arm the UPS and cut the WHOLE rack
# ~ups.delay.shutdown later — mid-operation after the NAS already came back.
if ! /usr/sbin/upsmon -K >/dev/null 2>&1; then
    log "POWERDOWNFLAG not set — routine reboot/shutdown, NOT arming UPS. exit."
    chmod 644 "$LOG" 2>/dev/null || true
    exit 0
fi
log "POWERDOWNFLAG set — power-fail shutdown; arming UPS kill-power (sdtype=5 @)"
issued=0

# Release the driver so the transient upsdrvctl instance can claim the USB
# serial device, then issue the sdtype-driven kill-power (hard hibernate @).
/usr/sbin/upsdrvctl stop apc1 >> "$LOG" 2>&1 || true
sleep 1
if /usr/sbin/upsdrvctl shutdown apc1 >> "$LOG" 2>&1; then
    issued=1
    log "OK: upsdrvctl shutdown ISSUED (sdtype=5 @). UPS should cut after ups.delay.shutdown"
    log "    then re-energize when mains present. NOTE: cut not verifiable from here (driver released)."
else
    log "FAIL: upsdrvctl shutdown did NOT issue — UPS NOT armed, it will drain flat. Investigate."
fi

log "=== shutdown hook end (kill-power issued=$issued) ==="
chmod 644 "$LOG" 2>/dev/null || true
# Never block the shutdown, regardless of outcome.
exit 0
