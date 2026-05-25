#!/usr/bin/env bash
# setup-minio-encryption.sh — enable SSE-S3 default encryption on every
# backup bucket of both MinIO AIStor instances. Idempotent: re-setting
# an already-encrypted bucket is a no-op.
#
# Why this exists: GDPR at-rest encryption (kube-infra #520 Workstream
# C). With SSE-S3 enabled, every object written to a bucket is
# encrypted server-side before it hits the ZFS pool — Velero / Longhorn
# / MSSQL / etcd backups all become ciphertext at rest, transparently.
# Article 34 breach-notification safe harbor for the backup substrate.
#
# ─── PREREQUISITE — KMS must be configured on the AIStor server ──────────────
# SSE-S3 on AIStor requires a KMS. We use the simple single-root-key
# mode (no KES sidecar). BEFORE running this script:
#
#   1. Generate a root key + store in Doppler infrastructure/ops:
#        doppler secrets set "MINIO_KMS_SECRET_KEY_PRD=kube-backup-prd:$(openssl rand -base64 32)" \
#          --project infrastructure --config ops
#        doppler secrets set "MINIO_KMS_SECRET_KEY_DEV=kube-backup-dev:$(openssl rand -base64 32)" \
#          --project infrastructure --config ops
#      (format is `<key-name>:<base64-32-bytes>`)
#   2. Add `MINIO_KMS_SECRET_KEY` to apps/minio-{prd,dev}/docker-compose.yaml
#      `environment:` and register it in `_DOPPLER_KEYS_PER_APP`
#      (src/truenas_infra/modules/apps.py) → MINIO_KMS_SECRET_KEY_{PRD,DEV}.
#   3. Redeploy: `./manage.sh phase apps --apply`
#   4. Mirror the 2 root keys into Apple Passwords — losing a KMS root
#      key makes every SSE-encrypted object unrecoverable.
#
# This script verifies KMS is live before touching any bucket and
# SKIPs cleanly if it isn't — so a premature run is harmless.
#
# Prereqs (same as the other setup-minio-*.sh scripts):
#   - mc installed locally; `nas-prd` / `nas-dev` aliases configured
#   - buckets already created (run setup-minio-buckets.sh first)
#
# Verification: `mc encrypt info nas-{dev,prd}/<bucket>` → SSE-S3.

set -euo pipefail

ALIASES=(nas-dev nas-prd)
BUCKETS=(
    cluster-agent
    etcd-snapshots
    loki-chunks
    longhorn
    mssql-backups
    pocket-id-litestream
    velero
)

for alias in "${ALIASES[@]}"; do
    if ! mc ls "$alias" >/dev/null 2>&1; then
        echo "SKIP  $alias (alias unreachable — set up \`mc alias set\` first)"
        continue
    fi

    # KMS must be live, else `mc encrypt set sse-s3` fails. Check once
    # per alias; skip the whole alias cleanly if KMS isn't configured.
    if ! mc admin kms key status "$alias" >/dev/null 2>&1; then
        echo "SKIP  $alias (KMS not configured — see PREREQUISITE in this script's header)"
        continue
    fi

    for bucket in "${BUCKETS[@]}"; do
        if ! mc ls "$alias/$bucket" >/dev/null 2>&1; then
            echo "SKIP  $alias/$bucket (bucket missing — run setup-minio-buckets.sh)"
            continue
        fi
        if mc encrypt info "$alias/$bucket" 2>/dev/null | grep -qi 'sse-s3'; then
            echo "OK    $alias/$bucket (SSE-S3 already enabled)"
        else
            mc encrypt set sse-s3 "$alias/$bucket" >/dev/null
            echo "OK    $alias/$bucket (SSE-S3 enabled)"
        fi
    done
done

echo
echo "Verify: mc encrypt info nas-{dev,prd}/<bucket>"
echo "Note:   SSE-S3 encrypts NEW objects only — pre-existing objects"
echo "        stay plaintext and age out via lifecycle / controller TTL."
