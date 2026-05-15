# CLAUDE.md — truenas-infra

Guidance for Claude Code when working in this repository.

---

## Overview

API-driven TrueNAS configuration for a homelab NAS. All configuration is stored in Git and applied via the TrueNAS REST API — no manual UI changes. This ensures the NAS can be fully rebuilt from scripts if needed.

The NAS serves the Kubernetes clusters defined in `guntars-rakitko/kube-infra` and sits on the network managed by `guntars-rakitko/mikrotik-infra`.

---

## Related Repositories

This repo is part of a coordinated homelab stack. When making changes that affect shared state — network topology, IP plan, hardware, shared services, or BIOS/boot configuration — update every affected repo so they stay in sync.

| Repo | Scope |
|---|---|
| [`guntars-rakitko/kube-infra`](https://github.com/guntars-rakitko/kube-infra) | Talos + Kubernetes clusters (prd/dev), Flux CD, workloads |
| [`guntars-rakitko/mikrotik-infra`](https://github.com/guntars-rakitko/mikrotik-infra) | Router, switches, WiFi, LTE, VLANs, firewall, DHCP/DNS |
| [`guntars-rakitko/truenas-infra`](https://github.com/guntars-rakitko/truenas-infra) | NAS storage (ZFS, NFS), MinIO, PXE server, NUT server, media apps (this repo) |
| [`guntars-rakitko/bios-config`](https://github.com/guntars-rakitko/bios-config) | ASUS Q170S1 BIOS settings (AMT, PXE, power, security) |
| [`guntars-rakitko/wiki`](https://github.com/guntars-rakitko/wiki) | Internal MkDocs wiki at [wiki.w1.lv](https://wiki.w1.lv/) — mirrors docs from all above |

**Always read the CLAUDE.md of every related repo before making cross-cutting changes.** Common shared concerns:
- **IP plan / VLAN design** — canonical in `mikrotik-infra` (router is source of truth); referenced here
- **Hardware inventory** — each repo describes its own devices; update all when adding/removing
- **PXE / NUT / MinIO services** — live here on the NAS; referenced by `kube-infra` and `bios-config`
- **Secrets** — Doppler `infrastructure/ops` (`TRUENAS_*` + `MINIO_ROOT_*` + `AMT_*` + `SHARED_CLOUDFLARE_API_TOKEN`). Migration tracked in kube-infra #92.
- **Wiki mirror** — hand-written topic pages in the `wiki` repo reproduce data from this one; update both in the same commit set (see [Wiki maintenance](#wiki-maintenance) below)

Local clones live at `/Users/gunrak/github/{kube-infra,mikrotik-infra,truenas-infra,bios-config,wiki}`.

---

## Wiki maintenance

The homelab wiki at https://wiki.w1.lv/ contains **hand-written topic
pages** that synthesize data across repos. They do not update
automatically. When you change any of the sources below in this repo,
edit the matching wiki page in the same commit set.

| Change in this repo | Update in `wiki/` |
|---|---|
| `CLAUDE.md` (this file) | _Auto-synced_ — `sync-repos.sh` pulls `truenas-infra/CLAUDE.md` → `docs/projects/truenas-infra.md` |
| `config/network.yaml` (NICs, sub-IPs, hostname) | `docs/architecture/ip-plan.md` (NAS static allocations table) |
| `config/dns.yaml` (add/remove DNS record) | `docs/architecture/hostnames.md` (record inventory) |
| `config/apps.yaml` (new Custom App) | `docs/architecture/hostnames.md`, `docs/reference/links.md` |
| `apps/traefik/routes.yaml` (new admin UI route) | `docs/architecture/hostnames.md` (admin-plane table), `docs/architecture/tls-split-horizon.md` |
| `config/tls.yaml` (cert config change) | `docs/architecture/tls-split-horizon.md` |
| `docs/*.md` (any runbook) | _Auto-synced_ — see `wiki/sync-map.yaml` |
| `apps/pxe/pxe-download.sh` (new PXE asset) | `docs/runbooks/bios-apply-pxe-setup.md` (if cross-repo flow changes), `docs/runbooks/pxe-operator.md` (adding / removing PXE ISOs) |
| `docs/bios-apply-pxe-setup.md` | _Auto-synced_ → `docs/runbooks/bios-apply-pxe-setup.md` |
| `docs/verification.md` | _Auto-synced_ → `docs/reference/verification-matrix.md` |
| Doppler `infrastructure/ops` (add/remove key) | `docs/reference/env-vars.md`, possibly `docs/architecture/secrets-flow.md` |
| "Policy for adding new services" section (above) | `docs/architecture/tls-split-horizon.md` decision tree |

**Deploy the wiki** after the edit:

```sh
cd ~/github/wiki && ./tools/deploy.sh --verify
```

The verify matrix (`./manage.sh phase verify`) catches structural drift
(DNS resolution, TLS SAN coverage, cert expiry, app state) for
anything added to `config/dns.yaml`. It does **not** catch prose drift
in the wiki (stale IPs in commentary, outdated VLAN descriptions) —
that's operator responsibility.

---

## Version Policy

**Target deployment version: TrueNAS Community Edition 25.10.3** (codename "Goldeye"). This is the version being installed on the Beelink ME Mini 2. The Community Edition is the free SCALE-lineage successor that uses Docker (not K3s) for apps.

**Always check the latest TrueNAS version and API documentation before deploying or configuring anything.** Never rely on cached knowledge. Verify at:
- https://www.truenas.com/docs/
- https://www.truenas.com/docs/api/
- Release notes: https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/

When upgrading, update the pinned version here and re-verify all scripts against the new API surface.

---

## Hardware

| Component | Detail |
|---|---|
| Device | Beelink ME Mini (post-RMA unit; 3.3V rail + ASM2824 link-training defect fixed in units manufactured after 2025-09-08) |
| CPU | Intel N150 (4 E-cores, no HT) |
| RAM | 12 GB LPDDR5 (soldered) |
| Storage | 6× M.2 NVMe slots — 1× 256 GB PM981 (boot) + 5× 1 TB NVMe in RAIDZ1 (3× PM981a + 2× PM9A1). Slot 4 is PCIe 3.0 x2 (boot); slots 1, 2, 3, 5, 6 are PCIe 3.0 x1 |
| NIC1 | Intel **I226-V** 2.5G (`igc` driver, `enp1s0`, MAC `78:55:36:07:25:93`) — data, tagged trunk carrying VLANs 10 / 15 / 20 (sub-interfaces 10.10.10.10 / 10.10.15.10 / 10.10.20.10) |
| NIC2 | Intel **I226-V** 2.5G (`igc` driver, `enp2s0`, MAC `78:55:36:07:25:92`) — management, untagged VLAN 5 (10.10.5.10) |
| OS | TrueNAS Community Edition 25.10.3.1 (codename Goldeye) |

---

## Network

| Interface | VLAN | IP | Purpose |
|---|---|---|---|
| NIC1 — tagged sub-iface | 10 | 10.10.10.10 | Prod Kube: NFS (Longhorn prd), MinIO (Velero prd) |
| NIC1 — tagged sub-iface | 15 | 10.10.15.10 | Dev Kube: NFS (Longhorn dev), MinIO (Velero dev) |
| NIC1 — tagged sub-iface | 20 | 10.10.20.10 | Home: Plex, torrent UI, SMB general share |
| NIC2 — untagged | 5 | 10.10.5.10 | TrueNAS API/UI, SSH, PXE/TFTP, NUT |

Connected to CRS310:
- `ether7` — tagged trunk, VLANs 10/15/20 (data, NIC1)
- `ether8` — untagged, VLAN 5 (management, NIC2)

Service-to-interface binding is enforced in TrueNAS (e.g. NFS only listens on `.10.10` and `.15.10`; Plex only on `.20.10`), so home devices cannot reach Kube backup targets even though NIC1 is physically shared.

---

## Planned Services

| Service | Purpose | Browser URL / endpoint |
|---|---|---|
| TrueNAS UI | NAS management | https://nas.w1.lv/ (10.10.5.10:443, direct) |
| MeshCentral | AMT KVM into K8s nodes | https://mc.w1.lv/ (via Traefik) |
| PXE directory index | Browse cached distro/utility assets | http://10.10.5.10:8080/ (nginx autoindex, no auth) |
| MinIO prd console | S3 admin (prd) | https://minio-prd.w1.lv/ (via Traefik, backend on mgmt VLAN) |
| MinIO dev console | S3 admin (dev) | https://minio-dev.w1.lv/ (via Traefik, backend on mgmt VLAN) |
| MinIO prd S3 API | Velero backup store | https://s3-prd.w1.lv:9000 (10.10.10.10:9000, direct HTTPS) |
| MinIO dev S3 API | Velero backup store | https://s3-dev.w1.lv:9000 (10.10.15.10:9000, direct HTTPS) |
| Traefik dashboard | Proxy ops view | https://traefik-nas.w1.lv/dashboard/ |
| NFS (prd) | Longhorn backups | 10.10.10.10 (NFS, service-level bindip) |
| NFS (dev) | Longhorn backups | 10.10.15.10 |
| PXE / TFTP server | custom iPXE 1.21.1+ built from source (apps/pxe/) — USB_HCD_USBIO fix for Intel Q170. Dynamic menu auto-listed from /mnt/tank/system/pxe/http/extras/{utils,distros,live}/*.iso by apps/pxe/pxe-genmenu.sh. Operator runbook: `docs/pxe-operator.md` | 10.10.5.10:69/udp (TFTP), :8080 (HTTP assets) |
| NUT server | UPS monitoring (1x APC Smart-UPS) | 10.10.5.10:3493 |
| SMB general share | Home file storage | 10.10.20.10 |
| Plex / Torrent | (deferred) | VLAN 20 |

All browser-facing services serve a valid Let's Encrypt `*.w1.lv` cert.
See `docs/tls-runbook.md` for rotation + recovery.

### Policy for adding new services

Decision tree — **apply every time you add an HTTPS endpoint on this network**:

1. **Admin / mgmt UI a human opens in a browser?**
   → Expose through Traefik at `10.10.5.20:443`. Backend plain HTTP on
     mgmt-VLAN IP. Portless URL `<name>.w1.lv`. Add DNS record (via
     `mikrotik-infra/manage.sh` option 15) pointing at `10.10.5.20`.
     Add a route in `apps/traefik/routes.yaml`.
2. **Data-plane API a machine consumes (S3, gRPC, K8s API, …)?**
   → Bind directly on the service's own VLAN IP using its **native
     port** (`:443` on data-VLAN IPs is reserved for future growth).
     Mount wildcard cert from `/mnt/tank/system/tls/`. DNS record
     points at the service VLAN IP.
3. **Fundamental infra-plane UI (TrueNAS, MikroTik, switch)?**
   → Leave on the device's native port, **never proxy**. These must
     remain reachable when Traefik is down.
4. **Internet-facing?**
   → Not in scope today. When needed: separate public DNS record on
     CloudFlare, CloudFlare Tunnel or dedicated ingress — NOT through
     mgmt-VLAN Traefik.

Hostname convention: `<role>.w1.lv` for singletons, `<role>-<env>.w1.lv`
for multi-instance (minio-prd, traefik-nas), `<role>-<NN>` for per-box
(kub-prd-01). All lowercase, hyphen-separated.

---

## Storage Design (TBD)

To be defined during Phase 1 hardware setup. Expected:
- ZFS pool across available NVMe drives
- Datasets for: Longhorn backups, Velero/MinIO, Plex media, general storage
- Snapshot schedule on critical datasets

---

## API Configuration Approach

All configuration is applied via TrueNAS REST API using the Python CLI under
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
4. Phases are idempotent — safe to re-run; default is dry-run, `--apply` to write

### MinIO bucket internals (buckets, users, lifecycle)

TrueNAS API doesn't reach inside the MinIO container — bucket-level
config (creation, users, lifecycle, retention) lives there. We drive
`mc` directly via three idempotent scripts under `scripts/`, all
using the operator's pre-configured `nas-prd` / `nas-dev` aliases
(set up once per laptop with `mc alias set` against the
`MINIO_ROOT_USER_{DEV,PRD}` / `MINIO_ROOT_PASSWORD_{DEV,PRD}` keys
in Doppler `infrastructure/ops`).

**Order of operations after a fresh MinIO bootstrap:**

```sh
./scripts/setup-minio-buckets.sh      # 4 canonical buckets per cluster
./scripts/setup-minio-users.sh        # service user + readwrite policy
./scripts/setup-minio-lifecycle.sh    # ILM rules
```

All three are idempotent and safe to re-run.

#### setup-minio-buckets.sh

Creates the four canonical backup buckets on each MinIO instance:

| Bucket | Consumer |
|---|---|
| `velero` | Velero — K8s manifest backups |
| `longhorn` | Longhorn — volume + system backups |
| `mssql-backups` | SQL Server — `BACKUP DATABASE TO URL` targets |
| `etcd-snapshots` | CronJob — `talosctl etcd snapshot` |

#### setup-minio-users.sh

Provisions the cluster's service user. **One user per cluster**,
shared across all backup tracks (Velero / Longhorn / MSSQL /
etcd-snapshots), `readwrite` policy. Per-track IAM scoping isn't
worth the operational overhead for this scale.

**Source of truth for the credentials is Doppler**
`infrastructure/{dev,prd}` → `KUBE_MINIO_ACCESS_KEY_ID` +
`KUBE_MINIO_SECRET_ACCESS_KEY` + `KUBE_MINIO_ENDPOINT`. The cluster
reads them via DopplerSecret CRDs (rendered as the K8s Secrets
`velero-minio`, `longhorn-s3`, `mssql-backup-creds`); this script
reads the same Doppler keys via `doppler secrets get --plain` to
provision the MinIO user. Single canonical copy, zero drift,
no cross-repo coupling.

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
| `mssql-backups` (both clusters) | 90 days | Auto-discovered backup chains for dropped DBs would otherwise accumulate forever. 90d is enough for the "I deleted a DB last quarter, need to recover" case while keeping bucket size bounded. |

Velero / Longhorn / etcd-snapshot buckets are intentionally not in
this script — Velero and Longhorn manage their own retention via
controller TTL, and etcd-snapshots is curated by hand for now.

---

## Secrets — Doppler `infrastructure/ops`

Credentials live in Doppler (project `infrastructure`, config `ops`).
`manage.sh` fetches the keys it needs at startup via per-key
`doppler secrets get --plain` calls; per-app secrets used by the apps
deploy flow are fetched at deploy time by `_load_doppler_for_app` in
`src/truenas_infra/modules/apps.py`.

**Per-script keys** (read by `manage.sh` top-level + Python config):

- `TRUENAS_HOST`, `TRUENAS_API_KEY`, `TRUENAS_VERIFY_SSL`
- `TRUENAS_NUT_MONPWD`
- `SHARED_CLOUDFLARE_API_TOKEN` (aliased to `CLOUDFLARE_API_TOKEN` after fetch — CloudFlare SDK convention)

**Emergency / break-glass credentials** (operator-typed-only paths
where Apple Passwords is the canonical store; Doppler holds the
machine-readable copy):

- **TrueNAS admin (Web UI Shell + emergency console)** — username +
  password rotated 2026-05-12 (closes
  [kube-infra#94](https://github.com/guntars-rakitko/kube-infra/issues/94)).
  No longer shares value with `AMT_PASSWORD` (the anti-pattern that
  motivated the rotation). Lives in:
    * Doppler `infrastructure/ops` → `TRUENAS_ADMIN_USER` +
      `TRUENAS_ADMIN_PASSWORD` (machine-readable copy for future DR
      scripts + as the canonical source the operator pulls from)
    * Apple Passwords → `TrueNAS root` entry (operator-typed mirror for
      browser autofill on the Web UI Shell login). Update both in
      lockstep on rotation.
  Note: SSH password auth is disabled at the sshd daemon (key-only),
  so this credential is for **Web UI Shell** access — for any
  destructive operation that the API can't do (see
  `wiki/docs/runbooks/rotate-amt-credentials.md` for an example of
  using the API+cronjob workaround to call `rm` as root).
- **SSH service account name** — `svc-automation` (the user the API
  key is bound to; also the SSH username for ad-hoc operator shell
  work, when configured). It's a convention, not a credential —
  documented here, not stored in Doppler. The API key is in Doppler
  as `TRUENAS_API_KEY`.

**Per-app keys** (`_DOPPLER_KEYS_PER_APP` in `modules/apps.py`):

- `minio-prd` → `MINIO_ROOT_USER_PRD`, `MINIO_ROOT_PASSWORD_PRD`
- `minio-dev` → `MINIO_ROOT_USER_DEV`, `MINIO_ROOT_PASSWORD_DEV`
- `amtctl` → `AMT_USER`, `AMT_PASSWORD`
- `homepage` → `TRUENAS_API_KEY` + `MINIO_ROOT_*_{DEV,PRD}` (5 keys total, mapped from `HOMEPAGE_VAR_*` placeholders in compose)

**Inspect / edit:**

```sh
# Show all NAS-related keys
doppler secrets --project infrastructure --config ops --only-names | grep -E "TRUENAS_|MINIO_|AMT_|SHARED_CLOUDFLARE"

# Get one value (revealed)
doppler secrets get TRUENAS_API_KEY --project infrastructure --config ops --plain

# Set a value
doppler secrets set TRUENAS_API_KEY=newvalue --project infrastructure --config ops
```

**Recovery if Doppler unreachable:** the operator-side Phase 1 backup
(age-encrypted tarball in iCloud + MinIO `disaster-recovery` bucket)
holds the same values. See [`kube-infra/docs/disaster-recovery.md`](https://github.com/guntars-rakitko/kube-infra/blob/main/docs/disaster-recovery.md).
Migration tracking: kube-infra #92.

---

## File Structure

```
CLAUDE.md
manage.sh             # Top-of-file fetches secrets from Doppler ops
config/
  apps.yaml           # App registry (no secrets path; Doppler keys
                      # mapped per-app in modules/apps.py)
src/                  # Python CLI implementation
scripts/
  setup-minio-{buckets,users,lifecycle}.sh    # one-shot MinIO bootstrap
```

---

## Related Repos

See the **Related Repositories** section at the top of this file for the full cross-repo map.
