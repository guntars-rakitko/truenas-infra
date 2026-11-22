# API approach reference

> Moved verbatim from CLAUDE.md § API Configuration Approach on 2026-11-22, truenas-infra#233.

## API Configuration Approach

All configuration is applied through the TrueNAS JSON-RPC 2.0 API over
WebSocket (`truenas-api-client`), plus the `/_upload` and `/_download` HTTP
endpoints for files, using the Python CLI under
`src/truenas_infra/` (dispatched by `manage.sh`). Credentials come from
Doppler `infrastructure/ops`, fetched in-process at startup — **no `.env`
file on disk**. Other repos that need the same TrueNAS API credentials
(e.g. `wiki/tools/deploy.sh` for site uploads) fetch the identical 3 keys
(`TRUENAS_HOST` / `TRUENAS_API_KEY` / `TRUENAS_VERIFY_SSL`) from Doppler
directly using the same idiom.

**Pattern:**
1. `manage.sh` fetches per-key values from Doppler `infrastructure/ops` at startup, exports into process env
2. Python CLI reads those env vars via `RuntimeConfig.from_env()` (no dotenv)
3. Each phase targets a specific domain (users, network, tls, pool, datasets, …)
4. Phases are idempotent — safe to re-run; default is dry-run, `--apply` to write.
   File uploads (`ensure_file_on_nas`: wiki `nginx.conf`, Traefik
   `routes.yaml`, the tls scripts) compare **content** (sha256), not
   size, since 2026-09-29, and each `*_ensured` line logs `content_verified`.
   Size alone missed equal-length edits (a port, an IP octet, `3.20`→`3.23`).
