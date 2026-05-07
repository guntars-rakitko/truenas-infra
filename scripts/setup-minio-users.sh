#!/usr/bin/env bash
# setup-minio-users.sh — provision the service user that K8s clusters
# use to read/write backup buckets. Idempotent.
#
# Each cluster has ONE service user shared across all backup tracks
# (Velero, Longhorn, MSSQL, etcd-snapshots) — global `readwrite`
# policy. Per-track scoping is by bucket name and IAM-policy granularity
# isn't worth the operational overhead for a homelab.
#
# Source of truth for the credentials: Doppler `infrastructure/{dev,prd}`
# `KUBE_MINIO_ACCESS_KEY_ID` + `KUBE_MINIO_SECRET_ACCESS_KEY`. The same
# credentials are consumed by Velero, Longhorn, and MSSQL backup
# CronJobs in-cluster (via DopplerSecret CRDs). Single canonical copy.
#
# Pre-Doppler this read sibling kube-infra SOPS files via `sops decrypt`.
# Migrated 2026-05-07 (kube-infra #92 follow-up — zero SOPS in any
# infra repo). The cross-repo `KUBE_INFRA` checkout dependency is also
# gone.
#
# Prereqs:
#   - mc installed and aliases set up (see setup-minio-buckets.sh)
#   - doppler CLI installed + signed in (`doppler login`)
#
# Verification:
#   mc admin user info nas-{dev,prd} <access-key>
#   mc admin policy entities nas-{dev,prd}     (user appears under readwrite)

set -euo pipefail

# ─── Desired state ───────────────────────────────────────────────────────────
# alias → Doppler config name (under project `infrastructure`).
declare -A CRED_CONFIGS=(
    [nas-dev]="dev"
    [nas-prd]="prd"
)

# ─── Pre-flight ──────────────────────────────────────────────────────────────
if ! command -v doppler >/dev/null 2>&1; then
    echo "ERROR: doppler CLI not installed (brew install dopplerhq/cli/doppler)" >&2
    exit 1
fi
if ! doppler whoami >/dev/null 2>&1; then
    echo "ERROR: doppler CLI not signed in. Run 'doppler login' first." >&2
    exit 1
fi

# ─── Apply ───────────────────────────────────────────────────────────────────
for alias in "${!CRED_CONFIGS[@]}"; do
    cfg="${CRED_CONFIGS[$alias]}"

    if ! mc ls "$alias" >/dev/null 2>&1; then
        echo "SKIP  $alias (alias unreachable)"
        continue
    fi

    ak=$(doppler secrets get KUBE_MINIO_ACCESS_KEY_ID \
            --project infrastructure --config "$cfg" --plain 2>/dev/null) || {
        echo "SKIP  $alias (Doppler get KUBE_MINIO_ACCESS_KEY_ID --config $cfg failed)"
        continue
    }
    sk=$(doppler secrets get KUBE_MINIO_SECRET_ACCESS_KEY \
            --project infrastructure --config "$cfg" --plain 2>/dev/null) || {
        echo "SKIP  $alias (Doppler get KUBE_MINIO_SECRET_ACCESS_KEY --config $cfg failed)"
        continue
    }

    if [[ -z "$ak" || -z "$sk" ]]; then
        echo "SKIP  $alias (Doppler returned empty access/secret key — check infrastructure/$cfg)"
        continue
    fi

    # `mc admin user add` is idempotent — re-running with the same key
    # is treated as an update (no error, no state change if key
    # unchanged). Policy attach is similarly idempotent.
    mc admin user add "$alias" "$ak" "$sk" >/dev/null
    mc admin policy attach "$alias" readwrite --user "$ak" >/dev/null 2>&1 || true
    echo "OK    $alias — service user $ak (readwrite)"
    unset ak sk
done

echo
echo "Verify: mc admin user list <alias>"
echo "        mc admin policy entities <alias>"
