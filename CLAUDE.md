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
| RAM | 16 GB LPDDR5 (soldered; reads as 16.0 GB via `system.info` — earlier "12 GB" spec was wrong) |
| Storage | 6× M.2 NVMe slots — 1× 256 GB PM981 boot (`nvme3n1`, S/N `S444NX0N496890`) + 5× 1 TB NVMe in RAIDZ1 tank (`nvme0n1`+`nvme1n1`+`nvme2n1` = 3× PM981a, `nvme4n1`+`nvme5n1` = 2× PM9A1). Slot 4 is PCIe 3.0 x2 (boot); slots 1, 2, 3, 5, 6 are PCIe 3.0 x1 |
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
| cluster-agent | LLM-driven SRE assistant. **P3 Mode A daily digest** live since 2026-05-26: one LLM call per cluster per day at 06:00 EEST examining 24h of alerts + Loki log patterns → 0-N curated GH issues in [cluster-agent-sandbox](https://github.com/guntars-rakitko/cluster-agent-sandbox). Runbook: `wiki/docs/runbooks/cluster-agent-runbook.md` | 10.10.10.10:9595/metrics (prd scrapes), 10.10.15.10:9595/metrics (dev scrapes) — data-VLAN per cluster, not mgmt |
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

### Object store: MinIO AIStor Free

The S3 object store is **MinIO AIStor Free** — the official maintained
successor to the open-source `minio/minio` (that GitHub repo was
archived 2026-04-25). AIStor Free is free + royalty-free-licensed,
single-node standalone (= exactly our two single-instance deployments),
and full-featured for our needs (S3 API, SSE-S3, lifecycle expiration —
only distributed/replication/tiering are Enterprise-gated, none of
which we use). Pinned to `quay.io/minio/aistor/minio:RELEASE.2026-05-04T23-02-27Z`
in `apps/minio-{prd,dev}/docker-compose.yaml`.

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
./scripts/setup-minio-buckets.sh      # 6 canonical buckets per cluster
./scripts/setup-minio-users.sh        # service user + readwrite policy
./scripts/setup-minio-lifecycle.sh    # ILM rules
./scripts/setup-minio-encryption.sh   # SSE-S3 default encryption (needs KMS — see script header)
```

All four are idempotent and safe to re-run.

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

#### setup-minio-encryption.sh

Enables **SSE-S3 default encryption** on all 6 buckets of both
instances (GDPR at-rest encryption, kube-infra #520 Workstream C).
With SSE-S3 on, every object is encrypted server-side before it hits
the ZFS pool — backups become ciphertext at rest, transparently.

**Prerequisite — KMS must be configured first.** SSE-S3 on AIStor
needs a KMS root key (`MINIO_KMS_SECRET_KEY` env on the container,
sourced from Doppler `infrastructure/ops` → `MINIO_KMS_SECRET_KEY_{PRD,DEV}`,
format `<key-name>:<base64-32-bytes>`). The script verifies KMS is
live and SKIPs cleanly if not — a premature run is harmless. Full
setup steps are in the script's header comment. SSE-S3 encrypts NEW
objects only; pre-existing objects stay plaintext and age out.

### cluster-agent ops (P3 daily-digest + log mining, live since 2026-05-26)

The cluster-agent runs as a NAS-side Docker container
(`apps/cluster-agent/docker-compose.yaml`). Mode A pivoted from 5-min
polling to a daily 06:00-EEST digest on 2026-05-26 (P2), then extended
same day to also mine Loki for notable log patterns that didn't trigger
an alert (P3). One LLM call per cluster per day produces a curated
`Report` with 0-N actionable Findings that land as GH issues in
[cluster-agent-sandbox](https://github.com/guntars-rakitko/cluster-agent-sandbox).
Full reference in `wiki/docs/runbooks/cluster-agent-runbook.md`.

**Daily-digest architecture (short version).** Each 06:00 fire:

1. Pulls 24h of `ALERTS{alertstate="firing"}` from Prometheus,
   aggregates per `(alertname, fingerprint)` with chronicity
   classification (chronic / flapping / active / self_healed / transient).
   Watchdog is silently skipped.
2. Pre-fetches kubectl describe + Loki excerpts for chronic+flapping
   alerts (not for self_healed/transient — presumed noise).
3. **P3: Mines Loki for notable log patterns** — namespaces with
   24h error/warn line count ≥ 3× their 7d baseline (`ratio_outlier`),
   PLUS 10 hard-coded tripwire regexes that surface on any occurrence
   (`panic`, `fatal`, `OOMKilled`, `CrashLoopBackOff`, `ImagePullBackOff`,
   `certificate_expired`, `x509_expired`, `connection_refused`,
   `permission_denied`, `evicted`). Sample lines are scrubbed of
   probable secrets before reaching the LLM.
4. Looks up existing open GH issue dedup_keys from state.db (LLM
   avoids semantic duplicates).
5. ONE LLM call → Report (alerts + log patterns + dedup context).
6. Each Finding dispatched to Grafana annotation + GH issue +
   state.db record.

Cost: ~$0.25-0.50/day on Sonnet 4.6 (cached prefix shared between
dev + prd runs since they fire 60s apart).

**Code change vs config change.** The compose bind-mounts
`apps/cluster-agent/` → `/app`, so Python source edits land on disk
immediately when `manage.sh phase apps --apply` runs. **But uvicorn
caches the loaded module in memory.** Code-only edits don't take
effect until the container restarts. `manage.sh` recreates only when
the rendered env-var hash changes (e.g. a Doppler key was edited):

```sh
# After llm.py / dispatch.py / etc. source-only changes:
ssh truenas_admin@10.10.5.10 'sudo docker restart cluster-agent'

# After Doppler key change:
cd ~/github/truenas-infra && ./manage.sh phase apps --apply
# (will report action=update changed=True — env hash differs)
```

**Venv self-heal — add markers when you add deps.** The container's
startup checks `python -c 'import uvicorn, jinja2, cryptography'` and
ONLY rebuilds the venv if that fails. When you add a new pip-install
entry, you MUST add the corresponding `import` to the check — else old
venvs (persisted across container recreates via bind mount) skip the
rebuild because the original markers still import. We learned this
twice in P1 (jinja2 + cryptography).

**LLM auth toggle.** Compose passes BOTH `ANTHROPIC_API_KEY` and
`CLAUDE_CODE_OAUTH_TOKEN` to the container; the Doppler key
`LLM_AUTH_MODE` (`oauth` or `api_key`) decides which one `main.py` keeps
in `os.environ` at startup. Operator flips billing modes with a single
Doppler command — no compose edit:

```sh
doppler secrets set LLM_AUTH_MODE=api_key --project cluster-agent --config prd
./manage.sh phase apps --apply       # env hash changed → container recreated
```

**Current default: `api_key` — and OAuth is no longer a viable path
for cluster-agent.** Flipped 2026-05-27 after discovering OAuth has
effectively never worked for this use case. Two compounding
Anthropic-side issues block it:

1. **TOS restriction (Feb 2026):** Anthropic's Authentication and
   Credential Use policy explicitly restricts OAuth tokens
   (`sk-ant-oat01-*`) to Claude Code and claude.ai. Calling
   `api.anthropic.com/v1/messages` directly with the bearer token —
   which is exactly what `llm.py:_sdk_query` does — is a TOS
   violation pattern and gets hard-blocked at the auth gate.
2. **Billing-system bug ([anthropics/claude-code#45326](https://github.com/anthropics/claude-code/issues/45326)):** Max plan
   subscribers without a "promotional credit claim flag" get
   silently 429'd on Sonnet/Opus calls via OAuth even with balance
   available. The error masquerades as a rate limit
   (`{"type":"rate_limit_error","message":"Error"}`) but no usage
   ever counts and the dashboard shows zero pressure — making it
   indistinguishable from a genuine cap until you read the
   upstream issue. (Haiku reportedly still works on the OAuth path,
   confirming this is a billing-flag bug, not a real rate limit.)

The combination meant cluster-agent's "successful" digests since
2026-05-26 must have all billed against the API key fallback inside
`llm.py:154` (`os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or
os.environ.get("ANTHROPIC_API_KEY")`) — the OAuth branch never reached
the LLM. Operator confirmed (2026-05-27) Max-subscription usage
metrics on claude.ai were always near zero, consistent with
cluster-agent never actually consuming from that pool.

Verified working under `LLM_AUTH_MODE=api_key` 2026-05-27 04:47 UTC
via manual in-process dev digest fire (14 alert groups, 2 findings
emitted, real LLM call billed at ~$0.20 against the API account).

Cost on `api_key`: ~$0.25-0.50/day on Sonnet 4.6 = ~$10/month.
Max subscription does not offset this (it can't — OAuth is blocked).

**Do NOT flip back to oauth** until either (a) Anthropic fixes
#45326 AND amends the TOS to allow direct API use, or (b) the
agent is refactored to invoke the `claude` CLI as a subprocess
(claude-agent-sdk pattern) — which IS within the TOS-allowed
Claude Code usage path. The bare-REST shortcut in `llm.py` is
incompatible with OAuth going forward.

**Cron timing.** dev fires at `DAILY_DIGEST_HOUR:DAILY_DIGEST_MINUTE`,
prd at `+1 minute` so the second call hits the first's prompt cache
(5-min TTL on Anthropic side). Schedule + window controlled by:

- `DAILY_DIGEST_HOUR` (default `6`)
- `DAILY_DIGEST_MINUTE` (default `0`)
- `DAILY_DIGEST_WINDOW_HOURS` (default `24`)
- `DAILY_DIGEST_BUDGET_USD` (default `0.50` — pre-call cost gate per run)

**Daily summary delivery (P3+ extension, 2026-05-27).** In addition
to the curated per-Finding GH issues the digest already files, each
run can ALSO deliver a "full landscape" summary listing *every*
alert group the LLM saw (including chronic-but-known noise it didn't
escalate). Surfaces the recurring-but-self-healing patterns that
otherwise vanish from operator view. Zero LLM cost — pure aggregation
of `AlertGroup` data already in memory.

Destinations are CSV-controlled via Doppler `DIGEST_SUMMARY`:

| Value | Behavior |
|---|---|
| _(empty)_ | disabled — no summary delivery |
| `issue` | GH issue only (label `digest-summary` + `kub-{dev,prd}` + `mode-A`) |
| `email` | email only (to `DIGEST_SUMMARY_EMAIL_TO`, From: `cluster-agent {cluster} <noreply@w1.lv>`) |
| `email,issue` | both — current default |

Per-day issue: title carries date + counts (e.g. `Daily digest — prd
2026-05-27 (26 alerts · 3 actionable · 23 background)`) for inbox
preview. Yesterday's auto-closes when today's is filed. Body groups
alerts by chronicity (chronic / flapping / active / self-healed /
transient), with a `rolled into` column linking back to the per-
Finding issues. Watchdog is silently excluded from the rendering.

Email body is the same markdown rendered as plain-text + HTML
alternative (HTML wraps in `<pre>` so monospace tables stay aligned
in Gmail / Outlook). SES SMTP credentials are mirrored from
`infrastructure/shr` `SHARED_SES_W1_*` into `cluster-agent/prd` as
`SES_SMTP_*` + `SES_FROM_DEFAULT` — rotate in lockstep with the
canonical copy. Implementation: `modes/summary_issue.py` orchestrator
+ `tools/email.py` stdlib smtplib wrapper.

**Label naming convention.** All GH issues created by the agent (both
per-Finding and per-digest-summary) carry a cluster label of the form
`kub-{dev,prd}` — matching the cluster label stamped on every Loki/
Prometheus series. A GitHub inbox query `label:kub-prd` lines up with
PromQL `{cluster="kub-prd"}` — same identifier, same vocabulary.

**Doppler keys** (`cluster-agent/prd`):

- `LLM_AUTH_MODE` — `oauth` | `api_key`. Source-of-truth for which
  auth path is active.
- `ANTHROPIC_API_KEY` — sk-ant-api03-*. Always present; stripped from
  env if LLM_AUTH_MODE=oauth.
- `CLAUDE_CODE_OAUTH_TOKEN` — sk-ant-oat01-* (1y validity from
  `claude setup-token`). Always present; stripped from env if
  LLM_AUTH_MODE=api_key.
- `DAILY_DIGEST_HOUR` (default `6`) / `_MINUTE` (default `0`) /
  `_WINDOW_HOURS` (default `24`) / `_BUDGET_USD` (default `0.75` —
  bumped from 0.50 with P3 to give log-mining the prompt headroom).
- `GH_APP_ID` / `GH_APP_PRIVATE_KEY` / `GH_APP_INSTALLATION_ID` —
  cluster-agent[bot] App credentials. Compose renames with
  `CLUSTER_AGENT_` prefix on injection (the github tool reads
  `CLUSTER_AGENT_GH_APP_*`).
- `KUBECONFIG_DEV` / `KUBECONFIG_PRD` / `KUBECONFIG_TEST_RESTORE_DEV` —
  base64-encoded kubeconfigs with SA tokens. These ALSO carry the auth
  the agent uses to reach Loki / Prometheus / Alertmanager / Grafana
  via apiserver-proxy — no separate annotation-auth token.
- `GRAFANA_API_TOKEN_DEV` / `_PRD` — for `grafana_post_annotation` tool
  (creates findings as Grafana annotations on the dev/prd Grafana).
- `DIGEST_SUMMARY` (CSV, `email,issue` currently) /
  `DIGEST_SUMMARY_EMAIL_TO` — per-day summary delivery config (see
  "Daily summary delivery" subsection above).
- `SES_SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` /
  `SES_FROM_DEFAULT` — SES SMTP creds, mirrored from
  `infrastructure/shr` `SHARED_SES_W1_*`. Rotate in lockstep with the
  canonical copy.
- `SANDBOX_REPO` / `LLM_MODEL` — Mode A config (sandbox repo for digest
  issues, model name for `_MODEL_RATES_PER_1M` lookup).
- `ENABLED` / `DISABLED_MODES` / `MODE_A_CLUSTERS` — runtime kill switches.

**Reserved-for-future keys (paused after 2026-05-27 wrap):**
- `MINIO_NAS_KEY_ID` / `MINIO_NAS_SECRET_KEY` — for Mode G
  (backup verification, deferred — see roadmap-reshape spec)
- `B2_KEY_ID` / `B2_APP_KEY` — same, for off-site verification
- `KUBECONFIG_TEST_RESTORE_DEV` — for Mode G's ephemeral test-restore
  namespace SA token

**Removed 2026-05-27** (post-P3 cleanup; re-add if reviving the
related mode): `AUTOMERGE_DISABLED_REPOS` (Mode J never spec'd),
`MODE_A_BUDGET_USD` (P1 5-min legacy, replaced by
`DAILY_DIGEST_BUDGET_USD`).

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
