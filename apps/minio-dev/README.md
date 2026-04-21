# minio-dev

S3 backend for dev cluster Velero backups.

- Bind: `10.10.15.10:9000` (S3 API), `10.10.15.10:9001` (console)
- Data: `/mnt/tank/kube/dev/velero`
- Secrets: `secrets.sops.yaml` (root user + password) — rendered to `.env` by `scripts/render-env.py`

Compose file TBD. Image: `minio/minio:latest` (community, single-binary).
