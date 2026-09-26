#!/bin/bash
# tls-rotate.sh — the hourly `tls-rotate` cronjob (registered by
# modules/apps.py::ensure_tls_rotate). Runs tls-export.sh; when the cert
# changed, `midclt call app.redeploy`s every app in TLS_CONSUMERS, i.e. every
# app that serves the cert and does NOT pick up a new one on its own.
#
# Every app that serves the wildcard bind-mounts the whole /mnt/tank/system/tls
# DIRECTORY read-only, so the new files are visible inside each container as
# soon as tls-export.sh installs them. Whether the PROCESS re-reads them is
# what decides who needs a redeploy. Measured 2026-09-14: the renewal was
# exported at 04:00 UTC and this script redeployed nothing (the `$?` bug
# below), so each app showed what it does on its own:
#   - traefik: does NOT re-read. Its file provider watches only
#     --providers.file.directory (/etc/traefik/dynamic,
#     apps/traefik/docker-compose.yaml). The cert is mounted at
#     /etc/traefik/certs and referenced by `certFile` in
#     apps/traefik/routes.yaml, a path nothing watches. It kept serving the
#     pre-renewal cert on wiki.w1.lv for nine days, until the 2026-09-23
#     pool-rebuild restart reloaded it by accident (BlackboxCertExpiringWarn,
#     kube-infra #1252 / #1253). -> REDEPLOYED here.
#   - minio-prd / minio-dev: DO re-read. Both s3-{prd,dev}.w1.lv:9000 served
#     the new cert on the 04:00:08 UTC blackbox sample, seconds after the
#     04:00 export, with probe_success=1 on every 30 s sample (no restart).
#     -> NOT redeployed. Each redeploy would be a ~30 s S3 outage for every
#     cluster backup track (CNPG WAL, etcd snapshots, Litestream), for nothing.
#     They were in this list until 2026-09-26, but the script never got as far
#     as redeploying anything, so MinIO has never actually been redeployed on a
#     rotation. Leaving them out keeps the behaviour MinIO has always had.
# tests/test_tls_rotate.py requires every enabled app that mounts
# /mnt/tank/system/tls to be either in TLS_CONSUMERS or on its exemption
# list with this evidence. If a MinIO endpoint ever serves an old cert after a
# renewal, see docs/tls-runbook.md § Cert renewed but an endpoint still serves
# the old one.
# A single-FILE bind mount would behave differently: `install` replaces the
# file with a new inode and the container keeps the old one. Keep these as
# directory mounts.
#
# Exit codes: 0 = nothing to do, or every redeploy triggered; 2 = tls-export.sh
# not executable; 3 = a redeploy failed (still pending, retried next run); any
# other = tls-export.sh's own error code, passed through.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXPORT_SCRIPT="${EXPORT_SCRIPT:-$SCRIPT_DIR/tls-export.sh}"
# Apps that need app.redeploy on cert rotation (names as in config/apps.yaml).
# Only those that do NOT re-read the cert themselves; see the header.
TLS_CONSUMERS="${TLS_CONSUMERS:-traefik}"
# Apps whose redeploy has not succeeded yet. It lives on the pool next to this
# script, so it survives reboots. MinIO's certs dir is this directory and MinIO
# ignores regular files there, as it already does for the scripts and the log.
PENDING_FILE="${PENDING_FILE:-$SCRIPT_DIR/.tls-redeploy-pending}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

if [ ! -x "$EXPORT_SCRIPT" ]; then
    log "ERROR: $EXPORT_SCRIPT not executable"
    exit 2
fi

# Run export and capture its exit code (0 = no change, 10 = changed).
#
# ⚠ THIS LINE HAS BEEN WRONG TWICE, AND BOTH TIMES NO REDEPLOY EVER RAN.
#   1. Until 2026-09-23 it read `if "$EXPORT_SCRIPT"; then ... exit 0; fi` and
#      then `rc=$?`. After a completed `if`, `$?` is the IF COMPOUND's status,
#      which is always 0. A rotation therefore logged "tls-export.sh failed with
#      exit=0" and returned without redeploying.
#   2. e3d5663 (2026-09-23) replaced that with a bare `"$EXPORT_SCRIPT"` and
#      `rc=$?` on the next line. Under `set -e` a bare command that exits 10
#      makes the SHELL exit 10 on that line. The rotation path still died before
#      any redeploy, silently this time.
# `|| rc=$?` is the form that survives `set -e` and keeps the real status.
# tests/test_tls_rotate.py fails against both earlier versions.
rc=0
"$EXPORT_SCRIPT" || rc=$?

if [ "$rc" -eq 10 ]; then
    log "cert rotated — redeploying TLS consumers: $TLS_CONSUMERS"
    pending="$TLS_CONSUMERS"
    # Record the list BEFORE the first redeploy, so a crash part-way through is
    # retried too. Failing to record it must never cost the redeploy itself.
    # shellcheck disable=SC2086  # word-split on purpose: one app per line
    if ! printf '%s\n' $pending > "$PENDING_FILE"; then
        log "WARNING: cannot write $PENDING_FILE — a failed redeploy will NOT be retried"
    fi
elif [ "$rc" -ne 0 ]; then
    log "tls-export.sh failed with exit=$rc — not redeploying"
    exit "$rc"
elif [ -s "$PENDING_FILE" ]; then
    # tls-export.sh reports a change exactly ONCE. On the next run the files
    # already match and it exits 0, so a redeploy that failed last time would
    # otherwise be forgotten, and that app would serve the old cert until it
    # expired.
    pending="$(tr '\n' ' ' < "$PENDING_FILE")"
    log "no cert change; retrying redeploys still pending: $pending"
else
    log "no cert change; no redeploys needed"
    exit 0
fi

failed=""
for app in $pending; do
    log "  redeploying $app..."
    if midclt call app.redeploy "$app" >/dev/null; then
        log "  $app: redeploy triggered"
    else
        log "  $app: redeploy FAILED (app may not exist)"
        failed="$failed $app"
    fi
done

if [ -n "$failed" ]; then
    # shellcheck disable=SC2086
    printf '%s\n' $failed > "$PENDING_FILE" 2>/dev/null || true
    log "redeploy FAILED for:$failed — pending, retried next run"
    exit 3
fi
rm -f "$PENDING_FILE"
log "done"
exit 0
