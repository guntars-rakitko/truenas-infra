#!/bin/bash
# tls-export.sh — copy TrueNAS's ACME-managed wildcard cert out to the
# pool so containers can read it via bind-mount.
#
# TrueNAS stores certs at /etc/certificates/<name>.{crt,key} on the boot
# overlay — not reachable from Docker bind-mounts (wrong UID mapping,
# wrong path discoverability). Container apps mount /mnt/tank/system/tls/
# read-only; this script keeps that directory in sync with whatever the
# TrueNAS ACME daemon has re-issued.
#
# Exit codes:
#   0  — no change (cert on pool already matches /etc/certificates)
#   10 — files were updated (caller should redeploy cert-consuming apps)
#   ≥1 — error (cert missing, permission denied, etc.)
#
# Idempotency is SHA-256-based (not timestamp) so manual pool touches
# don't trigger spurious rotates.
set -eu

CERT_NAME="${CERT_NAME:-w1-wildcard}"
SRC_DIR="${SRC_DIR:-/etc/certificates}"
DST_DIR="${DST_DIR:-/mnt/tank/system/tls}"

SRC_CRT="$SRC_DIR/$CERT_NAME.crt"
SRC_KEY="$SRC_DIR/$CERT_NAME.key"
DST_CRT="$DST_DIR/fullchain.pem"
DST_KEY="$DST_DIR/privkey.pem"

log() { printf '[%s] %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }

if [ ! -f "$SRC_CRT" ] || [ ! -f "$SRC_KEY" ]; then
    log "ERROR: source cert missing ($SRC_CRT or $SRC_KEY)"
    exit 1
fi

mkdir -p "$DST_DIR"

# Compare SHA-256 — if both match, noop.
src_crt_sha=$(sha256sum "$SRC_CRT" | cut -d' ' -f1)
src_key_sha=$(sha256sum "$SRC_KEY" | cut -d' ' -f1)
dst_crt_sha=$(sha256sum "$DST_CRT" 2>/dev/null | cut -d' ' -f1 || echo "")
dst_key_sha=$(sha256sum "$DST_KEY" 2>/dev/null | cut -d' ' -f1 || echo "")

if [ "$src_crt_sha" = "$dst_crt_sha" ] && [ "$src_key_sha" = "$dst_key_sha" ]; then
    log "cert on pool already matches /etc/certificates ($src_crt_sha — 8 chars: ${src_crt_sha:0:8}); no change"
    exit 0
fi

log "cert changed (src ${src_crt_sha:0:8} vs dst ${dst_crt_sha:0:8}); copying"

# Install with explicit perms. The private key is 0640 with group `docker`
# (present on TrueNAS Apps host) — readable by containerized processes
# running as the docker gid but not world-readable.
install -m 0644 "$SRC_CRT" "$DST_CRT"
# Some gid lookups fail during early boot; chgrp best-effort.
install -m 0640 "$SRC_KEY" "$DST_KEY"
chgrp docker "$DST_KEY" 2>/dev/null || log "warning: chgrp docker on privkey failed — check group exists"

log "exported cert ${CERT_NAME} → $DST_DIR"
exit 10
