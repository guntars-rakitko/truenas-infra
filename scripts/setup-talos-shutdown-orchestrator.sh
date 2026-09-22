#!/usr/bin/env bash
# setup-talos-shutdown-orchestrator.sh — stage the Path B UPS orchestrator on
# the NAS (kube-infra #611). Idempotent. Run from the operator's laptop.
#
# Places three artifacts under /mnt/tank/system/talos/ via the TrueNAS API
# (filesystem.put — runs as root; NOT ssh+sudo):
#   1. talosctl                       (linux-amd64, pinned, mode 0755)
#   2. dev-shutdown.talosconfig       (os:operator-scoped, mode 0600)
#   3. prd-shutdown.talosconfig       (os:operator-scoped, mode 0600)
#   4. nas-ups-orchestrator.sh        (the SHUTDOWNCMD script, mode 0750)
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
#
# Pin TALOSCTL_VERSION to the RUNNING cluster version.
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

: "${TRUENAS_HOST:?set TRUENAS_HOST (e.g. nas.w1.lv)}"
: "${TRUENAS_API_KEY:?set TRUENAS_API_KEY from Doppler infrastructure/ops}"
: "${TALOS_NAS_SHUTDOWN_CONFIG_DEV:?set TALOS_NAS_SHUTDOWN_CONFIG_DEV (base64) from Doppler infrastructure/ops}"
: "${TALOS_NAS_SHUTDOWN_CONFIG_PRD:?set TALOS_NAS_SHUTDOWN_CONFIG_PRD (base64) from Doppler infrastructure/ops}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
ORCH_LOCAL="$REPO/scripts/nas-ups-orchestrator.sh"
[[ -x "$PY" ]] || { echo "ERROR: $PY not found — run ./manage.sh once to build the venv" >&2; exit 1; }
[[ -f "$ORCH_LOCAL" ]] || { echo "ERROR: $ORCH_LOCAL not found" >&2; exit 1; }

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

# --- 2. materialize the two scoped talosconfigs from Doppler (base64) ------
printf '%s' "$TALOS_NAS_SHUTDOWN_CONFIG_DEV" | base64 -d > "$WORK/dev-shutdown.talosconfig"
printf '%s' "$TALOS_NAS_SHUTDOWN_CONFIG_PRD" | base64 -d > "$WORK/prd-shutdown.talosconfig"
for f in dev-shutdown.talosconfig prd-shutdown.talosconfig; do
  grep -q 'context' "$WORK/$f" || { echo "ERROR: $f does not look like a talosconfig" >&2; exit 1; }
done
chmod 0600 "$WORK/dev-shutdown.talosconfig" "$WORK/prd-shutdown.talosconfig"

# --- 3. upload everything via the TrueNAS filesystem API (root) ------------
TRUENAS_VERIFY_SSL="${TRUENAS_VERIFY_SSL:-false}" \
REPO="$REPO" WORK="$WORK" ORCH_LOCAL="$ORCH_LOCAL" \
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
        (WORK / "dev-shutdown.talosconfig",  f"{DIR}/dev-shutdown.talosconfig",  0o600, 60.0),
        (WORK / "prd-shutdown.talosconfig",  f"{DIR}/prd-shutdown.talosconfig",  0o600, 60.0),
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
echo "✓ Staged talosctl + scoped configs + orchestrator on the NAS."
echo "  NEXT (read-only smoke test, no shutdown):"
echo "    ssh truenas_admin@${TRUENAS_HOST} \\"
echo "      'sudo /mnt/tank/system/talos/talosctl --talosconfig /mnt/tank/system/talos/dev-shutdown.talosconfig \\"
echo "        --nodes 10.10.5.14 --endpoints 10.10.5.14 version'"
echo "  THEN (maintenance-window Task 5) wire ups.config.shutdowncmd to the orchestrator."
