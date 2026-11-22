# MinIO reference

> Moved verbatim from CLAUDE.md § Object store: MinIO AIStor Free, § MinIO bucket internals (buckets, users, lifecycle, encryption) on 2026-11-22, truenas-infra#233.

### Object store: MinIO AIStor Free

The S3 object store is **MinIO AIStor Free** — the official maintained
successor to the open-source `minio/minio` (that GitHub repo was
archived 2026-04-25). AIStor Free is free + royalty-free-licensed,
single-node standalone (= exactly our two single-instance deployments),
and full-featured for our needs (S3 API, SSE-S3, lifecycle expiration —
only distributed/replication/tiering are Enterprise-gated, none of
which we use). Pinned (tag + digest) to `quay.io/minio/aistor/minio:RELEASE.2026-06-06T02-44-06Z`
in `apps/minio-{prd,dev}/docker-compose.yaml`. ⚠ This said
`RELEASE.2026-05-04T23-02-27Z` until 2026-09-13 — one release behind what the
compose files actually run. Re-read the compose file before quoting a version
here; this section is a mirror, not the source of truth.

⚠ **Do not upgrade to RELEASE.2026-09-19 or later on the Free tier** (2026-10-10, #181 closed).
Its release notes (#6509, #6331) end the Free tier's automatic encryption and refuse encryption
requests. A throwaway local copy with OUR licence (the running 06-06 release wrote first, then 09-19
started on the same data) showed what that means here:
- a PUT into a bucket with SSE-S3 configured FAILS ("This feature is not available for Free tier
  licenses"). That is every bucket `setup-minio-encryption.sh` covers, so CNPG WAL archiving, the
  etcd snapshots, Litestream and the v1 box's MSSQL backups would all stop;
- a PUT into a bucket without it succeeds and lands in PLAINTEXT on disk: `postgres-backups-w1`,
  which holds the SMS gateway's data since 2026-10-03;
- setting bucket encryption, or an explicit SSE request, is refused. Existing objects stay readable;
- the server bumped its cluster API version (9 → 18) at start, so there is no downgrade.

Renovate holds MinIO behind Dependency Dashboard approval (`renovate.json`). The way forward is a
decision, not an upgrade: move encryption at rest to ZFS native encryption so that MinIO needs no
KMS, buy a tier that keeps it, or change object store.

**A license file is required — even for the Free tier.** The "runs
license-free" claim was wrong: AIStor gates S3 *data-plane* operations
(`mc ls`, GET/PUT, etc.) on a valid license; without one the server
starts but every data op fails with `License has fully expired`
(admin/KMS APIs still work, which masks the problem). The free-tier
license is obtained at no cost from the [MinIO pricing page](https://www.min.io/pricing)
(Free tier → Get Started) — one org-scoped token, reusable for both
single-node instances. It lives in Doppler `infrastructure/ops` →
`MINIO_AISTOR_LICENSE` and is surfaced into each container as
`/minio.license` via a Docker Compose `configs:` block (content
substituted from Doppler by `_render_compose`, same as the root
credentials); the server command passes `--license /minio.license`.
The license is a ~440-char JWT; it has an expiry — renew from the
same page before it lapses.

**Naming stays `minio-*` / `MINIO_*`** — AIStor *is* MinIO: same server
binary, same `MINIO_*` env vars, same `mc` client. Renaming infra
resources to "aistor" would half-match reality and confuse; the `minio`
naming is correct, not stale.

### MinIO bucket internals (buckets, users, lifecycle, encryption)

TrueNAS API doesn't reach inside the MinIO container — bucket-level
config (creation, users, lifecycle, retention, encryption) lives there.
We drive `mc` directly via four idempotent scripts under `scripts/`, all
using the operator's pre-configured `nas-prd` / `nas-dev` aliases
(set up once per laptop with `mc alias set` against the
`MINIO_ROOT_USER_{DEV,PRD}` / `MINIO_ROOT_PASSWORD_{DEV,PRD}` keys
in Doppler `infrastructure/ops`).

**Order of operations after a fresh MinIO bootstrap:**

```sh
./scripts/setup-minio-buckets.sh      # 8 buckets per instance (incl. the orphan loki-chunks)
./scripts/setup-minio-users.sh        # service user + readwrite policy
./scripts/setup-minio-lifecycle.sh    # ILM rules
./scripts/setup-minio-encryption.sh   # SSE-S3 default encryption (needs KMS — see script header)
```

All four are idempotent and safe to re-run.

#### setup-minio-buckets.sh

Creates the backup buckets on each MinIO instance — **eight** today, one of
them (`loki-chunks`) an orphan, plus `archive` (2026-10-29) for things kept for good, with no consumer (authoritative list = the
`BUCKETS` array in the script; its header deliberately states no count: it said
"nine" for two buckets after that stopped being true). `tests/test_minio_setup_scripts.py`
runs `setup-minio-{buckets,encryption,lifecycle}.sh` against a stub `mc` and fails if it creates a
deleted bucket (`longhorn`, `velero` and `pvc-backups` at the teardown, `cluster-agent` on
2026-10-17), if `postgres-backups` is not encrypted on both instances, if the encryption or
lifecycle script names a bucket this one never creates (a live `SKIP` with exit 0), or if any
ILM row targets a bucket whose client prunes it (`loki-chunks`, `pocket-id-litestream`):

| Bucket | Consumer |
|---|---|
| `archive` | Long-term archive, kept for good. Holds the final GIKS v1 `GiksDb` backup (`giks-v1/`, taken 2026-10-29 before the v1 box was shut down). ⚠ **No ILM, on purpose**: never add this bucket to `setup-minio-lifecycle.sh` |
| `etcd-snapshots` | CronJob — `talosctl etcd snapshot` |
| `loki-chunks` | ⚠ **ORPHAN — no consumer.** Loki has kept chunks and index on its local filesystem PVC since 2026-05-26 and never touches S3 (kube-infra `flux-cd/infrastructure/helmreleases/loki.yaml` header). Still in `BUCKETS`, so every MinIO bootstrap recreates it (the 2026-09-23 rebuild did). Removal pending: drop it from `setup-minio-{buckets,encryption}.sh`, confirm the bucket is empty on both instances, then `mc rb`. |
| `mssql-backups` | **Legacy GIKS-v1 box** (docker-prd-01) — MSSQL `BACKUP DATABASE TO URL` targets. **Not a K8s-cluster track** — the cluster MSSQL was decommissioned 2026-06-17 (GIKS is on Postgres); only the v1 box still writes here. |
| `postgres-backups` | CloudNativePG — Barman Cloud Plugin WAL + base backups |
| `postgres-backups-w1` | CloudNativePG **w1-db** (web-tracker) — its own bucket, not a prefix. MinIO ILM is bucket-wide, so two retention windows need two buckets; and it makes a `serverName` typo fail into an empty bucket instead of silently landing in the financial chain's prefix. ⚠ An ILM boundary, **not** a credential one — the shared service user has `readwrite` on `s3:*`. |
| `pocket-id-litestream` | Pocket-ID — SQLite Litestream replicas (DR for the OIDC IdP) |
| `sms-gateway-backups` | SMS-gateway appliance — nightly `pg_dump` of the box's `smsgw`+`gammu` DBs (`box-<env>/` prefix) |

*Deleted at the Q170S1 teardown (2026-10-06, kube-infra#1443), on the operator's decision: `longhorn` (the old clusters' volume and system backups: 72.6 GB prd / 133.8 GB dev), `velero` (their manifest backups) and `pvc-backups` (restic repositories of the declined PVC-backup design; empty on prd, 4 objects on dev). None had object lock, and nothing on the MS-A2 clusters wrote to them. Their restic keys left Doppler the same day.*

*Deleted 2026-10-17, when the agent left the NAS: `cluster-agent`, an orphan (empty on both instances: the nightly `state.db` copies it was meant for were never built).*

#### setup-minio-users.sh

Provisions the cluster's service user. **One user per cluster**,
shared across all cluster backup tracks (Postgres / etcd-snapshots /
Pocket-ID / …), `readwrite` policy.
Per-track IAM scoping isn't worth the operational overhead for this
scale. (`mssql-backups` is **not** a cluster track — only the legacy
GIKS-v1 box, docker-prd-01, writes there.)

⚠ **Which MinIO user the v1 box authenticates as is UNCONFIRMED here.**
This section used to say the box has its own credentials; the wiki's
`giks-v1-box-backup` runbook says it **reuses this prd per-cluster user**
(a hand copy in the box's `/opt/stacks/mssql/.env`). If the wiki is right,
rotating the prd user without updating that `.env` and restarting
`mssql-native-backup` silently stops backups of the only production
database. Check the box before any prd rotation (compare access-key IDs,
never print the secret).

**Source of truth for the credentials is Doppler**
`infrastructure/{dev,prd}` → `KUBE_MINIO_ACCESS_KEY_ID` +
`KUBE_MINIO_SECRET_ACCESS_KEY` + `KUBE_MINIO_ENDPOINT`. The clusters
read them via DopplerSecret CRDs (kube-infra
`flux-cd/infrastructure/configs/per-cluster/<cluster>/secrets/`), rendered as
`postgres-backups-s3` + `postgres-backups-s3-w1-db` (every cluster),
`loki-s3` (every cluster; unused since Loki moved to filesystem, slated for
removal), `pocket-id-secrets` (reads them on msa2-dev only), and
`etcd-snapshot-minio` (`velero-minio` and `longhorn-s3` went with the Q170S1
clusters' Flux side at the teardown, kube-infra#1443; `mssql-backup-creds`
with the cluster MSSQL decommission, 2026-06-17).
This script reads the same Doppler keys via `doppler secrets get --plain` to
provision the MinIO user. Single canonical copy, zero drift,
no cross-repo coupling.

⚠ **`infrastructure/{dev,prd}` was shared by each Q170S1 cluster and its msa2
successor** (kube-infra `talos-os/estates.yaml` `doppler_env`), so until the
cutover a `KUBE_MINIO_*` rotation reached both at once. Since the teardown
only the msa2 clusters read them. ⚠ The prd user is ALSO copied by hand onto
the GIKS v1 box (see the ⚠ above): rotate it there in the same window.

To rotate: generate a new key pair, update Doppler
(`doppler secrets set KUBE_MINIO_ACCESS_KEY_ID=... \
--project infrastructure --config <env>` and the matching
`_SECRET_ACCESS_KEY`), run this script. It will `mc admin user add`
with the new key (idempotent update). Old key continues to work
until you `mc admin user remove` explicitly — useful for rolling
rotation.

#### setup-minio-lifecycle.sh

Current ILM rules:

| Bucket | Expiration | Why |
|---|---|---|
| `archive` (both clusters) | **none** | Deliberate: the bucket exists to keep things after their source is gone (the final GIKS v1 backup). An expiry rule here would delete the only copy. |
| `etcd-snapshots` (dev) | **7 days** | Hourly `talosctl etcd snapshot`. 168 snapshots. Dev cluster state is re-creatable, so a week of hourly granularity is generous. Added 2026-08-07 — see the warning below. |
| `etcd-snapshots` (prd) | **14 days** | 336 snapshots. Production gets the longer window so a problem noticed a week late still has pre-incident snapshots. |
| `mssql-backups` (both clusters) | 90 days | Auto-discovered backup chains for dropped DBs would otherwise accumulate forever. 90d is enough for the "I deleted a DB last quarter, need to recover" case while keeping bucket size bounded. |
| `postgres-backups` (prd) | 90 days | Coarse backstop for orphaned Barman objects. The CNPG ObjectStore `retentionPolicy: 30d` is the real PITR-window pruner; the 90d ILM only sweeps objects Barman's own retention misses (e.g. after a cluster delete). |
| `postgres-backups` (dev) | 14 days | Per-env split — dev's PITR window is 7d (regenerable data), so its ILM backstop is 14d (always kept > the Barman retention). |
| `postgres-backups-w1` (prd) | 90 days | Same shape as `postgres-backups`: the w1-db ObjectStore's `retentionPolicy: 30d` is the real pruner; ILM only sweeps what Barman's own retention misses. |
| `postgres-backups-w1` (dev) | 14 days | w1-db dev PITR window is 7d, ILM backstop 14d — kept > the Barman retention, same rule as above. |
| `sms-gateway-backups` (both clusters) | 30 days | SMS-gateway appliance nightly `pg_dump`s (own backup track, separate from CloudNativePG's `postgres-backups`). 30d is ample for the box's member/billing data. |

⚠ **`etcd-snapshots` had NO retention at all until 2026-08-07, despite two
files claiming otherwise.** This section said it was "curated by hand"; the
CronJob header in `kube-infra` said retention was "handled MinIO-side via
`mc ilm rule add ... --expire-days 30`". Neither was true — no ILM rule had
ever existed and nothing had ever been pruned. Measured on 2026-08-07, oldest
object being the first snapshot ever taken (2026-05-23):

| Bucket | Size | Objects |
|---|---|---|
| `nas-dev/etcd-snapshots` | 451 GiB | 1825 |
| `nas-prd/etcd-snapshots` | 382 GiB | 1812 |

833 GiB — roughly half of `tank/kube` (1.69 T). Hourly and growing (80 MiB per
snapshot in May → 294 MiB in August), so it compounded. The first sweep was
irreversible: both buckets are un-versioned with no object-lock.

⚠ **Tiered retention is not expressible via ILM here.** Objects share one flat
`etcd-<stamp>.db` namespace with no date prefixes, so `--expire-days` applies
uniformly. Hourly-then-daily tiering would need a prune step in the CronJob.

pocket-id-litestream remains intentionally absent (and so does the orphan `loki-chunks`, which
nothing writes): Litestream prunes its own replicas. ⚠ **Never give a bucket whose client owns
deletion an age-based backstop.** The Q170S1 era's three cases were Longhorn (incremental block
chains, so expiring a base corrupts every later backup), Velero (its TTL controller) and
`pvc-backups` (restic repositories, whose `config` and `keys/*` are the oldest objects, so an age
rule deletes them first). All three buckets were deleted at the 2026-10-06 teardown.
`tests/test_minio_setup_scripts.py` fails if a `RULES` row targets `loki-chunks` or
`pocket-id-litestream`.

#### setup-minio-encryption.sh

Enables **SSE-S3 default encryption** on the buckets in its own `BUCKETS`
list, on both instances (GDPR at-rest encryption, kube-infra #520
Workstream C). ⚠ **That list is NOT the bucket list above:** it has five
entries (`longhorn`, `velero` and `pvc-backups` left with #187, `cluster-agent` on 2026-10-17) and omits `postgres-backups-w1` and `sms-gateway-backups` (the
latter holds the SMS-gateway's member/billing dumps). So this script has
never enabled default encryption on those two. Their objects are encrypted
anyway, by AIStor's automatic SSE-KMS (`aws:kms` on their newest objects,
read 2026-10-10): the mode RELEASE.2026-09-19 removes on the Free tier
(§ Object store).
With SSE-S3 on, every object is encrypted server-side before it hits
the ZFS pool — backups become ciphertext at rest, transparently.

**Prerequisite — KMS must be configured first.** SSE-S3 on AIStor
needs a KMS root key (`MINIO_KMS_SECRET_KEY` env on the container,
sourced from Doppler `infrastructure/ops` → `MINIO_KMS_SECRET_KEY_{PRD,DEV}`,
format `<key-name>:<base64-32-bytes>`). The script verifies KMS is
live and SKIPs cleanly if not — a premature run is harmless. Full
setup steps are in the script's header comment. SSE-S3 encrypts NEW
objects only; pre-existing objects stay plaintext and age out.
