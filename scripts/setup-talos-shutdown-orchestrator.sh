#!/usr/bin/env bash
# setup-talos-shutdown-orchestrator.sh — stage the Path B UPS orchestrator on
# the NAS (kube-infra #611). Idempotent. Run from the operator's laptop.
#
# Places these artifacts under /mnt/tank/system/talos/ via the TrueNAS API
# (filesystem.put — runs as root; NOT ssh+sudo):
#   1. talosctl                          (linux-amd64, pinned, mode 0755)
#   2. dev-shutdown.talosconfig          (kub-dev os:operator, mode 0600)
#   3. prd-shutdown.talosconfig          (kub-prd os:operator, mode 0600)
#   4. msa2-dev-shutdown.talosconfig     (msa2-dev os:operator, mode 0600) ┐ ONLY when
#   5. msa2-prd-shutdown.talosconfig     (msa2-prd os:operator, mode 0600) ┘ its key is set
#   6. nas-ups-orchestrator.sh           (the SHUTDOWNCMD script, mode 0750)
#
# ⚠ Keep 2 and 3 staged until the Q170S1 TEARDOWN, not just until cutover: they
# are what shuts the old nodes down during each cluster's ~2-week rollback window
# (kube-infra plan § Cutover inventory row 15).
#
# The msa2 configs are OPTIONAL on purpose: a cluster that is not built yet
# (msa2-prd until kube-infra plan Part H) has no key, and its absence must not
# block re-staging the live path. The orchestrator skips a cluster whose config
# is not staged. ⚠ An msa2 config is never DELETED from the NAS by this script,
# and a STALE one is not harmless: its shutdown is rejected, so that box is not
# shut down at all, and at a FINAL address (.11/.12, also in the Q170S1 prd list
# until prd's teardown) the orchestrator then polls it to the 300 s backstop.
# Re-run this script after every `bootstrap.sh <msa2-env>` menu 10 so the
# staged copy tracks Doppler, and run the printed check.
#
# `--print-checks` prints the authenticated post-staging check (below) and exits;
# it needs no credentials and touches nothing.
#
# It does NOT wire ups.config.shutdowncmd — that's a separate, deliberate
# step (the maintenance-window Task 5) so staging stays non-disruptive: this
# script changes nothing about the live shutdown behaviour, it only puts the
# tools in place + smoke-tests them read-only.
#
# Source of truth for the scoped talosconfigs is Doppler infrastructure/ops
# (base64), so a NAS rebuild can restore them. Generate + push them first:
#   cd ~/github/kube-infra/talos-os
#   for cl in dev prd; do
#     talosctl --talosconfig generated/$cl/talosconfig --nodes <one-node-ip> \
#       config new --roles os:operator --crt-ttl 87600h generated/$cl/nas-shutdown.talosconfig
#   done
#
# ⚠ THE TTL ABOVE WAS `720h` (30 DAYS) UNTIL 2026-09-22, AND THAT WAS A LANDMINE.
# The credentials actually deployed do not match it: read from Doppler on
# 2026-09-22 they are `notBefore=Jun 1 2026 / notAfter=May 29 2036` — about TEN
# YEARS. So whoever minted them did not follow this line, and the line sat here
# waiting for the next person who would.
#
# Why that mattered: nothing rotates this credential (no scheduler, no job — unlike
# `render-cluster-agent-kubeconfigs.sh`, which prints remaining lifetime) and nothing
# ALERTS on it (verified: no `prometheus-rules-*.yaml` in kube-infra references it).
# A 30-day credential minted from this comment would therefore have died silently
# after a month, and the failure surfaces ONLY during a real power cut — the one
# moment it cannot be discovered safely.
#
# ⚠ THE NEXT RE-ISSUE IS ALREADY SCHEDULED: the MS-A2 greenfield rebuild mints a new
# PKI, which invalidates both of these configs. That is the moment this comment would
# have been followed. It now says 87600h, matching what is actually deployed.
#
# The long TTL is DELIBERATE, and the reasoning is the opposite of the cluster-agent's
# (which moved to 1 year after 90 days lapsed unnoticed): this is a BREAK-GLASS
# credential whose entire job is to work during an unattended power failure. An
# expiring credential on that path converts a survivable outage into data loss.
# The blast radius is bounded by the role, not by time — `os:operator` can
# shutdown/reboot/etcd-snapshot/read-logs and CANNOT reset, wipe, upgrade, read
# secrets/PKI/config, apply config or mint creds. Configs are 0600 root-owned and
# apid is firewalled to the NAS IP.
# ⚠ If you ever shorten it, add an expiry alert IN THE SAME CHANGE.
#   doppler secrets set TALOS_NAS_SHUTDOWN_CONFIG_DEV="$(base64 < generated/dev/nas-shutdown.talosconfig)" \
#     --project infrastructure --config ops
#   doppler secrets set TALOS_NAS_SHUTDOWN_CONFIG_PRD="$(base64 < generated/prd/nas-shutdown.talosconfig)" \
#     --project infrastructure --config ops
#
# Prereqs (Doppler infrastructure/ops; manage.sh exports the first two):
#   TRUENAS_HOST                    e.g. nas.w1.lv (or 10.10.5.10)
#   TRUENAS_API_KEY                 long-lived key
#   TALOS_NAS_SHUTDOWN_CONFIG_DEV   base64 of the dev os:operator talosconfig
#   TALOS_NAS_SHUTDOWN_CONFIG_PRD   base64 of the prd os:operator talosconfig
#   TALOS_NAS_SHUTDOWN_CONFIG_MSA2_DEV  (optional) base64, msa2-dev os:operator
#   TALOS_NAS_SHUTDOWN_CONFIG_MSA2_PRD  (optional) base64, msa2-prd os:operator
#     The msa2 keys are minted by kube-infra `talos-os/bootstrap.sh <cluster>`
#     menu 10 (`TALOS_NAS_SHUTDOWN_CONFIG_$(_doppler_suffix)`: msa2-dev →
#     MSA2_DEV), 10-year os:operator, against that cluster's OWN CA.
#
# Run it as (the orchestrator it uploads is whatever THIS checkout holds, so
# bring `main` up to date first — a stale checkout re-stages the old script and
# every credential check still passes; the printed check compares the staged
# script's sha256 with this checkout's):
#   cd ~/github/truenas-infra && git switch main && git pull --ff-only && git log -1 --oneline
#   doppler run -p infrastructure -c ops -- ./scripts/setup-talos-shutdown-orchestrator.sh
#
# Pin TALOSCTL_VERSION to the RUNNING cluster version.
#
# ⚠ MIXED ESTATE (2026-09-26): the Q170S1 nodes run v1.14.0 and the MS-A2 boxes
# v1.14.1. One binary serves both; same MINOR is the compatibility line this file
# already relies on (a client newer than a server only WARNS — talosctl
# ClientVersionCheck). The default stays v1.14.0, the version the live Q170S1
# path was re-staged with, so adding the msa2 clusters changes nothing about it.
# Move to the msa2 version at the cutover (kube-infra plan § Cutover inventory
# row 15), and re-run the checks from `--print-checks` against every node.
#
# ⚠ WAS v1.13.2, PINNED TO A CLUSTER STATE THAT NO LONGER EXISTS. That pin dated
# from a rollback off v1.13.3; both clusters have since rolled to **v1.14.0**
# (2026-09-19), leaving the staged binary a FULL MINOR behind. The orchestrator's
# own header only ever claimed a SAME-MINOR pairing was verified, and it warns that
# "nothing else would surface a break until a real power outage".
#
# ⚠ RE-STAGE THIS BINARY AFTER EVERY CLUSTER UPGRADE. Re-running this script is the
# mechanism; nothing does it automatically and nothing alerts on the skew. Verify
# read-only afterwards (needs root on the NAS — the configs are 0600):
#   sudo /mnt/tank/system/talos/talosctl \
#     --talosconfig /mnt/tank/system/talos/prd-shutdown.talosconfig \
#     -n <node-ip> version
set -euo pipefail

TALOSCTL_VERSION="${TALOSCTL_VERSION:-v1.14.0}"   # ⚠ must match the RUNNING nodes — see note above

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ORCH_LOCAL="$REPO/scripts/nas-ups-orchestrator.sh"
[[ -f "$ORCH_LOCAL" ]] || { echo "ERROR: $ORCH_LOCAL not found" >&2; exit 1; }

# Every (talosconfig, node) pair the orchestrator will try, as `config@ip`,
# read from the orchestrator's OWN inventory lines so there is no second copy
# to drift. Fails loudly if a list or config line is not where it expects.
check_pairs() {
  local v nodes cfg ip
  for v in PRD DEV MSA2_PRD MSA2_DEV; do
    nodes="$(sed -n "s/^${v}_NODES=\"\([0-9. ]*\)\"\$/\1/p" "$ORCH_LOCAL")"
    cfg="$(sed -n "s|^${v}_CFG=\\\$TALOS_DIR/\([a-z0-9-]*\.talosconfig\)\$|\1|p" "$ORCH_LOCAL")"
    if [[ -z "$nodes" || -z "$cfg" ]]; then
      echo "ERROR: cannot read ${v}_NODES / ${v}_CFG from $ORCH_LOCAL" >&2
      return 1
    fi
    for ip in $nodes; do printf '%s@%s\n' "$cfg" "$ip"; done
  done
}

# The post-staging check (kube-infra plan § Cutover inventory row 15, S1-10/S1-33):
# the STAGED binary + STAGED configs, as root on the NAS, against every address
# the orchestrator targets, plus the sha256 of the STAGED orchestrator — the
# credential checks alone cannot tell whether the NAS runs this checkout's
# script or an older one. One ssh, one sudo prompt (talosctl is not on
# truenas_admin's NOPASSWD allowlist, so it needs the TTY).
print_checks() {
  local pairs body want head
  pairs="$(check_pairs | tr '\n' ' ')"
  pairs="${pairs% }"
  [[ -n "$pairs" ]] || return 1
  want="$(shasum -a 256 "$ORCH_LOCAL" | awk '{print $1}')"
  head="$(git -C "$REPO" log -1 --format='%h %s' 2>/dev/null || echo 'not a git checkout')"
  if [[ -n "$(git -C "$REPO" status --porcelain -- scripts/nas-ups-orchestrator.sh 2>/dev/null)" ]]; then
    head="$head — ⚠ WITH UNCOMMITTED EDITS to nas-ups-orchestrator.sh"
  fi
  # Single-quoted: the \$ reach the NAS login shell, which turns them into $
  # for `bash -c`.
  # shellcheck disable=SC2016
  body='T=/mnt/tank/system/talos; sha256sum \$T/nas-ups-orchestrator.sh; \$T/talosctl version --client --short; for p in PAIRS; do c=\${p%@*}; n=\${p#*@}; echo; echo == \$c @ \$n; if [ -f \$T/\$c ]; then \$T/talosctl --talosconfig \$T/\$c -n \$n -e \$n version --short; else echo not staged; fi; done'
  body="${body/PAIRS/$pairs}"
  echo "  Authenticated check of every staged (config, node) pair — one sudo prompt:"
  echo
  printf "  ssh -t truenas_admin@%s 'sudo bash -c \"%s\"'\n" "${TRUENAS_HOST:-nas.w1.lv}" "$body"
  echo
  echo "  Expect first the staged orchestrator's sha256, which must be"
  echo "    ${want}"
  echo "  (this checkout: ${head})."
  echo "  A different hash means the NAS runs another version of the script,"
  echo "  whatever the checks below say."
  echo "  Then Client: ${TALOSCTL_VERSION}, then per pair ONE of:"
  echo "    - Server: v1.14.x — the address is that cluster's node and the config works"
  echo "      (this is also the Version pre-check an msa2 shutdown makes first);"
  echo "    - x509 / unknown authority — the address is ANOTHER cluster's node (before"
  echo "      the cutovers msa2-*@10.10.5.11 and @.12; after them prd@.11, prd@.12), or"
  echo "      that cluster's box is in Talos maintenance mode;"
  echo "    - a dial error / timeout — no box answers (msa2-prd until it is built, an"
  echo "      old node whose cord is pulled);"
  echo "    - 'not staged' — that msa2 cluster's Doppler key did not exist at staging."
  echo "  Read the result per box: every box that is up and installed must show"
  echo "  Server: for its OWN config at its CURRENT address — anything else there"
  echo "  means it would NOT be shut down. The orchestrator does not wait for an msa2"
  echo "  BUILD address (.17/.18) that fails BY DESIGN; but .11/.12 are also in the"
  echo "  Q170S1 prd list until prd's teardown, so an msa2 box there that fails is"
  echo "  also polled to the 300 s backstop."
}

if [[ "${1:-}" == "--print-checks" ]]; then
  print_checks
  exit $?
fi

: "${TRUENAS_HOST:?set TRUENAS_HOST (e.g. nas.w1.lv)}"
: "${TRUENAS_API_KEY:?set TRUENAS_API_KEY from Doppler infrastructure/ops}"
: "${TALOS_NAS_SHUTDOWN_CONFIG_DEV:?set TALOS_NAS_SHUTDOWN_CONFIG_DEV (base64) from Doppler infrastructure/ops}"
: "${TALOS_NAS_SHUTDOWN_CONFIG_PRD:?set TALOS_NAS_SHUTDOWN_CONFIG_PRD (base64) from Doppler infrastructure/ops}"

PY="$REPO/.venv/bin/python"
[[ -x "$PY" ]] || { echo "ERROR: $PY not found — run ./manage.sh once to build the venv" >&2; exit 1; }
check_pairs >/dev/null   # fail BEFORE uploading if the inventory cannot be read

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- 1. download + checksum-verify talosctl linux-amd64 (pinned) -----------
BASE="https://github.com/siderolabs/talos/releases/download/${TALOSCTL_VERSION}"
echo "==> downloading talosctl ${TALOSCTL_VERSION} (linux-amd64)"
curl -fsSL "$BASE/talosctl-linux-amd64" -o "$WORK/talosctl"
echo "==> verifying sha256 against the release sha256sum.txt"
if curl -fsSL "$BASE/sha256sum.txt" -o "$WORK/sha256sum.txt" 2>/dev/null; then
  EXPECTED="$(grep 'talosctl-linux-amd64$' "$WORK/sha256sum.txt" | awk '{print $1}' | head -1)"
  ACTUAL="$(shasum -a 256 "$WORK/talosctl" | awk '{print $1}')"
  if [[ -z "$EXPECTED" ]]; then
    echo "WARN: talosctl-linux-amd64 not found in sha256sum.txt — skipping verify" >&2
  elif [[ "$EXPECTED" != "$ACTUAL" ]]; then
    echo "ERROR: talosctl checksum mismatch (expected $EXPECTED got $ACTUAL)" >&2; exit 1
  else
    echo "    sha256 OK ($ACTUAL)"
  fi
else
  echo "WARN: could not fetch sha256sum.txt — proceeding WITHOUT checksum verify" >&2
fi
chmod 0755 "$WORK/talosctl"

# --- 2. materialize the scoped talosconfigs from Doppler (base64) -----------
# CFGS = the config files this run stages. Q170S1 always; each msa2 cluster only
# when its key exists (see the header).
CFGS="dev-shutdown.talosconfig prd-shutdown.talosconfig"
printf '%s' "$TALOS_NAS_SHUTDOWN_CONFIG_DEV" | base64 -d > "$WORK/dev-shutdown.talosconfig"
printf '%s' "$TALOS_NAS_SHUTDOWN_CONFIG_PRD" | base64 -d > "$WORK/prd-shutdown.talosconfig"
for cl in msa2-dev msa2-prd; do
  key="TALOS_NAS_SHUTDOWN_CONFIG_$(echo "$cl" | tr '[:lower:]-' '[:upper:]_')"
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}" | base64 -d > "$WORK/$cl-shutdown.talosconfig"
    CFGS="$CFGS $cl-shutdown.talosconfig"
    echo "==> $key set — staging $cl-shutdown.talosconfig"
  else
    echo "WARN: $key not set — $cl NOT staged; the orchestrator will skip $cl" >&2
  fi
done
for f in $CFGS; do
  grep -q 'context' "$WORK/$f" || { echo "ERROR: $f does not look like a talosconfig" >&2; exit 1; }
  chmod 0600 "$WORK/$f"
done

# --- 3. upload everything via the TrueNAS filesystem API (root) ------------
TRUENAS_VERIFY_SSL="${TRUENAS_VERIFY_SSL:-false}" \
REPO="$REPO" WORK="$WORK" ORCH_LOCAL="$ORCH_LOCAL" CFGS="$CFGS" \
"$PY" - <<'PY'
import os, sys, pathlib
_repo = os.environ.get("REPO")
if _repo:
    sys.path.insert(0, str(pathlib.Path(_repo) / "src"))
from truenas_infra.client import connected, upload_file  # noqa: E402

HOST = os.environ["TRUENAS_HOST"]
KEY  = os.environ["TRUENAS_API_KEY"]
VSSL = os.environ.get("TRUENAS_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
WORK = pathlib.Path(os.environ["WORK"])
ORCH = pathlib.Path(os.environ["ORCH_LOCAL"])
CFGS = os.environ["CFGS"].split()
DIR  = "/mnt/tank/system/talos"


def ensure_dir(cli, path):
    try:
        cli.call("filesystem.stat", path); return "exists"
    except Exception:
        pass
    last = None
    for arg in ({"path": path}, path):
        try:
            cli.call("filesystem.mkdir", arg); return "created"
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


print(f"==> connecting to {HOST}")
with connected(HOST, KEY, verify_ssl=VSSL) as cli:
    print(f"==> {DIR}: {ensure_dir(cli, DIR)}")
    plan = [
        # talosctl is ~90 MB — give the upload job a generous timeout
        (WORK / "talosctl",                  f"{DIR}/talosctl",                  0o755, 300.0),
        *[(WORK / c, f"{DIR}/{c}", 0o600, 60.0) for c in CFGS],
        # the orchestrator last, as before
        (ORCH,                               f"{DIR}/nas-ups-orchestrator.sh",   0o750, 60.0),
    ]
    for local, remote, mode, jt in plan:
        upload_file(cli, host=HOST, api_key=KEY, local_path=local,
                    remote_path=remote, mode=mode, verify_ssl=VSSL, job_timeout=jt)
        print(f"==> uploaded {remote} (mode {mode:o})")

    # also ensure the orchestrator's log dir exists (world-readable trace)
    print(f"==> /mnt/tank/system/nut: {ensure_dir(cli, '/mnt/tank/system/nut')}")
PY

echo
echo "✓ Staged talosctl + configs ($CFGS) + orchestrator on the NAS."
echo "  NEXT (read-only, no shutdown):"
print_checks
echo "  (ups.config.shutdowncmd already points at the orchestrator — config/services.yaml"
echo "   § nut.shutdowncmd; this script never changes it.)"
