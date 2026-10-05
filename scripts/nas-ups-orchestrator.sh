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
#   1) talosctl shutdown --force --wait=false at EVERY node, all in parallel
#      (one invocation per node — see the note at the fire loop for why). The
#      nodes are the two MS-A2 single-node clusters, msa2-prd and msa2-dev; the
#      six Q170S1 nodes left this list at their teardown (kube-infra#1443,
#      2026-10). --force skips ONLY Talos's API-dependent pod *drain*
#      (CordonAndDrainNode); StopAllPods (graceful CRI SIGTERM + 30 s ceiling)
#      and the fs sync still run, so data flushing is unchanged.
#
#      ⚠ --force IS LOAD-BEARING. Talos's drain uses the Kubernetes *eviction*
#      API. Each MS-A2 cluster is ONE node, so there is nowhere to evict to, and
#      CNPG keeps its own `<cluster>-primary` PDB for `giks` and `w1` at
#      `instances: 1` (kube-infra keeps CNPG's PDBs on purpose; cutover
#      inventory row 15, S1-11). A single-node drain was measured never to
#      finish on msa2-dev (2026-09-24, kube-infra plan Task C1 Step 5): without
#      --force every node would burn Talos's hardcoded 5-min DrainTimeout on a
#      battery-constrained shutdown. Do NOT remove it. (On the Q170S1 estate
#      the same flag also beat etcd-quorum loss and allowed=0 PDBs on Longhorn,
#      Prometheus & co.: git history of this file, before the teardown.)
#
#      Talos v1.14 verified: `shutdown --force` exists with unchanged semantics
#      ("force a node to shutdown without a cordon/drain").
#
#      ⚠ $TCTL below is a SEPARATELY STAGED binary and does NOT track the nodes.
#      RE-VERIFY AFTER ANY NODE UPGRADE — nothing else would surface a break
#      until a real power outage. `setup-talos-shutdown-orchestrator.sh
#      --print-checks` prints one command that checks every (config, address)
#      pair below plus the staged script's sha256.
#   2) poll apid (:50000) on each address whose shutdown was ACCEPTED, until
#      all are down, or a timeout backstop so the NAS never hangs forever.
#   3) halt the NAS LAST → /sbin/shutdown -P now. That fires the #57 Init/Shutdown
#      hook (nas-ups-shutdown.sh) which arms the UPS (sdtype=5, hard hibernate).
#      Because the NAS is genuinely last, ups.delay.shutdown only needs to cover
#      the NAS's own poweroff (trimmed to 90s).
#
# ══ The three rules (each MS-A2 cluster is ONE node with its OWN PKI) ═════════
# Each cluster has its own os:operator talosconfig (kube-infra bootstrap.sh
# menu 10 mints TALOS_NAS_SHUTDOWN_CONFIG_MSA2_{DEV,PRD};
# setup-talos-shutdown-orchestrator.sh stages each one that exists as
# msa2-{dev,prd}-shutdown.talosconfig). All three rules exist so a box that is
# NOT there — in Talos maintenance mode (a reinstall), unplugged, its config
# not staged, or on a PKI the staged config no longer matches — costs the
# battery nothing:
#
#   a) ONLY AN ACCEPTED SHUTDOWN IS WAITED FOR. apid's :50000 probe is
#      UNAUTHENTICATED: a maintenance-mode box answers it, and so does a box
#      whose PKI no longer matches the staged config. Polling such an address
#      would read "up" until the 300 s backstop. So an address joins the poll
#      only if its shutdown call returned 0. rc!=0 means the shutdown was not
#      delivered to a node of THAT cluster (no route / dial timeout / capped by
#      rule (c), rc=124 / x509 unknown authority / maintenance mode), so nothing
#      this script does would bring that address down, and waiting for it only
#      drains the battery. ⚠ Such a box is NOT shut down: when apc1 cuts power
#      it is hard-cut. The log says so (an `⚠ NOT shut down` line per box, and
#      a summary line when no box accepted). That is why the setup script's
#      printed check must show `Server:` for every box after each re-stage.
#   b) --wait=false, so rc=0 means the Version pre-check AND the Shutdown RPC
#      both succeeded, and rc!=0 means the shutdown was not delivered. (talosctl
#      v1.14's --wait=false branch first runs helpers.ClientVersionCheck — a
#      Version RPC — and returns before any Shutdown if it fails, so os:operator
#      must keep Version access; the setup script's --print-checks `version`
#      call proves that pre-flight for every pair.) With the default --wait,
#      talosctl instead tracks events after the RPC, and a tracker error after a
#      delivered shutdown would ALSO be rc!=0 — which (a) would misread as "not
#      delivered". Waiting is the poll's job here.
#   c) Every call is capped at CALL_TIMEOUT via coreutils `timeout`. That BOUNDS
#      the delay, it does not remove it: the poll, and so the NAS halt, starts
#      only after every call has returned, so an address that connects and then
#      hangs holds everything up by at most CALL_TIMEOUT+5 s (see
#      CALL_TIMEOUT for why 60 s).
#
# (Until the Q170S1 teardown there was a rule (d) and a second path for the six
# old nodes, polled whatever their rc while they were live: git history.)
#
# ══ Power ══════════════════════════════════════════════════════════════════════
# Both MS-A2 boxes are on apc1's battery outlets since 2026-09-28 (the cutover;
# ~163 W total, ~26 min runtime at 100 % read 19:06Z that day — truenas-infra
# CLAUDE.md § UPS / NUT). So on an outage this script shuts them down cleanly,
# and apc1's kill-power cycle plus BIOS "AC power loss: Always On" (both boxes;
# kube-infra plan G1 / H2) boots them again when mains return. A box that is
# NOT on apc1 but still powered (a drill on mains, a box moved to another
# supply) is shut down cleanly and STAYS OFF until someone powers it on,
# because its AC never drops — an availability cost, never a data one.
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

# ── Test seams ── every UPS_ORCH_* variable is for tests/test_ups_orchestrator.py
# ONLY and is unset in production (upsmon runs SHUTDOWNCMD with a clean
# environment). Unset, each default below is the literal value this script has
# always used.
TALOS_DIR=${UPS_ORCH_TALOS_DIR:-/mnt/tank/system/talos}
HALT=${UPS_ORCH_HALT:-/sbin/shutdown}

TCTL=$TALOS_DIR/talosctl
MSA2_DEV_CFG=$TALOS_DIR/msa2-dev-shutdown.talosconfig
MSA2_PRD_CFG=$TALOS_DIR/msa2-prd-shutdown.talosconfig
LOG=${UPS_ORCH_LOG:-/mnt/tank/system/nut/last-node-shutdown.log}

# ══ NODE INVENTORY ═════════════════════════════════════════════════════════
# Space-separated, one list and one config per cluster. ⚠ The poll set is
# DERIVED (rule (a)), never maintained by hand: an earlier version listed every
# IP a second time, so editing one list and not the other would have left the
# poll watching nodes the shutdown never targeted (or worse, reported "all
# down" while a node was still up).
#
# Each box's FINAL address (kube-infra talos-os/estates.yaml, plan D12). Its
# BUILD address (msa2-prd 10.10.5.17, msa2-dev 10.10.5.18) left with the Q170S1
# teardown: an installed box runs its static FINAL address, and a box in
# maintenance mode (a DHCP lease) accepts no authenticated shutdown anyway.
# setup-talos-shutdown-orchestrator.sh reads these two lines and the two *_CFG
# lines above by name (check_pairs): keep their form.
MSA2_PRD_NODES="10.10.5.11"
MSA2_DEV_NODES="10.10.5.12"

TIMEOUT=${UPS_ORCH_TIMEOUT:-300}  # 5 min poll backstop so the NAS never hangs forever. Real drill
              # 2026-06-01: talosctl --force returned in 187s with nodes already
              # down (poll then needed only 8s), so this is pure cushion.
POLL=${UPS_ORCH_POLL:-10}         # seconds between apid liveness sweeps
CALL_TIMEOUT=${UPS_ORCH_CALL_TIMEOUT:-60}  # cap per shutdown call (rule c).
              # A delivered shutdown returns in well under a second (--wait=false);
              # an absent box fails in ~3 s (no ARP reply) and a silently-dropping
              # one in ~20 s (talosctl's MinConnectTimeout), so neither needs the
              # cap. It is for a call that connects and then hangs, and it stays
              # HIGH on purpose: a cap that fires on a slow but delivered shutdown
              # reads as rc=124, keeps that box out of the poll, and lets the NAS
              # halt (and apc1 cut power 90 s later) while it is still stopping
              # Postgres. A high cap costs at most 65 s of battery, and only when
              # a call hangs. Pinned by a test; re-measure before lowering it.

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

# Run "$@" under coreutils `timeout` when it exists (it does on TrueNAS SCALE):
# SIGTERM at the cap, SIGKILL 5 s later if that was ignored; rc 124 either way.
# Without `timeout`, run unbounded rather than not at all.
bounded(){
  if command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=5 "$CALL_TIMEOUT" "$@"
  else
    "$@"
  fi
}

log "=== SHUTDOWNCMD orchestrator START (committed — no mains re-check) ==="

if [ ! -x "$TCTL" ]; then
  log "FATAL: talosctl not found/executable at $TCTL — halting NAS anyway so it doesn't hang"
  "$HALT" -P now
  exit 0
fi

# 1) fire --force shutdown at every node, all in parallel.
#
# ⚠ ONE INVOCATION PER NODE, NOT ONE PER CLUSTER (since 2026-09-22). A single
# call with a comma-separated --nodes list was fine while every listed node
# existed, but talosctl's behaviour when part of a multi-node target is
# unreachable was never verified here: if it aborts rather than proceeding, a
# stale list would mean the LIVE node never receives the shutdown — silently,
# on battery. Per-node calls make that impossible: an unreachable IP costs
# exactly one failed background job and one rc= line in the log, and the log
# says WHICH node failed.
#
# Rules (b) and (c) from the header. A cluster whose config is not staged (its
# Doppler key did not exist at staging time) is skipped, and logged as such.
pids=()
fire(){  # $1 = cluster, $2 = its talosconfig, $3.. = its addresses
  local cl=$1 cfg=$2 ip
  shift 2
  if [ ! -f "$cfg" ]; then
    log "$cl: no talosconfig staged at $cfg — skipped, NOT shut down (re-run setup-talos-shutdown-orchestrator.sh)"
    return 0
  fi
  for ip in "$@"; do
    bounded "$TCTL" --talosconfig "$cfg" shutdown --force --wait=false --nodes "$ip" --endpoints "$ip" >>"$LOG" 2>&1 &
    pids+=("$!|$cl|$ip")
  done
}
# shellcheck disable=SC2086  # the node lists are space-separated on purpose
fire msa2-prd "$MSA2_PRD_CFG" $MSA2_PRD_NODES
# shellcheck disable=SC2086
fire msa2-dev "$MSA2_DEV_CFG" $MSA2_DEV_NODES

in_list(){ case " $2 " in *" $1 "*) return 0 ;; esac; return 1; }  # is $1 in list $2?

accepted=""   # every address a call to which was accepted (rc=0) — rule (a)
for entry in ${pids[@]+"${pids[@]}"}; do  # empty-safe under set -u on bash < 4.4
  pid=${entry%%|*}; rest=${entry#*|}; cl=${rest%%|*}; ip=${rest#*|}
  wait "$pid"; rc=$?
  log "$cl $ip shutdown --force rc=$rc"
  if [ "$rc" -eq 0 ]; then
    accepted="$accepted $ip"
  else
    log "  $cl $ip did not accept — NOT polled, ⚠ NOT shut down (no box here, maintenance mode, or another cluster's PKI)"
  fi
done

# The poll set: every accepted address, each once.
POLL_NODES=""
poll_add(){ in_list "$1" "$POLL_NODES" || POLL_NODES="${POLL_NODES:+$POLL_NODES }$1"; }
for ip in $accepted; do poll_add "$ip"; done
# shellcheck disable=SC2086  # word-splitting the list is the point
NODE_COUNT=$(set -- $POLL_NODES; echo $#)
if [ "$NODE_COUNT" -eq 0 ]; then
  log "⚠ no node accepted its shutdown — nothing to wait for; any box still up loses power when apc1 cuts"
fi
log "shutdown --force sent; polling apid on ${NODE_COUNT} node(s) until down: ${POLL_NODES}"

# 2) poll until all nodes stop answering apid, or the timeout backstop
start=$(date +%s)
while :; do
  up=0
  for ip in $POLL_NODES; do probe "$ip" && up=$((up+1)); done
  el=$(( $(date +%s) - start ))
  log "  [${el}s] nodes still answering apid: ${up}/${NODE_COUNT}"
  [ "$up" -eq 0 ] && { log "all nodes down at ${el}s"; break; }
  [ "$el" -ge "$TIMEOUT" ] && { log "TIMEOUT ${TIMEOUT}s reached (${up}/${NODE_COUNT} still up) — halting NAS anyway"; break; }
  sleep "$POLL"
done

# 3) halt the NAS LAST — the #57 hook arms the UPS on this poweroff
log "=== halting NAS LAST → /sbin/shutdown -P now (arms UPS via #57 hook) ==="
"$HALT" -P now
