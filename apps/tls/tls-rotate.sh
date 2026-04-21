#!/bin/bash
# tls-rotate.sh — runs tls-export.sh hourly; on cert change, redeploys
# cert-consuming apps so their bind-mount picks up the new inode.
#
# Why apps need a redeploy on rotation: Docker bind-mounts are inode-
# bound. When `install` rewrites /mnt/tank/system/tls/{fullchain,privkey}
# the file gets a new inode; the mount inside the running container still
# points at the OLD inode. `midclt call app.redeploy <name>` recreates
# the container with a fresh mount.
#
# Exempt: Traefik (hot-reloads its cert file automatically — no redeploy
# needed). Listed apps here are the ones that hold cert bytes in-memory
# at startup (MinIO).
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXPORT_SCRIPT="${EXPORT_SCRIPT:-$SCRIPT_DIR/tls-export.sh}"
# Apps that need app.redeploy on cert rotation. Traefik is NOT here —
# it watches the cert file and reloads automatically.
TLS_CONSUMERS="${TLS_CONSUMERS:-minio-prd minio-dev}"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

if [ ! -x "$EXPORT_SCRIPT" ]; then
    log "ERROR: $EXPORT_SCRIPT not executable"
    exit 2
fi

# Run export — capture exit code (0=noop, 10=changed).
if "$EXPORT_SCRIPT"; then
    log "no cert change; no redeploys needed"
    exit 0
fi
# Exit was non-zero. Only 10 means "updated"; anything else is a real error.
rc=$?
if [ "$rc" -ne 10 ]; then
    log "tls-export.sh failed with exit=$rc — not redeploying"
    exit "$rc"
fi

log "cert rotated — redeploying TLS consumers: $TLS_CONSUMERS"
for app in $TLS_CONSUMERS; do
    log "  redeploying $app..."
    if midclt call app.redeploy "$app" >/dev/null; then
        log "  $app: redeploy triggered"
    else
        log "  $app: redeploy FAILED (app may not exist)"
    fi
done
log "done"
exit 0
