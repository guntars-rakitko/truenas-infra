#!/bin/sh
# nas-ups-shutdown.sh — arm the UPS power-off during NAS shutdown (bug #57).
#
# Registered as a TrueNAS **Init/Shutdown Script** (type=SCRIPT, when=SHUTDOWN)
# by scripts/setup-ups-shutdown-hook.sh — NOT as upsmon's SHUTDOWNCMD. The
# first attempt (upsmon SHUTDOWNCMD) failed live on 2026-05-30: the UPS was
# never armed and drained flat. Community evidence (NUT issue #2587, the
# systemd `nutshutdown` hook) shows the kill-power must run as a shutdown-time
# hook, and that `upsdrvctl shutdown` only works once the driver has released
# the USB device.
#
# This runs DURING TrueNAS poweroff while the box can still reach the UPS.
# It ONLY arms the UPS (TrueNAS already handles the host poweroff) so the UPS
# cuts power after `ups.delay.shutdown` (450s) and re-powers when mains returns.
#
# Why bug #57 exists: the UPS is on a DB-9 -> RS-232 -> USB adapter
# (/dev/ttyUSB0). TrueNAS's built-in `powerdown` kill-power runs only after the
# USB device is torn down, so `shutdown.return` never reaches the UPS and it
# drains the dead load flat. We arm it earlier, here.
#
# Robust to either ordering (NUT still up, or already stopped):
#   1. `upscmd shutdown.return` — works while upsd+driver are up; the running
#      driver relays it over the already-open USB link (no device-claim race).
#   2. `upsdrvctl stop` then `upsdrvctl shutdown` — fallback if upsd is gone;
#      stopping the driver frees /dev/ttyUSB0 so upsdrvctl can claim it,
#      avoiding the "Can't claim USB device" error (NUT issue #2587).
#
# Diagnosable WITHOUT journal access: appends a timestamped trace to LOG
# (world-readable) so we can confirm what fired after a drill.
set -u
UPS="apc1@localhost"
DIR="/mnt/tank/system/nut"
PWFILE="$DIR/.upsadmin-pw"
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
log "POWERDOWNFLAG set — power-fail shutdown; arming UPS power-off (bug #57)"
armed=0

# Path 1 (preferred): instcmd via the running upsd/driver — no device-claim race.
if [ -r "$PWFILE" ]; then
    PW=$(cat "$PWFILE")
    if /usr/bin/upscmd -u upsadmin -p "$PW" "$UPS" shutdown.return >> "$LOG" 2>&1; then
        log "OK: upscmd shutdown.return accepted (UPS cuts after ups.delay.shutdown)"
        armed=1
    else
        log "MISS: upscmd shutdown.return failed (upsd down / auth?) — trying upsdrvctl"
    fi
    unset PW
else
    log "MISS: $PWFILE unreadable — trying upsdrvctl"
fi

# Path 2 (fallback): release the driver, then kill-power via upsdrvctl.
if [ "$armed" != 1 ]; then
    /usr/sbin/upsdrvctl stop apc1 >> "$LOG" 2>&1 || true
    sleep 1
    if /usr/sbin/upsdrvctl shutdown apc1 >> "$LOG" 2>&1; then
        log "OK: upsdrvctl shutdown issued (fallback path)"
        armed=1
    else
        log "FAIL: upsdrvctl shutdown failed — UPS NOT armed, it will drain flat"
    fi
fi

log "=== shutdown hook end (armed=$armed) ==="
chmod 644 "$LOG" 2>/dev/null || true
# Never block the shutdown, regardless of outcome.
exit 0
