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
#
# ⚠ velero / longhorn are DELIBERATELY absent — do not "add a backstop".
# Longhorn backups are incremental block chains whose later backups reference
# blocks written by earlier ones; expiring a base by age corrupts every
# surviving backup that depended on it. Velero's own TTL controller expects to
# own deletion. Age-based ILM is the wrong tool for both.
# loki-chunks and pocket-id-litestream are absent because Loki's compactor and
# Litestream prune their own object stores.
#
# etcd-snapshots IS here as of 2026-08-07. It previously was not, and both this
# file and kube-infra's etcd-snapshot-cronjob.yaml carried a comment claiming
# retention was handled — this one said "hand-curated", the CronJob said
# "handled MinIO-side via mc ilm ... --expire-days 30". Neither was true: no ILM
# rule had ever existed and nothing was ever pruned. Measured 2026-08-07, with
# the oldest object being the first snapshot ever taken (2026-05-23):
#     nas-dev/etcd-snapshots  451 GiB  1825 objects
#     nas-prd/etcd-snapshots  382 GiB  1812 objects
# 833 GiB — about half of tank/kube. Snapshots are hourly and growing (80 MiB
# in May, 294 MiB in August), so it compounded.
#
# ⚠ The first sweep is IRREVERSIBLE. Both buckets are un-versioned with no
# object-lock, so expired objects are gone. Deleting ~1,657 objects on dev and
# ~1,476 on prd was a conscious operator decision, not a side effect.
#
# Per-env split, same shape as postgres-backups above (dev 14 / prd 90):
#   dev  7d = 168 hourly snapshots — dev cluster state is re-creatable, and a
#             week of hourly granularity is already generous for it.
#   prd 14d = 336 hourly snapshots — production gets the longer window, so a
#             problem noticed a week late still has pre-incident snapshots.
# etcd snapshots are DR artifacts where recent granularity is what matters;
# Velero's daily cluster-state backup covers the longer tail on both clusters.
# The trade is explicit: a corruption first noticed after the window has no
# pre-incident etcd snapshot left.
#
# ⚠ Tiered retention (e.g. hourly for 3d + daily for 30d) is NOT expressible
# here. Objects share one flat `etcd-<stamp>.db` namespace with no date
# prefixes, so a single --expire-days rule applies uniformly. Doing better
# needs a prune step in the CronJob, not an ILM rule.
RULES=(
    "nas-dev/cluster-agent 30"
    "nas-dev/etcd-snapshots 7"
    "nas-dev/mssql-backups 90"
    "nas-dev/postgres-backups 14"
    "nas-dev/sms-gateway-backups 30"
    "nas-prd/cluster-agent 30"
    "nas-prd/etcd-snapshots 14"
    "nas-prd/mssql-backups 90"
    "nas-prd/postgres-backups 90"
    "nas-prd/sms-gateway-backups 30"
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
