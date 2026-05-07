# minio-dev

S3 backend for dev cluster Velero backups.

- Bind: `10.10.15.10:9000` (S3 API), `10.10.15.10:9001` (console)
- Data: `/mnt/tank/kube/dev/velero`
- Secrets: Doppler `infrastructure/ops` → `MINIO_ROOT_USER_DEV` + `MINIO_ROOT_PASSWORD_DEV`, rendered into the compose env by `_render_compose` at deploy time.

Compose file TBD. Image: `minio/minio:latest` (community, single-binary).
