#!/bin/sh
# nas-ups-shutdown.sh — NAS pre-halt UPS power-off hook.
#
# Wired as TrueNAS `ups.config.shutdowncmd` → NUT upsmon SHUTDOWNCMD. upsmon
# runs this the instant the master decides to power down (on LB/FSD when
# `shutdown=LOWBATT`), while the system AND the USB-serial link are STILL UP.
#
# WHY THIS EXISTS (bug #57): the UPS reaches the NAS over a DB-9 → RS-232 →
# USB adapter (/dev/ttyUSB0). TrueNAS's default `powerdown` killpower runs at
# the very END of halt, AFTER the kernel removes the USB device — so
# `shutdown.return` never reaches the UPS and it keeps draining the dead load
# on a real outage (then a second uncontrolled outage when utility returns).
# We send the power-off command HERE instead, up-front: once the UPS receives
# `shutdown.return`, its own `ups.delay.shutdown` (540 s / 9 min) countdown is
# AUTONOMOUS and fires even after USB is gone.
#
# shutdown.return semantics: "cut the load after ups.delay.shutdown, then
# re-power when mains is back (after ups.delay.start)". On battery (DR) this is
# a clean delayed cut + automatic recovery on utility return — the intended flow.
#
# Deployed to /mnt/tank/system/nut/ by scripts/setup-ups-shutdown-hook.sh
# (a ZFS dataset path, so it survives reboots + TrueNAS updates).
#
# VALIDATE VIA DRILL A before trusting it — see
# wiki/docs/runbooks/ups-operations.md § Drill A.
set -u

UPS="apc1@localhost"
PWFILE="/mnt/tank/system/nut/.upsadmin-pw"
L="logger -t nas-ups-shutdown"

$L "pre-halt: arming UPS power-off (shutdown.return) before USB teardown"
armed=0

# Primary path: explicit instcmd via upsd. Uses ups.delay.shutdown exactly and
# is the command Drill A documents. Needs the upsadmin password from a
# root-only file (Doppler is unreachable during shutdown — no network).
if [ -r "$PWFILE" ]; then
    PW=$(cat "$PWFILE")
    if /usr/bin/upscmd -u upsadmin -p "$PW" "$UPS" shutdown.return >/dev/null 2>&1; then
        $L "upscmd shutdown.return OK — UPS cuts power after ups.delay.shutdown"
        armed=1
    else
        $L "upscmd shutdown.return FAILED — trying upsdrvctl fallback"
    fi
    unset PW
else
    $L "$PWFILE unreadable — trying upsdrvctl fallback (no auth needed)"
fi

# Fallback path: local driver control, no password. Behavior depends on the
# apcsmart shutdown command (sdtype); use only if the primary path is unusable.
if [ "$armed" != 1 ]; then
    if /usr/sbin/upsdrvctl shutdown apc1 >/dev/null 2>&1; then
        $L "upsdrvctl shutdown OK (fallback)"
        armed=1
    fi
fi

[ "$armed" = 1 ] || $L "WARNING: UPS power-off NOT armed — UPS may keep draining after halt"

# Let the command land over the still-alive USB link before we tear it down.
sleep 3

# Graceful systemd poweroff (stops all services in order, exports ZFS, halts).
$L "halting NAS now"
exec /usr/sbin/poweroff
