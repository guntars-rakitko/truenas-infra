#!/bin/bash
# NVMe drop diagnostics capture — Beelink ME Mini NVMe dropout investigation.
# Runs as root from a TrueNAS cron job every minute.
#
# Purpose: the root cause of the NVMe dropouts is UNKNOWN (power brownout vs
# nvme driver bug vs PCIe AER link failure vs controller firmware hang). Those
# have different fixes and are only distinguishable from the kernel log AT THE
# MOMENT OF THE DROP. Nobody is watching at 03:00, so capture it automatically.
#
# Emails are NOT sent from here — TrueNAS's own VolumeStatus alert handles that.
#
# ─────────────────────────────────────────────────────────────────────────────
# ⚠ WHY THIS WRITES TO /var/log AND NOT /mnt/tank  (learned the hard way)
#
# The 2026-09-14 incident SUSPENDED the pool (2 of 5 raidz1 members erroring)
# and this script captured NOTHING — because the previous version wrote to
# /mnt/tank/system/nvme-diag, i.e. the pool being diagnosed. On a SUSPENDED
# pool every I/O blocks UNINTERRUPTIBLY, so the unconditional `mkdir -p` at the
# top did not fail, it HUNG — and cron spawned another hung copy every minute.
#
# It had worked for the five earlier incidents only because those left the pool
# DEGRADED-but-writable. It failed on the one case that mattered most, and the
# kernel signature for the only double-drop on record was lost permanently.
#
# So: capture ALWAYS to local disk, and touch /mnt/tank ONLY when the pool is
# healthy again. /var/log is boot-pool/ROOT/<BE>/var/log — a separate pool from
# tank, ~222 G free.
#
# ⚠ NOT /tmp: it is tmpfs, and the documented recovery for this fault is a full
# POWER CYCLE — which would destroy the capture we just took.
# ─────────────────────────────────────────────────────────────────────────────
set -u

# Overridable ONLY so tests/test_nvme_capture.py can exercise the control flow
# against temp dirs. Production always uses the defaults.
LOCAL="${NVME_DIAG_LOCAL:-/var/log/nvme-diag}"      # staging — never on tank
ARCHIVE="${NVME_DIAG_ARCHIVE:-/mnt/tank/system/nvme-diag}"  # ONLY when healthy
MARK="$LOCAL/.incident-active"
KEEP_LOCAL=10                            # cap, so a stuck flush can't fill boot-pool

# Local only. Safe even with tank suspended.
mkdir -p "$LOCAL" 2>/dev/null

STATUS="$(timeout 30 zpool status -x tank 2>/dev/null)"

if [ "$STATUS" = "pool 'tank' is healthy" ]; then
    # ── Healthy: flush any captures taken while tank was unavailable. ────────
    # Every tank touch is timeout-guarded: a pool that is "healthy" per zpool
    # but still settling must not be able to hang cron.
    for dir in "$LOCAL"/*/; do
        [ -d "$dir" ] || continue
        name="$(basename "$dir")"
        case "$name" in .*) continue ;; esac
        if timeout 60 mkdir -p "$ARCHIVE/$name" 2>/dev/null &&
           timeout 300 cp -a "$dir." "$ARCHIVE/$name/" 2>/dev/null; then
            rm -rf "$dir" 2>/dev/null
            logger -t nvme-drop-capture "flushed $name -> $ARCHIVE/$name"
        else
            logger -t nvme-drop-capture "flush of $name FAILED or timed out; kept locally"
        fi
    done
    # Trim local leftovers (oldest first) if flushes keep failing.
    # shellcheck disable=SC2012
    ls -1dt "$LOCAL"/*/ 2>/dev/null | tail -n +$((KEEP_LOCAL + 1)) | while read -r old; do
        rm -rf "$old" 2>/dev/null
    done
    rm -f "$MARK" 2>/dev/null
    exit 0
fi

# ── Unhealthy. Capture once per incident, ENTIRELY LOCALLY. ──────────────────
# Nothing below may reference $ARCHIVE.
[ -f "$MARK" ] && exit 0

T="$(date -u +%Y%m%dT%H%M%SZ)"
O="$LOCAL/$T"
mkdir -p "$O" 2>/dev/null
touch "$MARK" 2>/dev/null   # set FIRST: if a later step wedges, we don't respawn

{
    echo "captured_utc: $(date -u)"
    echo "uptime: $(uptime -p)"
    echo "boot: $(uptime -s)"
    echo "zpool_status_x: $STATUS"
} > "$O/00-when.txt" 2>&1

# Pool state. SUSPENDED vs DEGRADED is the Mode C discriminator — a SUSPENDED
# pool cannot even enumerate its error list, so a large count here is NOT
# evidence of data loss.
timeout 30 zpool status -v tank      > "$O/01-zpool-status.txt" 2>&1
timeout 30 zpool events -v 2>/dev/null | tail -200 > "$O/02-zpool-events.txt" 2>&1

# THE key artifact: what the kernel actually said.
dmesg -T 2>/dev/null | tail -500 > "$O/03-dmesg-tail.txt" 2>&1
dmesg -T 2>/dev/null | grep -iE 'nvme|pcie|aer|CSTS|Disabling device|controller is down|link|reset|timeout' \
                     | tail -200 > "$O/04-dmesg-nvme-pcie.txt" 2>&1
journalctl -k --since "-45 min" --no-pager > "$O/05-journal-kernel-45min.txt" 2>&1

# PCIe link state + AER counters (distinguishes link failure from power loss).
lspci -vv                 > "$O/06-lspci-vv.txt" 2>&1
for p in /sys/bus/pci/devices/*/; do
    b="$(basename "$p")"
    if [ -r "${p}current_link_speed" ]; then
        echo "$b speed=$(cat "${p}current_link_speed" 2>&1) width=$(cat "${p}current_link_width" 2>&1) enable=$(cat "${p}enable" 2>&1)"
    fi
done > "$O/07-pcie-link-state.txt" 2>&1
grep -r . /sys/bus/pci/devices/*/aer_dev_* 2>/dev/null > "$O/08-pcie-aer-counters.txt" 2>&1

# Per-drive NVMe state: error log, SMART, and the PS2 power cap.
for d in /dev/nvme[0-9]; do
    [ -e "$d" ] || continue
    { echo "===== $d ====="; nvme error-log "$d" 2>&1 | head -60; } >> "$O/09-nvme-error-log.txt" 2>&1
    { echo "===== $d ====="; nvme smart-log "$d" 2>&1;              } >> "$O/10-nvme-smart-log.txt" 2>&1
    { echo -n "$d ps: ";     nvme get-feature "$d" -f 0x02 2>&1;    } >> "$O/11-nvme-power-state.txt" 2>&1
done

# ⚠ Mode C signature: the device stays ENUMERATED but reports SIZE 0 — it does
# not vanish. Record sizes explicitly so 0B is visible in the artifact.
lsblk -o NAME,SIZE,SERIAL,MODEL > "$O/12-lsblk.txt" 2>&1
{
    for b in /sys/block/nvme*; do
        [ -e "$b" ] || continue
        echo "$(basename "$b") sectors=$(cat "$b/size" 2>&1)"
    done
} >> "$O/12-lsblk.txt" 2>&1
for n in /sys/class/nvme/nvme*; do
    [ -e "$n" ] || continue
    echo "$(basename "$n") state=$(cat "$n/state" 2>&1) serial=$(cat "$n/serial" 2>&1 | xargs) model=$(cat "$n/model" 2>&1 | xargs)"
done > "$O/13-nvme-controllers.txt" 2>&1

# Thermals + load at drop time (tests the thermal-trip hypothesis).
grep . /sys/class/hwmon/hwmon*/name /sys/class/hwmon/hwmon*/temp*_input > "$O/14-temps.txt" 2>&1
{ cat /proc/loadavg; echo; cat /proc/pressure/io 2>/dev/null; } > "$O/15-load.txt" 2>&1

# Live ZFS tunables + UPS state (rules power events in/out).
grep . /sys/module/zfs/parameters/zfs_vdev_*active \
       /sys/module/zfs/parameters/zfs_txg_timeout \
       /sys/module/nvme_core/parameters/default_ps_max_latency_us \
       > "$O/16-tunables.txt" 2>&1
upsc apc1@localhost > "$O/17-ups.txt" 2>&1

logger -t nvme-drop-capture "tank NOT healthy - diagnostics captured locally to $O (flushes to $ARCHIVE when pool recovers)"
exit 0
