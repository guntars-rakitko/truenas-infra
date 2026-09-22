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
#   1) talosctl shutdown --force EVERY node in both clusters, all in parallel
#      (one invocation per node — see the note at the loop for why).
#      --force skips ONLY Talos's API-dependent pod *drain* (CordonAndDrainNode),
#      which burns a hardcoded 5-min DrainTimeout once etcd quorum is lost on a
#      whole-rack outage. StopAllPods (graceful CRI SIGTERM + 30s ceiling) + fs
#      sync still run, so data flushing is unchanged. ~3.2 min, 0 faulted volumes.
#
#      ⚠ --force IS LOAD-BEARING FOR A SECOND, INDEPENDENT REASON (found
#      2026-07-29 during the Talos v1.13.5->v1.13.7 roll). The etcd-quorum
#      rationale above is INCOMPLETE: that drain uses the Kubernetes *eviction*
#      API, and on BOTH clusters every node hosts PodDisruptionBudgets sitting at
#      ALLOWED DISRUPTIONS = 0 — the single-replica Prometheus / Alertmanager /
#      Grafana / Loki / Pocket-ID (1 replica vs minAvailable:1 is structurally
#      unsatisfiable), the CNPG `giks-primary` PDB, and all three Longhorn
#      `instance-manager-*` PDBs. So the drain would hang until DrainTimeout even
#      on a perfectly HEALTHY cluster with quorum intact — quorum loss is not
#      required to trigger it. Dropping --force would re-add ~5 min per node to a
#      battery-constrained shutdown. Do NOT remove it.
#
#      ⚠ ON MS-A2 THE CONCLUSION HOLDS BUT THE REASONS NARROW — do not re-derive
#      it from the list above and conclude --force is unnecessary. Longhorn is
#      dropped, so the three `instance-manager-*` PDBs vanish; and the msa2
#      per-cluster overlays `$patch: delete` the three repo-owned PDBs (loki,
#      pocket-id, coredns) precisely because minAvailable:1 on a 1-replica
#      workload blocks EVERY eviction at n=1. What REMAINS is chart-level PDBs
#      (cert-manager, metrics-server, traefik x3, kube-prometheus-stack x3) —
#      open item 6 in `clusters/msa2-{prd,dev}/infrastructure.yaml`, still in
#      place. Those alone re-create the hang. `talos-os/bootstrap.sh` keeps
#      `--disable-eviction` on `kubectl drain` for the same reason.
#
#      Talos v1.13.7 verified: `shutdown --force` still exists with unchanged
#      semantics ("force a node to shutdown without a cordon/drain"). Note this
#      is a DIFFERENT command from `talosctl upgrade` (whose --preserve flag was
#      DEPRECATED, not removed, in v1.13) — neither affects this path.
#
#      ⚠ $TCTL below is a SEPARATELY STAGED binary and does NOT track the nodes.
#      RE-VERIFY AFTER ANY NODE UPGRADE — nothing else would surface a break
#      until a real power outage:
#        sudo /mnt/tank/system/talos/talosctl --talosconfig <cfg> -n <node> version
#
#      ⚠ SKEW AS OF 2026-09-22: the staged binary is **v1.13.2** (mtime Jun 1) and
#      both clusters are on **v1.14.0** (rolled 2026-09-19) — a FULL MINOR apart.
#      The only pairing ever verified here was same-minor (v1.13.2 client against a
#      v1.13.7 node, 2026-07-29). This is UNVERIFIED, not known-broken: re-stage via
#      setup-talos-shutdown-orchestrator.sh (TALOSCTL_VERSION now defaults to
#      v1.14.0) and run the version check above.
#      ⚠ The credentials are NOT the problem — read from Doppler 2026-09-22 they are
#      valid to May 2036. An earlier suspicion that they had expired came from the
#      setup script documenting `--crt-ttl 720h`, which does not match what was
#      actually minted; that comment is now corrected.
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

# ══ NODE INVENTORY — THE ONLY THING THAT CHANGES AT CUTOVER ════════════════
# Space-separated. ⚠ ALL_NODES is DERIVED, not maintained by hand: the previous
# version listed all six IPs a second time, so editing one list and not the other
# would have left the poll watching nodes the shutdown never targeted (or worse,
# reported "all down" while a node was still up).
#
# ⚠ THIS MUST SURVIVE A MIXED TOPOLOGY, NOT JUST THE END STATE. The audit
# recommends destroying PRD first and DEV last, so there is a window where PRD is
# one MS-A2 and DEV is still three Q170S1 nodes. Nothing below hardcodes a count.
#
# MS-A2 end state (each box takes its cluster's FIRST node IP):
#   DEV_NODES="10.10.5.14"
#   PRD_NODES="10.10.5.11"
DEV_NODES="10.10.5.14 10.10.5.15 10.10.5.16"
PRD_NODES="10.10.5.11 10.10.5.12 10.10.5.13"

ALL_NODES="$DEV_NODES $PRD_NODES"
NODE_COUNT=$(set -- $ALL_NODES; echo $#)

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

# 1) fire --force shutdown at every node, all in parallel.
#
# ⚠ ONE INVOCATION PER NODE, NOT ONE PER CLUSTER — changed 2026-09-22. The previous
# version passed a comma-separated list (`--nodes .11,.12,.13`) as a single call.
# That is fine when every listed node exists, but during the MS-A2 transition a
# stale list will contain IPs of nodes that have been physically removed, and
# talosctl's behaviour when part of a multi-node target is unreachable was never
# verified here. If it aborts rather than proceeding, a stale list would mean the
# LIVE node never receives the shutdown — silently, on battery.
#
# Per-node calls make that impossible: an unreachable IP costs exactly one failed
# background job and one rc= line in the log. It also makes the log say WHICH node
# failed instead of which cluster.
pids=()
for ip in $PRD_NODES; do
  "$TCTL" --talosconfig "$PRD_CFG" shutdown --force --nodes "$ip" --endpoints "$ip" >>"$LOG" 2>&1 &
  pids+=("$!|prd|$ip")
done
for ip in $DEV_NODES; do
  "$TCTL" --talosconfig "$DEV_CFG" shutdown --force --nodes "$ip" --endpoints "$ip" >>"$LOG" 2>&1 &
  pids+=("$!|dev|$ip")
done

for entry in "${pids[@]}"; do
  pid=${entry%%|*}; rest=${entry#*|}; cl=${rest%%|*}; ip=${rest#*|}
  wait "$pid"; log "$cl $ip shutdown --force rc=$?"
done
log "shutdown --force delivered to all ${NODE_COUNT} node(s); polling apid until down."

# 2) poll until all nodes stop answering apid, or the timeout backstop
start=$(date +%s)
while :; do
  up=0
  for ip in $ALL_NODES; do probe "$ip" && up=$((up+1)); done
  el=$(( $(date +%s) - start ))
  log "  [${el}s] nodes still answering apid: ${up}/${NODE_COUNT}"
  [ "$up" -eq 0 ] && { log "all nodes down at ${el}s"; break; }
  [ "$el" -ge "$TIMEOUT" ] && { log "TIMEOUT ${TIMEOUT}s reached (${up}/${NODE_COUNT} still up) — halting NAS anyway"; break; }
  sleep "$POLL"
done

# 3) halt the NAS LAST — the #57 hook arms the UPS on this poweroff
log "=== halting NAS LAST → /sbin/shutdown -P now (arms UPS via #57 hook) ==="
/sbin/shutdown -P now
