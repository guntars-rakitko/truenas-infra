# Services reference

> Moved verbatim from CLAUDE.md § Planned Services on 2026-11-22, truenas-infra#233.

## Planned Services

| Service | Purpose | Browser URL / endpoint |
|---|---|---|
| TrueNAS UI | NAS management | https://nas.w1.lv/ (10.10.5.10:443, direct) |
| MinIO prd console | S3 admin (prd) | https://minio-prd.w1.lv/ (via Traefik, backend on mgmt VLAN) |
| MinIO dev console | S3 admin (dev) | https://minio-dev.w1.lv/ (via Traefik, backend on mgmt VLAN) |
| MinIO prd S3 API | Cluster backup store (buckets: § setup-minio-buckets.sh) — CNPG Barman WAL+base (giks-db, w1-db), etcd snapshots, Pocket-ID Litestream, SMS-gateway dumps **+ the GIKS v1 MSSQL chain (`mssql-backups/box-prd`)**. (`velero`, `longhorn` and `pvc-backups` were deleted at the Q170S1 teardown, 2026-10-06, kube-infra#1443, and the orphan `cluster-agent` on 2026-10-17: § setup-minio-buckets.sh) | https://s3-prd.w1.lv:9000 (10.10.10.10:9000, direct HTTPS) |
| MinIO dev S3 API | Cluster backup store (buckets: § setup-minio-buckets.sh) — CNPG Barman WAL+base (giks-db, w1-db), etcd snapshots, Pocket-ID Litestream, SMS-gateway dumps. (`velero`, `longhorn` and `pvc-backups` were deleted at the Q170S1 teardown, 2026-10-06, kube-infra#1443, and the orphan `cluster-agent` on 2026-10-17: § setup-minio-buckets.sh) | https://s3-dev.w1.lv:9000 (10.10.15.10:9000, direct HTTPS) |
| Bambuddy | Bambu Lab printer control + spool inventory (since 2026-11-12). Docs, workflow and its Git backups: private repo [`3d-printing`](https://github.com/guntars-rakitko/3d-printing). ⚠ The hostname is in every spool's QR label: never rename it | https://bambuddy.w1.lv/ (via Traefik, backend 10.10.5.10:8000); for msa2-dev's print-shop, the API paths it calls (queue, library files, archives, the media token) at https://bambuddy-api.w1.lv:8000 (Traefik's `devapi` entry point on 10.10.15.10:8000, since 2026-11-24; the login and setup routes stay off it) |
| NUT server | UPS monitoring (1x APC Smart-UPS) | 10.10.5.10:3493 |
| SMB general share | Home file storage | 10.10.20.10 |
| Plex / Torrent | (deferred) | VLAN 20 |

All browser-facing services serve a valid Let's Encrypt `*.w1.lv` cert.
See `docs/tls-runbook.md` for rotation + recovery.

⚠ **Cert rotation redeploys Traefik only.** TrueNAS renews at 30 days left. The
hourly `tls-rotate` cronjob (`apps/tls/tls-rotate.sh`) copies the cert to
`/mnt/tank/system/tls/` and `app.redeploy`s every app in `TLS_CONSUMERS`,
which today is just `traefik`. Its file provider watches `/etc/traefik/dynamic`,
not the cert. MinIO prd/dev re-read the cert themselves and are deliberately
NOT redeployed; a redeploy would be ~30 s of S3 outage for every backup track.
Both facts were measured at the 2026-09-14 renewal, which the docs had
**backwards**. Until 2026-09-26 the script also died under `set -e` before any
redeploy. Together that left wiki.w1.lv on the old cert for nine days
(kube-infra #1252 / #1253). A failed redeploy is retried hourly from
`.tls-redeploy-pending`. `tests/test_tls_rotate.py` requires every enabled app
that mounts the TLS dir to be in `TLS_CONSUMERS` or on its evidence-backed
exemption list.
