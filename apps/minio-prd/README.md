# minio-prd

S3 backend for prd cluster Velero backups.

- Bind: `10.10.10.10:9000` (S3 API), `10.10.10.10:9001` (console)
- Data: `/mnt/tank/kube/prd/velero`
- Secrets: Doppler `infrastructure/ops` → `MINIO_ROOT_USER_PRD` + `MINIO_ROOT_PASSWORD_PRD`, rendered into the compose env by `_render_compose` at deploy time.

Compose file TBD. Image: `minio/minio:latest` (community, single-binary).
