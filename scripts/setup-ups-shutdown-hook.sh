#!/usr/bin/env bash
# setup-ups-shutdown-hook.sh — install the NAS pre-halt UPS power-off hook.
# Idempotent: re-running re-uploads the script + refreshes the password file
# + re-asserts the shutdowncmd field. Run from the operator's laptop.
#
# Why this exists (bug #57): the UPS reaches the NAS over a DB-9 → RS-232 →
# USB adapter (/dev/ttyUSB0). TrueNAS's default `powerdown` killpower runs too
# late — after the kernel tears down the USB device — so `shutdown.return`
# never reaches the UPS and it keeps draining on a real outage. This installs
# scripts/nas-ups-shutdown.sh as upsmon's SHUTDOWNCMD so the power-off is
# armed up-front, while USB comms are still alive.
#
# FILE PLACEMENT GOES THROUGH THE TrueNAS API (filesystem.put), NOT ssh+sudo:
# the NOPASSWD sudo allowlist only covers midclt/upscmd/upsrw/docker — `sudo
# tee`/`mkdir`/`chmod` would prompt for a password and fail non-interactively.
# The middleware API runs as root, so files land root-owned with the right mode.
#
# Prereqs (all from Doppler infrastructure/ops; manage.sh exports them):
#   - TRUENAS_HOST         e.g. nas.w1.lv (or 10.10.5.10)
#   - TRUENAS_API_KEY      long-lived key
#   - TRUENAS_NUT_ADMINPWD upsadmin NUT password (baked into the root-only
#                          pw file the hook reads when Doppler is unreachable)
#
# ⚠️  This wires the live shutdown path. VALIDATE with Drill A immediately
#     after (whole-rack maintenance window) — see
#     wiki/docs/runbooks/ups-operations.md § Drill A. Until validated, leave
#     `powerdown: true` as the (failing-but-harmless) backup.
set -euo pipefail

: "${TRUENAS_HOST:?set TRUENAS_HOST (e.g. nas.w1.lv)}"
: "${TRUENAS_API_KEY:?set TRUENAS_API_KEY from Doppler infrastructure/ops}"
: "${TRUENAS_NUT_ADMINPWD:?set TRUENAS_NUT_ADMINPWD from Doppler infrastructure/ops}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
HOOK_LOCAL="$REPO/scripts/nas-ups-shutdown.sh"
[[ -x "$PY" ]] || { echo "ERROR: $PY not found — run ./manage.sh once to build the venv" >&2; exit 1; }
[[ -f "$HOOK_LOCAL" ]] || { echo "ERROR: $HOOK_LOCAL not found" >&2; exit 1; }

TRUENAS_VERIFY_SSL="${TRUENAS_VERIFY_SSL:-false}" \
HOOK_LOCAL="$HOOK_LOCAL" \
REPO="$REPO" \
"$PY" - <<'PY'
import os, sys, time, tempfile, pathlib
# truenas_infra is editable-installed in this repo's venv; add src as a
# safe fallback in case the venv was built without the editable install.
_repo = os.environ.get("REPO")
if _repo:
    sys.path.insert(0, str(pathlib.Path(_repo) / "src"))
from truenas_infra.client import connected, upload_file  # noqa: E402

HOST = os.environ["TRUENAS_HOST"]
KEY  = os.environ["TRUENAS_API_KEY"]
PW   = os.environ["TRUENAS_NUT_ADMINPWD"]
VSSL = os.environ.get("TRUENAS_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
DIR  = "/mnt/tank/system/nut"
HOOK = f"{DIR}/nas-ups-shutdown.sh"
PWF  = f"{DIR}/.upsadmin-pw"
hook_local = pathlib.Path(os.environ["HOOK_LOCAL"])


def ensure_dir(cli, path):
    try:
        cli.call("filesystem.stat", path)
        return "exists"
    except Exception:
        pass
    last = None
    for arg in ({"path": path}, path):
        try:
            cli.call("filesystem.mkdir", arg)
            return "created"
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


print(f"==> connecting to {HOST}")
with connected(HOST, KEY, verify_ssl=VSSL) as cli:
    print(f"==> [1/4] {DIR}: {ensure_dir(cli, DIR)}")
    upload_file(cli, host=HOST, api_key=KEY, local_path=hook_local,
                remote_path=HOOK, mode=0o700, verify_ssl=VSSL)
    print(f"==> [2/4] uploaded hook -> {HOOK} (mode 700, root)")
    tf = tempfile.NamedTemporaryFile("w", delete=False)
    tf.write(PW)
    tf.close()
    try:
        os.chmod(tf.name, 0o600)
        upload_file(cli, host=HOST, api_key=KEY, local_path=pathlib.Path(tf.name),
                    remote_path=PWF, mode=0o600, verify_ssl=VSSL)
        print(f"==> [3/4] uploaded password file -> {PWF} (mode 600, root)")
    finally:
        os.unlink(tf.name)
    cli.call("ups.update", {"shutdowncmd": HOOK})
    # RESTART can leave the ups service STOPPED on TrueNAS 25.10 — STOP+START+verify.
    try:
        cli.call("service.control", "STOP", "ups")
    except Exception as e:  # noqa: BLE001
        print("    (stop note:", e, ")")
    time.sleep(2)
    cli.call("service.control", "START", "ups")
    time.sleep(5)
    cfg = cli.call("ups.config")
    svc = cli.call("service.query", [["service", "=", "ups"]])[0]
    print(f"==> [4/4] shutdowncmd = {cfg['shutdowncmd']!r}")
    print(f"    ups service state = {svc['state']} (enable={svc['enable']})")
    if svc["state"] != "RUNNING":
        print("ERROR: ups service not RUNNING — START it and check 'upsc apc1@localhost'", file=sys.stderr)
        sys.exit(1)

print()
print("✓ Installed. NEXT: validate end-to-end with Drill A (whole-rack window).")
print("  Until validated, the default powerdown killpower stays as backup.")
PY
