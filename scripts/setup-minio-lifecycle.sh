#!/usr/bin/env bash
# setup-minio-lifecycle.sh — apply desired ILM rules to MinIO buckets.
#
# Idempotent: re-running with the same rules is a no-op (mc replaces the
# bucket's lifecycle config wholesale). Run after a MinIO instance is
# (re-)bootstrapped, or after editing this file.
#
# Why a script and not a `truenas-infra phase`: the TrueNAS API doesn't
# manage MinIO bucket internals — those live inside the MinIO container.
# We drive `mc` via the operator's pre-configured aliases (`nas-prd` /
# `nas-dev`, set up once per laptop). If/when more buckets need
# lifecycle, this grows into a `minio-buckets` phase.
#
# Prereqs:
#   - mc installed locally (`brew install minio-mc`)
#   - `mc alias set nas-prd https://s3-prd.w1.lv:9000 ...` already done
#   - `mc alias set nas-dev https://s3-dev.w1.lv:9000 ...` already done
#
# Verification: `mc ilm rule list nas-{dev,prd}/<bucket>` shows the rule.

set -euo pipefail

# ─── Desired state ───────────────────────────────────────────────────────────
# Each row: <alias>/<bucket> <expire-days>
# Other backup tracks (velero/longhorn/etcd-snapshots) intentionally
# omitted today — Velero and Longhorn manage their own retention via
# their controllers, and etcd-snapshots is hand-curated for now.
RULES=(
    "nas-dev/mssql-backups 90"
    "nas-prd/mssql-backups 90"
)

# ─── Apply ───────────────────────────────────────────────────────────────────
for row in "${RULES[@]}"; do
    target="${row% *}"
    days="${row##* }"

    if ! mc ls "$target" >/dev/null 2>&1; then
        echo "SKIP  $target (bucket missing or alias unreachable)"
        continue
    fi

    # mc ilm rule add appends a new rule each call — clear first to stay
    # idempotent. `mc ilm rule remove` with no ID removes ALL rules.
    mc ilm rule remove --all --force "$target" >/dev/null 2>&1 || true
    mc ilm rule add --expire-days "$days" "$target" >/dev/null
    echo "OK    $target — expire after ${days}d"
done

echo
echo "Verify: mc ilm rule list <alias>/<bucket>"
