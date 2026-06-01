#!/usr/bin/env bash
# nas-ups-orchestrator.sh — NAS-side UPS shutdown orchestrator (Path B, kube-infra #611).
#
# Registered as the NAS NUT **SHUTDOWNCMD** (ups.config.shutdowncmd) by
# scripts/setup-talos-shutdown-orchestrator.sh. upsmon runs this once it has
# already committed to shutdown at Low-Battery (LB) — so this is COMMITTED:
# no mains re-check, no debounce (glitch-filtering lives upstream at the LB
# thresholds: battery.charge.low / battery.runtime.low). Even if mains return
# mid-sequence, the shutdown proceeds (re-dipping a depleted battery is riskier
# than finishing).
#
# Sequence (operator's desired flow):
#   1) talosctl shutdown --force the 6 Talos nodes (both clusters, in parallel).
#      --force skips ONLY Talos's API-dependent pod *drain* (CordonAndDrainNode),
#      which burns a hardcoded 5-min DrainTimeout once etcd quorum is lost on a
#      whole-rack outage. StopAllPods (graceful CRI SIGTERM + 30s ceiling) + fs
#      sync still run, so data flushing is unchanged. ~3.2 min, 0 faulted volumes.
#   2) poll each node's apid (:50000) until all are down (or a timeout backstop
#      so the NAS never hangs forever).
#   3) halt the NAS LAST → /sbin/shutdown -P now. That fires the #57 Init/Shutdown
#      hook (nas-ups-shutdown.sh) which arms the UPS (shutdown.return). Because the
#      NAS is genuinely last, ups.delay.shutdown only needs to cover the NAS's own
#      poweroff (trimmed to 90s).
#
# WHY this exists: the Talos nut-client extension's SHUTDOWNCMD can only ever run
# the slow bare /sbin/poweroff (no shell, no arg tokenization — posix_spawn — so
# "/sbin/poweroff --force" fails -1; tracked upstream siderolabs/extensions#1084).
# Driving shutdown from here with a least-privilege os:operator talosconfig is the
# only fast, working path.
#
# Credential blast radius: os:operator is the floor for Shutdown — it can
# shutdown/reboot/etcd-snapshot/read-logs, but CANNOT reset/wipe/upgrade, read
# file contents/secrets/PKI/config, apply config, or mint creds (all Admin-only).
# Configs are 0600 root-owned on the NAS + apid is firewalled to the NAS IP.
#
# Diagnosable WITHOUT journal access: appends a timestamped trace to LOG
# (world-readable) so the drill can confirm what fired + the ordering.
set -uo pipefail

TCTL=/mnt/tank/system/talos/talosctl
DEV_CFG=/mnt/tank/system/talos/dev-shutdown.talosconfig
PRD_CFG=/mnt/tank/system/talos/prd-shutdown.talosconfig
LOG=/mnt/tank/system/nut/last-node-shutdown.log

DEV_NODES="10.10.5.14,10.10.5.15,10.10.5.16"
PRD_NODES="10.10.5.11,10.10.5.12,10.10.5.13"
ALL_NODES="10.10.5.14 10.10.5.15 10.10.5.16 10.10.5.11 10.10.5.12 10.10.5.13"

TIMEOUT=300   # 5 min poll backstop so the NAS never hangs forever. Real drill
              # 2026-06-01: talosctl --force returned in 187s with nodes already
              # down (poll then needed only 8s), so this is pure cushion.
POLL=10       # seconds between apid liveness sweeps

log(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }

# apid (:50000) liveness probe. Prefer nc; fall back to bash /dev/tcp so the
# orchestrator works even if nc is absent on the host.
probe(){
  if command -v nc >/dev/null 2>&1; then
    nc -z -w2 "$1" 50000 >/dev/null 2>&1
  else
    timeout 2 bash -c "exec 3<>/dev/tcp/$1/50000" >/dev/null 2>&1
  fi
}

log "=== SHUTDOWNCMD orchestrator START (committed — no mains re-check) ==="

if [ ! -x "$TCTL" ]; then
  log "FATAL: talosctl not found/executable at $TCTL — halting NAS anyway so it doesn't hang"
  /sbin/shutdown -P now
  exit 0
fi

# 1) fire --force shutdown at both clusters in parallel
"$TCTL" --talosconfig "$DEV_CFG" shutdown --force --nodes "$DEV_NODES" --endpoints "$DEV_NODES" >>"$LOG" 2>&1 &
dev_pid=$!
"$TCTL" --talosconfig "$PRD_CFG" shutdown --force --nodes "$PRD_NODES" --endpoints "$PRD_NODES" >>"$LOG" 2>&1 &
prd_pid=$!
wait "$dev_pid"; log "dev talosctl shutdown --force rc=$?"
wait "$prd_pid"; log "prd talosctl shutdown --force rc=$?"
log "shutdown --force delivered to all 6 nodes; polling apid until down."

# 2) poll until all nodes stop answering apid, or the timeout backstop
start=$(date +%s)
while :; do
  up=0
  for ip in $ALL_NODES; do probe "$ip" && up=$((up+1)); done
  el=$(( $(date +%s) - start ))
  log "  [${el}s] nodes still answering apid: ${up}/6"
  [ "$up" -eq 0 ] && { log "all nodes down at ${el}s"; break; }
  [ "$el" -ge "$TIMEOUT" ] && { log "TIMEOUT ${TIMEOUT}s reached (${up}/6 still up) — halting NAS anyway"; break; }
  sleep "$POLL"
done

# 3) halt the NAS LAST — the #57 hook arms the UPS on this poweroff
log "=== halting NAS LAST → /sbin/shutdown -P now (arms UPS via #57 hook) ==="
/sbin/shutdown -P now
