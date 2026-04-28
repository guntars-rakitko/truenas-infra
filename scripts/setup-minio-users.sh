#!/usr/bin/env bash
# setup-minio-users.sh — provision the service user that K8s clusters
# use to read/write backup buckets. Idempotent.
#
# Each cluster has ONE service user shared across all backup tracks
# (Velero, Longhorn, MSSQL, etcd-snapshots) — global `readwrite`
# policy. Per-track scoping is by bucket name and IAM-policy granularity
# isn't worth the operational overhead for a homelab.
#
# Source of truth for the credentials: kube-infra SOPS files
# (the cluster has to read them to USE the user; this script READS
# them to PROVISION the user — single canonical copy, no drift).
#
# Cross-repo dependency: assumes `kube-infra` is checked out as a
# sibling at ../kube-infra (matches the layout described in
# kube-infra/CLAUDE.md "Local clones live at ~/Documents/github/...").
#
# Prereqs:
#   - mc installed and aliases set up (see setup-minio-buckets.sh)
#   - sops + age key configured (`SOPS_AGE_KEY_FILE` or default location)
#   - kube-infra repo at ../kube-infra
#
# Verification:
#   mc admin user info nas-{dev,prd} <access-key>
#   mc admin policy entities nas-{dev,prd}     (user appears under readwrite)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KUBE_INFRA="$(cd "$SCRIPT_DIR/../kube-infra" 2>/dev/null && pwd)" || {
    echo "ERROR: kube-infra repo not found at $SCRIPT_DIR/../kube-infra" >&2
    exit 1
}

# ─── Desired state ───────────────────────────────────────────────────────────
# Read canonical credentials from kube-infra SOPS. The mssql-backup-creds
# Secret has both ACCESS and SECRET; pick any one — they're the same
# across all backup tracks within a cluster.
declare -A CRED_SOURCES=(
    [nas-dev]="$KUBE_INFRA/flux-cd/apps/sql-servers/giks-dev/mssql-backup-creds.sops.yaml"
    [nas-prd]="$KUBE_INFRA/flux-cd/apps/sql-servers/giks-prd/mssql-backup-creds.sops.yaml"
)

# ─── Apply ───────────────────────────────────────────────────────────────────
for alias in "${!CRED_SOURCES[@]}"; do
    src="${CRED_SOURCES[$alias]}"

    if ! mc ls "$alias" >/dev/null 2>&1; then
        echo "SKIP  $alias (alias unreachable)"
        continue
    fi

    if [[ ! -f "$src" ]]; then
        echo "SKIP  $alias (credential source missing: $src)"
        continue
    fi

    creds=$(sops decrypt "$src" 2>/dev/null) || {
        echo "SKIP  $alias (sops decrypt failed — check age key)"
        continue
    }

    ak=$(echo "$creds" | yq -r '.stringData.MINIO_ACCESS_KEY')
    sk=$(echo "$creds" | yq -r '.stringData.MINIO_SECRET_KEY')

    if [[ -z "$ak" || -z "$sk" || "$ak" == "null" || "$sk" == "null" ]]; then
        echo "SKIP  $alias (could not extract MINIO_ACCESS_KEY / SECRET_KEY from $src)"
        continue
    fi

    # `mc admin user add` is idempotent — re-running with the same key
    # is treated as an update (no error, no state change if key
    # unchanged). Policy attach is similarly idempotent.
    mc admin user add "$alias" "$ak" "$sk" >/dev/null
    mc admin policy attach "$alias" readwrite --user "$ak" >/dev/null 2>&1 || true
    echo "OK    $alias — service user $ak (readwrite)"
done

echo
echo "Verify: mc admin user list <alias>"
echo "        mc admin policy entities <alias>"
