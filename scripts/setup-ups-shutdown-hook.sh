#!/usr/bin/env bash
# setup-ups-shutdown-hook.sh — install the NAS UPS power-off hook (bug #57).
# Idempotent. Run from the operator's laptop.
#
# APPROACH (revised 2026-05-31 after the upsmon-SHUTDOWNCMD attempt failed):
# register the hook as a TrueNAS **Init/Shutdown Script** (when=SHUTDOWN) and
# CLEAR `ups.config.shutdowncmd` back to TrueNAS's default host-poweroff. The
# init/shutdown script runs during poweroff and only ARMS the UPS
# (`shutdown.return`); TrueNAS still owns the host shutdown. Community basis:
# NUT issue #2587 + the systemd `nutshutdown` hook — kill-power belongs at a
# shutdown-time hook, not at FSD/SHUTDOWNCMD time.
#
# All file placement + config goes through the TrueNAS API (filesystem.put,
# ups.update, initshutdownscript.*), NOT ssh+sudo — the NOPASSWD allowlist only
# covers midclt/upscmd/upsrw/docker, and the middleware API runs as root.
#
# Prereqs (Doppler infrastructure/ops; manage.sh exports the first two):
#   TRUENAS_HOST          e.g. nas.w1.lv (or 10.10.5.10)
#   TRUENAS_API_KEY       long-lived key
#   TRUENAS_NUT_ADMINPWD  upsadmin NUT password (baked into the root-only pw file)
#
# ⚠️  Wires a live shutdown path. VALIDATE with the LIGHT drill afterwards:
#     `ssh truenas_admin@HOST 'sudo upsmon -c fsd'` (no battery drain), then
#     read /mnt/tank/system/nut/last-shutdown.log. See
#     wiki/docs/runbooks/ups-operations.md § Drill A.
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
COMMENT = "UPS pre-halt power-off (bug #57)"
hook_local = pathlib.Path(os.environ["HOOK_LOCAL"])


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


def restart_ups(cli):
    # RESTART can leave the service STOPPED on TrueNAS 25.10 — STOP+START+verify.
    try:
        cli.call("service.control", "STOP", "ups")
    except Exception:  # noqa: BLE001
        pass
    time.sleep(2)
    cli.call("service.control", "START", "ups")
    time.sleep(5)
    return cli.call("service.query", [["service", "=", "ups"]])[0]["state"]


print(f"==> connecting to {HOST}")
with connected(HOST, KEY, verify_ssl=VSSL) as cli:
    # 1. Retire the failed upsmon-SHUTDOWNCMD approach FIRST so upsmon goes back
    #    to TrueNAS's own host-poweroff before we change the script file.
    cfg = cli.call("ups.config")
    if cfg.get("shutdowncmd"):
        cli.call("ups.update", {"shutdowncmd": ""})
        state = restart_ups(cli)
        print(f"==> [1/5] cleared ups.config.shutdowncmd (ups service: {state})")
        if state != "RUNNING":
            print("ERROR: ups service not RUNNING after clear+restart", file=sys.stderr)
            sys.exit(1)
    else:
        print("==> [1/5] ups.config.shutdowncmd already empty")

    # 2. Place the hook + root-only password file (as root, via the API).
    print(f"==> [2/5] {DIR}: {ensure_dir(cli, DIR)}")
    upload_file(cli, host=HOST, api_key=KEY, local_path=hook_local,
                remote_path=HOOK, mode=0o700, verify_ssl=VSSL)
    print(f"==> [3/5] uploaded hook -> {HOOK} (mode 700, root)")
    tf = tempfile.NamedTemporaryFile("w", delete=False); tf.write(PW); tf.close()
    try:
        os.chmod(tf.name, 0o600)
        upload_file(cli, host=HOST, api_key=KEY, local_path=pathlib.Path(tf.name),
                    remote_path=PWF, mode=0o600, verify_ssl=VSSL)
        print(f"==> [4/5] uploaded password file -> {PWF} (mode 600, root)")
    finally:
        os.unlink(tf.name)

    # 3. Register (or update) the SHUTDOWN init/shutdown script — persists in
    #    the config DB across reboots + TrueNAS updates.
    desired = {"type": "SCRIPT", "script": HOOK, "when": "SHUTDOWN",
               "enabled": True, "timeout": 20, "comment": COMMENT}
    existing = [s for s in cli.call("initshutdownscript.query")
                if s.get("comment") == COMMENT or s.get("script") == HOOK]
    if existing:
        cli.call("initshutdownscript.update", existing[0]["id"], desired)
        action = f"updated id={existing[0]['id']}"
    else:
        new = cli.call("initshutdownscript.create", desired)
        action = f"created id={new['id']}"
    print(f"==> [5/5] SHUTDOWN init/shutdown script {action}")

    # Verify
    cfg = cli.call("ups.config")
    scripts = [s for s in cli.call("initshutdownscript.query")
               if s.get("script") == HOOK and s.get("when") == "SHUTDOWN"]
    print()
    print(f"VERIFY ups.config.shutdowncmd = {cfg['shutdowncmd']!r} (should be '')")
    print(f"VERIFY SHUTDOWN script registered = {bool(scripts)} "
          f"(enabled={scripts[0]['enabled'] if scripts else 'n/a'})")

print()
print("✓ Installed. NEXT: light validation drill (no battery drain):")
print("    ssh truenas_admin@HOST 'sudo upsmon -c fsd'")
print("  then read /mnt/tank/system/nut/last-shutdown.log to see what fired.")
PY
