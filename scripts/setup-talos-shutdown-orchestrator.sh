#!/usr/bin/env bash
# setup-talos-shutdown-orchestrator.sh — stage the Path B UPS orchestrator on
# the NAS (kube-infra #611). Idempotent. Run from the operator's laptop.
#
# Places these artifacts under /mnt/tank/system/talos/ via the TrueNAS API
# (filesystem.put — runs as root; NOT ssh+sudo):
#   1. talosctl                          (linux-amd64, pinned, mode 0755)
#   2. msa2-dev-shutdown.talosconfig     (msa2-dev os:operator, mode 0600)
#   3. msa2-prd-shutdown.talosconfig     (msa2-prd os:operator, mode 0600)
#   4. nas-ups-orchestrator.sh           (the SHUTDOWNCMD script, mode 0750)
#
# Both configs are REQUIRED since the Q170S1 teardown (kube-infra#1443): both
# MS-A2 clusters are built and are the only nodes on the UPS, so a missing key
# would stage an orchestrator that skips that box, and the box would be
# hard-cut when apc1 cuts power. (While msa2-prd was not built yet they were
# optional, and the Q170S1 pair `{dev,prd}-shutdown.talosconfig` was required:
# git history.) This script never DELETES a file on the NAS: after the
# teardown the old `{dev,prd}-shutdown.talosconfig` stay there unused until
# someone removes them by hand (truenas-infra CLAUDE.md § UPS / NUT). A STALE
# config is not harmless: its shutdown is rejected, so that box is not shut
# down at all (the orchestrator does not wait for it either, its rule (a), so
# the battery is spared, but Postgres is hard-cut). Re-run this script after
# every `bootstrap.sh <msa2-env>` menu 10 so the staged copies track Doppler,
# and run the printed check.
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
# (base64), so a NAS rebuild can restore them. kube-infra `talos-os/bootstrap.sh
# <cluster>` menu 10 mints them (`TALOS_NAS_SHUTDOWN_CONFIG_$(_doppler_suffix)`:
# msa2-dev → MSA2_DEV), os:operator, against that cluster's OWN CA, with
# `--crt-ttl 87600h` (ten years).
#
# ⚠ A COMMENT HERE ONCE SAID `--crt-ttl 720h` (30 DAYS), AND THAT WAS A LANDMINE.
# The credentials actually deployed never matched it (read from Doppler on
# 2026-09-22: valid about ten years), so the line sat here waiting for the next
# person who would follow it. Why that mattered: nothing rotates this credential
# (no scheduler, no job — unlike `render-cluster-agent-kubeconfigs.sh`, which
# prints remaining lifetime) and nothing ALERTS on it (verified: no
# `prometheus-rules-*.yaml` in kube-infra references it). A 30-day credential
# would have died silently after a month, and the failure surfaces ONLY during
# a real power cut — the one moment it cannot be discovered safely.
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
#
# Prereqs (Doppler infrastructure/ops; manage.sh exports the first two):
#   TRUENAS_HOST                        e.g. nas.w1.lv (or 10.10.5.10)
#   TRUENAS_API_KEY                     long-lived key
#   TALOS_NAS_SHUTDOWN_CONFIG_MSA2_DEV  base64 of the msa2-dev os:operator talosconfig
#   TALOS_NAS_SHUTDOWN_CONFIG_MSA2_PRD  base64 of the msa2-prd os:operator talosconfig
#
# Run it as (the orchestrator it uploads is whatever THIS checkout holds, so
# bring `main` up to date first — a stale checkout re-stages the old script and
# every credential check still passes; the printed check compares the staged
# script's sha256 with this checkout's):
#   cd ~/github/truenas-infra && git switch main && git pull --ff-only && git log -1 --oneline
#   doppler run -p infrastructure -c ops -- ./scripts/setup-talos-shutdown-orchestrator.sh
#
# Pin TALOSCTL_VERSION to the RUNNING cluster version: v1.14.2, both MS-A2
# boxes since 2026-10-10 (kube-infra talos-os/patches/node-msa2-*.yaml; v1.14.1 before). Same MINOR is the
# compatibility line this file relies on (a client newer than a server only
# WARNS — talosctl ClientVersionCheck); a full-minor gap was never verified
# here, and "nothing else would surface a break until a real power outage"
# (the orchestrator's header).
#
# ⚠ RE-STAGE THIS BINARY AFTER EVERY CLUSTER UPGRADE. Re-running this script is the
# mechanism; nothing does it automatically and nothing alerts on the skew. Verify
# read-only afterwards with the command `--print-checks` prints (needs root on
# the NAS — the configs are 0600).
set -euo pipefail

TALOSCTL_VERSION="${TALOSCTL_VERSION:-v1.14.2}"   # ⚠ must match the RUNNING nodes — see note above

REPO="$(cd "$(dirname "$0")/.." && pwd)"
ORCH_LOCAL="$REPO/scripts/nas-ups-orchestrator.sh"
[[ -f "$ORCH_LOCAL" ]] || { echo "ERROR: $ORCH_LOCAL not found" >&2; exit 1; }

# Every (talosconfig, node) pair the orchestrator will try, as `config@ip`,
# read from the orchestrator's OWN inventory lines so there is no second copy
# to drift. Fails loudly if a list or config line is not where it expects.
check_pairs() {
  local v nodes cfg ip
  for v in MSA2_PRD MSA2_DEV; do
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
  echo "    - Server: v1.14.x — the box answers and its config works (this is also"
  echo "      the Version pre-check its shutdown makes first);"
  echo "    - x509 / unknown authority — the box's PKI is not the one the staged config"
  echo "      was minted against (a rebuild with NEW_PKI=1 and no re-stage since), or"
  echo "      the box is in Talos maintenance mode;"
  echo "    - a dial error / timeout — no box answers at that address;"
  echo "    - 'not staged' — that cluster's config is not on the NAS."
  echo "  Every box that is up and installed must show Server: — anything else there"
  echo "  means it would NOT be shut down. The orchestrator does not wait for an"
  echo "  address that fails, BY DESIGN (rule (a) in its header), so a failure here"
  echo "  costs that box a hard power-off, not the NAS its battery."
}

if [[ "${1:-}" == "--print-checks" ]]; then
  print_checks
  exit $?
fi

: "${TRUENAS_HOST:?set TRUENAS_HOST (e.g. nas.w1.lv)}"
: "${TRUENAS_API_KEY:?set TRUENAS_API_KEY from Doppler infrastructure/ops}"
: "${TALOS_NAS_SHUTDOWN_CONFIG_MSA2_DEV:?set TALOS_NAS_SHUTDOWN_CONFIG_MSA2_DEV (base64) from Doppler infrastructure/ops — kube-infra bootstrap.sh msa2-dev menu 10 mints it}"
: "${TALOS_NAS_SHUTDOWN_CONFIG_MSA2_PRD:?set TALOS_NAS_SHUTDOWN_CONFIG_MSA2_PRD (base64) from Doppler infrastructure/ops — kube-infra bootstrap.sh msa2-prd menu 10 mints it}"

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
# CFGS = the config files this run stages: one per cluster, both required (see
# the header; the `:?` checks above stop the run before this point).
CFGS="msa2-dev-shutdown.talosconfig msa2-prd-shutdown.talosconfig"
printf '%s' "$TALOS_NAS_SHUTDOWN_CONFIG_MSA2_DEV" | base64 -d > "$WORK/msa2-dev-shutdown.talosconfig"
printf '%s' "$TALOS_NAS_SHUTDOWN_CONFIG_MSA2_PRD" | base64 -d > "$WORK/msa2-prd-shutdown.talosconfig"
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
