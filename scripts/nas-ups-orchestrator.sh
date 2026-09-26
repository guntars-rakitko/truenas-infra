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
#   1) talosctl shutdown --force EVERY node of every cluster, all in parallel
#      (one invocation per node — see the note at the loop for why): the six
#      Q170S1 nodes (kub-prd, kub-dev) exactly as before, plus the two MS-A2
#      single-node clusters (msa2-prd, msa2-dev) — see "MS-A2" below.
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
#      dropped, so the three `instance-manager-*` PDBs vanish; the repo-owned
#      PDBs moved to kube-infra `flux-cd/legacy-q170s1/` (Q170S1 only) and the
#      chart-level ones render none at n=1 (kube-infra plan Task D2). What
#      REMAINS is CNPG's own `<cluster>-primary` PDB for `giks` and `w1` at
#      `instances: 1` (kept deliberately; cutover inventory row 15, S1-11) —
#      and at n=1 ANY blocking PDB makes the drain unfinishable: a single-node
#      drain was measured never to finish on msa2-dev (2026-09-24, kube-infra
#      plan Task C1 Step 5). truenas-infra CLAUDE.md § UPS / NUT has the full
#      paragraph. `talos-os/bootstrap.sh` keeps `--disable-eviction` on
#      `kubectl drain` for the same reason.
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
#      so the NAS never hangs forever). The poll set is every Q170S1 node (as
#      before) plus each MS-A2 address that ACCEPTED its shutdown — narrowed at
#      the FINAL addresses once their Q170S1 cluster is gone (rule (d) below).
#   3) halt the NAS LAST → /sbin/shutdown -P now. That fires the #57 Init/Shutdown
#      hook (nas-ups-shutdown.sh) which arms the UPS (shutdown.return). Because the
#      NAS is genuinely last, ups.delay.shutdown only needs to cover the NAS's own
#      poweroff (trimmed to 90s).
#
# ══ MS-A2 (msa2-prd, msa2-dev) — added 2026-09-26, kube-infra cutover row 15 ══
# Each MS-A2 cluster is ONE node with its OWN PKI, so it has its own os:operator
# talosconfig (kube-infra bootstrap.sh menu 10 mints TALOS_NAS_SHUTDOWN_CONFIG_
# MSA2_{DEV,PRD}; setup-talos-shutdown-orchestrator.sh stages each one that
# exists as msa2-{dev,prd}-shutdown.talosconfig). Four rules differ from the
# Q170S1 path, and all four exist so an MS-A2 box that is NOT there — not built
# (msa2-prd today), in Talos maintenance mode (msa2-dev during a reinstall),
# unplugged, or on the other side of a cutover — costs nothing:
#
#   a) ONLY AN ACCEPTED SHUTDOWN IS WAITED FOR. apid's :50000 probe is
#      UNAUTHENTICATED: a maintenance-mode box answers it, and so does a box
#      whose PKI no longer matches the staged config. Polling such an address
#      would read "up" until the 300 s backstop — the exact burn the kube-infra
#      cutover inventory warns about (row 15). So an MS-A2 address joins the poll
#      only if its shutdown call returned 0. rc!=0 means the shutdown was not
#      delivered to a node of THAT cluster (no route / dial timeout / capped by
#      rule (c), rc=124 / x509 unknown authority / maintenance mode), so nothing
#      this script does would bring that address down and waiting for it only
#      drains the battery.
#      This alone covers an address that is ONLY in an MS-A2 list: the BUILD
#      addresses. The FINAL addresses are ALSO in PRD_NODES — rule (d).
#   b) --wait=false, so rc=0 means the Version pre-check AND the Shutdown RPC
#      both succeeded, and rc!=0 means the shutdown was not delivered. (talosctl
#      v1.14's --wait=false branch first runs helpers.ClientVersionCheck — a
#      Version RPC — and returns before any Shutdown if it fails, so os:operator
#      must keep Version access; the setup script's --print-checks `version`
#      call proves that pre-flight for every pair.) With the default --wait,
#      talosctl instead tracks events after the RPC, and a tracker error after a
#      delivered shutdown would ALSO be rc!=0 — which (a) would misread as "not
#      delivered". Waiting is the poll's job here, as it is for every node.
#   c) Every MS-A2 call is capped at MSA2_CALL_TIMEOUT via coreutils `timeout`.
#      That BOUNDS the delay, it does not remove it: the poll, and so the NAS
#      halt, starts only after every call has returned, so an address that
#      swallows packets holds everything up by at most MSA2_CALL_TIMEOUT+5 s.
#      Today the ~187 s Q170S1 calls hide it; after the cutovers those fail fast
#      and the cap is the delay (see MSA2_CALL_TIMEOUT for why 60 s).
#   d) A FINAL ADDRESS (.11/.12 — in PRD_NODES AND in an MS-A2 list) FOLLOWS THE
#      Q170S1 RULE WHILE kub-prd IS LIVE, AND RULE (a) ONCE IT IS GONE. While
#      at least one kub-prd call returned 0 (before prd's cutover, and after a
#      rollback) .11/.12 are polled whatever their rc — unchanged. Once NO
#      kub-prd call returned 0 (after the cutover the old nodes' power cords are
#      out, kube-infra plan D12) each is polled only if some call to it returned
#      0. Without this, from prd's cutover until its teardown an msa2 box at
#      .11/.12 that does not accept (maintenance mode, config not staged, a
#      stale PKI) was polled to the 300 s backstop, like a Q170S1 node that
#      rejects. ⚠ The ONE change to the live Q170S1 path: if EVERY kub-prd call
#      fails while the old nodes are up, .11/.12 are no longer waited for. For a
#      broken prd config that changes nothing in practice (a rejecting .13 is
#      still polled to the backstop). For a --wait tracker error on all three
#      AFTER the shutdown was delivered, the NAS can halt while .11/.12 are still
#      going down — gated by .13 and kub-dev, which shut down in parallel, then
#      apc1's 90 s ups.delay.shutdown. One failing kub-prd call changes nothing.
#      Both sides are pinned by tests; CLAUDE.md § UPS / NUT has the trade-off.
#   --force stays (see the paragraph above): a single-node drain cannot finish.
#
# BOTH addresses of each box are listed — BUILD first, FINAL (cutover) second:
# msa2-prd 10.10.5.17 → 10.10.5.11, msa2-dev 10.10.5.18 → 10.10.5.12 (kube-infra
# talos-os/estates.yaml, plan D12), so this script needs NO edit at either
# cutover or at a rollback. The address that is not the box's today is rejected
# (x509 — another cluster's CA — or no route), which executes nothing. Today
# .11/.12 are kub-prd-01/-02, polled through the Q170S1 list. After prd's
# cutover .11 is msa2-prd: the Q170S1 prd config is rejected there instead, the
# msa2-prd config is accepted, and .11 is polled once (the poll set is
# de-duplicated). Even a mis-aimed ACCEPTED shutdown would be harmless: this
# script exists to shut down every node on the rack.
#
# ⚠ UNKNOWN (2026-09-26): whether the MS-A2 boxes are on this NAS's UPS (apc1).
#   The DESIGN assumes they are (kube-infra docs/msa2-audit/02-design-decisions.md
#   § 10 gate 7 plans a real UPS drill to "confirm both new boxes go down" and
#   sizes the UPS load as "~2×65–100 W + NAS 25 W + networking 70 W") — but
#   nothing confirms it physically. Either way every Drill A (`upsmon -c fsd`)
#   now takes the msa2 clusters down too: power-cycled back up if on apc1, left
#   OFF if not.
#   ON apc1 → shut down cleanly here; apc1's kill-power cycle + BIOS "AC power
#     loss: Always On" (both boxes; kube-infra plan G1 / H2) boots them again,
#     like the Q170S1 nodes.
#   NOT on apc1, real outage → they lost power when the mains did; the call fails
#     fast (no route) and rules (a)/(d) keep them out of the poll.
#   NOT on apc1 but still powered (a drill on mains, or a second UPS) → they are
#     shut down cleanly and STAY OFF until someone powers them on, because their
#     AC never drops. An availability cost, never a data one — and the opposite
#     choice (leave them out) hard-cuts a single-instance Postgres on a non-PLP
#     drive if they ARE on apc1.
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
DEV_CFG=$TALOS_DIR/dev-shutdown.talosconfig
PRD_CFG=$TALOS_DIR/prd-shutdown.talosconfig
MSA2_DEV_CFG=$TALOS_DIR/msa2-dev-shutdown.talosconfig
MSA2_PRD_CFG=$TALOS_DIR/msa2-prd-shutdown.talosconfig
LOG=${UPS_ORCH_LOG:-/mnt/tank/system/nut/last-node-shutdown.log}

# ══ NODE INVENTORY ═════════════════════════════════════════════════════════
# Space-separated. ⚠ The poll set is DERIVED, not maintained by hand: an earlier
# version listed all six IPs a second time, so editing one list and not the other
# would have left the poll watching nodes the shutdown never targeted (or worse,
# reported "all down" while a node was still up).
#
# ⚠ THIS MUST SURVIVE A MIXED TOPOLOGY, NOT JUST THE END STATE. PRD cuts over
# first and DEV after PRD's ~2-week rollback window closes, so for weeks some
# clusters are MS-A2 and some Q170S1 — and a rollback can swap them back. Nothing
# below hardcodes a count.
#
# Q170S1 (kub-prd, kub-dev) — LIVE until each cluster's cutover, then its
# rollback target. Keep both lists and both configs staged until that cluster's
# teardown (kube-infra plan § Cutover inventory row 15). ⚠ Teardown is a code
# change with its own PR, not a deletion of these lines: under `set -u` a
# leftover reference to a deleted list aborts this script BEFORE it fires the
# msa2 shutdowns or halts the NAS. CLAUDE.md § UPS / NUT "At teardown" lists
# every reference, here and in setup-talos-shutdown-orchestrator.sh.
DEV_NODES="10.10.5.14 10.10.5.15 10.10.5.16"
PRD_NODES="10.10.5.11 10.10.5.12 10.10.5.13"
Q170S1_NODES="$DEV_NODES $PRD_NODES"

# MS-A2 (msa2-prd, msa2-dev) — one node each, BUILD address then FINAL address.
# Both stay listed through cutover and rollback (see "MS-A2" in the header);
# drop the BUILD address at that cluster's teardown.
MSA2_PRD_NODES="10.10.5.17 10.10.5.11"
MSA2_DEV_NODES="10.10.5.18 10.10.5.12"

TIMEOUT=${UPS_ORCH_TIMEOUT:-300}  # 5 min poll backstop so the NAS never hangs forever. Real drill
              # 2026-06-01: talosctl --force returned in 187s with nodes already
              # down (poll then needed only 8s), so this is pure cushion.
POLL=${UPS_ORCH_POLL:-10}         # seconds between apid liveness sweeps
MSA2_CALL_TIMEOUT=${UPS_ORCH_MSA2_CALL_TIMEOUT:-60}  # cap per MS-A2 shutdown call (rule c).
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
    timeout --kill-after=5 "$MSA2_CALL_TIMEOUT" "$@"
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

# MS-A2: rules (b) and (c) from the header. A cluster whose config is not staged
# (not built yet, or its Doppler key did not exist at staging time) is skipped.
fire_msa2(){  # $1 = cluster, $2 = its talosconfig, $3.. = its candidate addresses
  local cl=$1 cfg=$2 ip
  shift 2
  if [ ! -f "$cfg" ]; then
    log "$cl: no talosconfig staged at $cfg — skipped (not built, or not staged yet)"
    return 0
  fi
  for ip in "$@"; do
    bounded "$TCTL" --talosconfig "$cfg" shutdown --force --wait=false --nodes "$ip" --endpoints "$ip" >>"$LOG" 2>&1 &
    pids+=("$!|$cl|$ip")
  done
}
# shellcheck disable=SC2086  # the node lists are space-separated on purpose
fire_msa2 msa2-prd "$MSA2_PRD_CFG" $MSA2_PRD_NODES
# shellcheck disable=SC2086
fire_msa2 msa2-dev "$MSA2_DEV_CFG" $MSA2_DEV_NODES

in_list(){ case " $2 " in *" $1 "*) return 0 ;; esac; return 1; }  # is $1 in list $2?

accepted=""   # every address a call to which was accepted (rc=0) — rules (a), (d)
live=""       # the Q170S1 clusters with at least one accepted call — rule (d)
for entry in "${pids[@]}"; do
  pid=${entry%%|*}; rest=${entry#*|}; cl=${rest%%|*}; ip=${rest#*|}
  wait "$pid"; rc=$?
  log "$cl $ip shutdown --force rc=$rc"
  if [ "$rc" -eq 0 ]; then
    accepted="$accepted $ip"
    case "$cl" in msa2-*) ;; *) live="$live $cl" ;; esac
  else
    case "$cl" in
      msa2-*)
        if in_list "$ip" "$Q170S1_NODES"; then
          log "  $cl $ip did not accept (a FINAL address: rule (d) decides whether it is polled)"
        else
          log "  $cl $ip did not accept — NOT polled (no box here, maintenance mode, or another cluster's PKI)"
        fi ;;
    esac
  fi
done

# The poll set, each address once:
#  - every Q170S1 node, whatever its rc (unchanged behaviour) — except a FINAL
#    address once its Q170S1 cluster is gone, which follows rule (a) (rule d);
#  - every accepted MS-A2 address.
POLL_NODES=""
poll_add(){ in_list "$1" "$POLL_NODES" || POLL_NODES="${POLL_NODES:+$POLL_NODES }$1"; }
poll_q170s1(){  # $1 = Q170S1 cluster, $2.. = its nodes
  local cl=$1 ip
  shift
  for ip in "$@"; do
    if in_list "$ip" "$MSA2_PRD_NODES $MSA2_DEV_NODES" && ! in_list "$ip" "$accepted"; then
      if in_list "$cl" "$live"; then
        log "  $ip still polled — a $cl node accepted, so the Q170S1 rule holds here (rule d)"
      else
        log "  $ip NOT polled — no $cl node accepted (that cluster is gone) and no call to $ip did (rule d)"
        continue
      fi
    fi
    poll_add "$ip"
  done
}
# shellcheck disable=SC2086  # the node lists are space-separated on purpose
poll_q170s1 dev $DEV_NODES
# shellcheck disable=SC2086
poll_q170s1 prd $PRD_NODES
for ip in $accepted; do poll_add "$ip"; done
# shellcheck disable=SC2086  # word-splitting the list is the point
NODE_COUNT=$(set -- $POLL_NODES; echo $#)
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
