# TrueNAS version reference

> Moved verbatim from CLAUDE.md § Version Policy on 2026-11-22, truenas-infra#233.

## Version Policy

**Running version: TrueNAS Community Edition 25.10.7** (codename "Goldeye"), upgraded
from 25.10.3.1 on 2026-09-13. This runs on the Beelink ME Mini 2. The Community Edition is the free SCALE-lineage successor that uses Docker (not K3s) for apps.

**Always check the latest TrueNAS version and API documentation before deploying or configuring anything.** Never rely on cached knowledge. Verify at:
- https://www.truenas.com/docs/
- https://www.truenas.com/docs/api/
- Release notes: https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/

When upgrading, update the pinned version here and re-verify all scripts against the new API surface.

### The REST API is removed in 26.0: nothing here uses it (truenas-infra#10, 2026-10-13)

TrueNAS 25.10 deprecated the REST API (`/api/v2.0/…`), and the next major removes it.
That major was announced as 26.04 and is now numbered 26.0 (`TS-26.0.0-BETA.3` on
2026-10-13). The middleware's master branch no longer has `restful.py` or the
deprecation alert.

- **Nothing calls `/api/v2.0`.** The NAS's middleware audit log, from 2026-10-05 on,
  holds **0** `LEGACY_REST` records. The 25.10 deprecation alert
  (`RESTAPIUsageAlertSource`) counts exactly those records once a day.
  - The May 2026 alert named `172.16.2.2`, a container on the NAS's Docker bridge,
    36 times a day. It was most likely Homepage's TrueNAS widget, the only app wired
    with `TRUENAS_API_KEY`. Homepage was retired with the pool rebuild on 2026-09-23.
- **The audit's `REST` records are not the deprecated API.** They are `filesystem.put`
  calls through `/_upload`: this repo's `ensure_file_on_nas`, and the wiki deploy's
  ~300 per run. Both send `Authorization: Bearer <key>`. Master keeps `/_upload` and
  `/_download` (`apps/file_app.py`), and their Bearer API-key auth.
- **The docs' REST commands** (this repo's bootstrap notes, the wiki's laptop-loss
  runbook) now use `./manage.sh preflight`. Each also put the key on curl's command
  line.
- **To re-check before upgrading**, count `LEGACY_REST` records: `audit.query` with
  `services: ["MIDDLEWARE"]`, the filter `service_data.protocol = LEGACY_REST`, and
  `count: true`. It must be 0. Also run `grep -rn 'api/v2\.0'` over the workspace.
  The upgrade itself, with a bump of `truenas-api-client`'s `TS-25.10.3` pin, is
  truenas-infra#208.
