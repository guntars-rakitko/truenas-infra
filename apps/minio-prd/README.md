# minio-prd

S3 backend for prd cluster Velero backups.

- Bind: `10.10.10.10:9000` (S3 API), `10.10.10.10:9001` (console)
- Data: `/mnt/tank/kube/prd/velero`
- Secrets: Doppler `infrastructure/ops` → `MINIO_ROOT_USER_PRD` + `MINIO_ROOT_PASSWORD_PRD`, rendered into the compose env by `_render_compose` at deploy time.

Compose file: [`docker-compose.yaml`](./docker-compose.yaml) (it exists — the
"TBD" here was stale). Image: `quay.io/minio/aistor/minio:RELEASE.2026-06-06T02-44-06Z`
pinned by digest, i.e. **MinIO AIStor Free**, not the community
`minio/minio`.

> ⚠ This line used to read *"Compose file TBD. Image: `minio/minio:latest`
> (community, single-binary)"*. Both halves were wrong, and the second is now
> actively harmful: MinIO **deleted their Docker Hub organisation**, so
> `minio/minio` (and `minio/mc`) return HTTP 404 there. Anyone following the old
> instruction would hit an unpullable image. AIStor Free is the maintained
> successor and is served from quay.io. See the § Object store: MinIO AIStor
> Free section of [`CLAUDE.md`](../../CLAUDE.md) for the licence requirement —
> a licence file is mandatory even on the Free tier, or every S3 data operation
> fails while the server still reports healthy.
