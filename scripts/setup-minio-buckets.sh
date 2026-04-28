#!/usr/bin/env bash
# setup-minio-buckets.sh — create the canonical backup buckets on both
# MinIO instances. Idempotent: re-running with the same buckets is a
# no-op. Run after a fresh MinIO bootstrap (or after editing this file).
#
# Why this exists: bucket creation was a manual `mc mb` step until
# 2026-04-29. A fresh NAS rebuild would silently leave Velero +
# Longhorn + SQL backup CronJobs failing with "bucket missing".
#
# Prereqs:
#   - mc installed locally (`brew install minio-mc`)
#   - `mc alias set nas-prd https://s3-prd.w1.lv:9000 ...` already done
#     (with the MINIO_ROOT credentials from apps/minio-prd/secrets.sops.yaml)
#   - `mc alias set nas-dev https://s3-dev.w1.lv:9000 ...` already done
#
# Verification: `mc ls nas-{dev,prd}` shows all four buckets.

set -euo pipefail

# ─── Desired state ───────────────────────────────────────────────────────────
# One bucket per backup track per cluster. Same names on both clusters
# (separation is at the alias level, not the bucket name).
ALIASES=(nas-dev nas-prd)
BUCKETS=(
    velero          # Velero — K8s manifest backups
    longhorn        # Longhorn — volume + system backups
    mssql-backups   # SQL Server — BACKUP DATABASE TO URL targets
    etcd-snapshots  # CronJob — talosctl etcd snapshot
)

# ─── Apply ───────────────────────────────────────────────────────────────────
for alias in "${ALIASES[@]}"; do
    if ! mc ls "$alias" >/dev/null 2>&1; then
        echo "SKIP  $alias (alias unreachable — set up `mc alias set` first)"
        continue
    fi

    for bucket in "${BUCKETS[@]}"; do
        if mc ls "$alias/$bucket" >/dev/null 2>&1; then
            echo "OK    $alias/$bucket (already exists)"
        else
            mc mb "$alias/$bucket" >/dev/null
            echo "OK    $alias/$bucket (created)"
        fi
    done
done

echo
echo "Verify: mc ls <alias>"
echo "Next:   ./scripts/setup-minio-users.sh   (provision service user)"
echo "        ./scripts/setup-minio-lifecycle.sh (apply ILM rules)"
