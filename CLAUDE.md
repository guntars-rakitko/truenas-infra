# CLAUDE.md — truenas-infra

Guidance for Claude Code when working in this repository.

---

## Overview

API-driven TrueNAS configuration for a homelab NAS. Almost all configuration is stored in Git and applied via the TrueNAS REST API, so the NAS can be rebuilt from scripts. **Two things are deliberately managed out-of-band in the UI** and are NOT reproduced by the tool: (1) the `truenas_admin` operator identity itself (its login + the "Allowed Sudo Commands (No Password)" allowlist that the automation depends on — see § SSH + sudo on NAS), and (2) a small set of NUT bits noted below (Extra Users, alert classes, the Init/Shutdown hook registration). A clean rebuild therefore re-runs the phases **and** re-applies those UI-managed pieces from their documented recipes.

The NAS serves the Kubernetes clusters defined in `guntars-rakitko/kube-infra` and sits on the network managed by `guntars-rakitko/mikrotik-infra`.

---

## Related Repositories

This repo is part of a coordinated homelab stack. When making changes that affect shared state — network topology, IP plan, hardware, shared services, or BIOS/boot configuration — update every affected repo so they stay in sync.

| Repo | Scope |
|---|---|
| [`guntars-rakitko/kube-infra`](https://github.com/guntars-rakitko/kube-infra) | Talos + Kubernetes clusters (prd/dev), Flux CD, workloads |
| [`guntars-rakitko/mikrotik-infra`](https://github.com/guntars-rakitko/mikrotik-infra) | Router, switches, WiFi, LTE, VLANs, firewall, DHCP/DNS |
| [`guntars-rakitko/truenas-infra`](https://github.com/guntars-rakitko/truenas-infra) | NAS storage (ZFS, SMB), MinIO, NUT server + UPS shutdown orchestrator, cluster-agent, wiki host (this repo). PXE retired 2026-09-23 |
| [`guntars-rakitko/bios-config`](https://github.com/guntars-rakitko/bios-config) | ASUS Q170S1 BIOS settings (AMT, PXE, power, security) |
| [`guntars-rakitko/wiki`](https://github.com/guntars-rakitko/wiki) | Internal MkDocs wiki at [wiki.w1.lv](https://wiki.w1.lv/) — mirrors docs from all above |

**Always read the CLAUDE.md of every related repo before making cross-cutting changes.** Common shared concerns:
- **IP plan / VLAN design** — canonical in `mikrotik-infra` (router is source of truth); referenced here
- **Hardware inventory** — each repo describes its own devices; update all when adding/removing
- **NUT / MinIO services** — live here on the NAS; referenced by `kube-infra`. (The PXE server that `bios-config` also used was retired 2026-09-23 — see § Planned Services.)
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
| `docs/{pxe-operator,talos-updater-setup,bios-apply-pxe-setup}.md` | _Auto-synced_ — ⚠ **retirement tombstones since PXE was retired 2026-09-23.** Delete them only together with their `wiki/sync-map.yaml` mappings, `.gitignore` lines and `.pages` entries in one coordinated wiki change: a deleted source with a live mapping makes `sync_repos.py` fail hard. `apps/pxe/` and `config/talos.yaml` are dead code awaiting the same removal (pool-rebuild plan Task 9a). |
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

**Running version: TrueNAS Community Edition 25.10.7** (codename "Goldeye"), upgraded
from 25.10.3.1 on 2026-09-13. This runs on the Beelink ME Mini 2. The Community Edition is the free SCALE-lineage successor that uses Docker (not K3s) for apps.

**Always check the latest TrueNAS version and API documentation before deploying or configuring anything.** Never rely on cached knowledge. Verify at:
- https://www.truenas.com/docs/
- https://www.truenas.com/docs/api/
- Release notes: https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/

When upgrading, update the pinned version here and re-verify all scripts against the new API surface.

### 25.10.3.1 → 25.10.7 (2026-09-13)

Taken for the **ZFS 2.3 → 2.3.9** fixes (silent read corruption after block cloning, data
loss on redacted replication send, zvol sync writes not reaching the ZIL) plus NFS server
crash fixes — both clusters mount NFS from here — and kernel 6.12.105.

⚠ **This is an elevated-risk operation on THIS hardware, not a routine patch.** The update
is a multi-GB download+write followed by a reboot, which is exactly the documented trigger
profile for the Beelink ME mini 3.3 V rail defect (see § NVMe 3.3V-rail mitigations): a
sudden write burst onto a cold array. The Jul-22 drop was caused by a 118 MB write. Do it
with physical power access — a warm reboot does NOT clear a hung NVMe controller; only a
full power-off does.

**Outcome: clean.** No drive dropped; all 6 NVMe present after reboot, `tank` ONLINE,
0 errors. API surface verified against 25.10.7 afterwards: `preflight` connects and
`users` / `network` / `shares` / `nut` / `apps` / `tunables` / `storage-tasks` all dry-run
`rc=0` with **zero `changed=True`** — i.e. the upgrade reverted none of our declared config.
Dependent services all returned on their own (minio-prd/dev, cluster-agent both VLANs, wiki,
PXE tftp+http), and both K8s clusters rode through it with Velero BackupStorageLocation
back to `Available` without intervention.

⚠ **Verify drives by SERIAL after any reboot, never by `nvmeN`.** This upgrade reshuffled
enumeration again — `S4GRNX1RB33857` and `S4GRNX0NA00357` swapped nvme1/nvme2. A
device-name comparison would have looked like a missing drive when nothing was wrong.

---

## Hardware

| Component | Detail |
|---|---|
| Device | Beelink ME Mini (post-RMA "v2" unit — 16 GB, no eMMC). ⚠ **The 3.3V-rail defect is NOT fixed on this unit.** Beelink's post-2025-09-08 inductor change was supposed to cure it; it did not — the NVMe drops recurred 2026-07-19 and 2026-07-22 (both the same drive `S4GRNX0NA00357` in slot 04, at ~03:00 UTC under the nightly write burst). Root cause is the shared 3.3V M.2 rail sagging under peak *simultaneous* NVMe current (community-corroborated; an external 90 W PSU + added regulator did not help upstream). Mitigations are software probability-reducers, NOT a cure — see § NVMe 3.3V-rail mitigations. Note: this unit has a **flat PCIe topology** (each NVMe on its own ADL-N PCH root port, no ASM2824 switch), so the ASM2824 link-training erratum does not apply here. |
| CPU | Intel N150 (4 E-cores, no HT) |
| RAM | 16 GB LPDDR5 (soldered; reads as 16.0 GB via `system.info` — earlier "12 GB" spec was wrong) |
| Storage | 6× M.2 NVMe slots, **4 populated since the 2026-09-23 rebuild** — 1× 256 GB PM981 boot (`nvme2n1`, S/N `S444NX0N496890`) + **3× 1 TB in RAIDZ1 tank**: `nvme0n1` Samsung 980 (`S649NF1R820750Y`), `nvme1n1` SM961 (`S34DNX0JA02364`), `nvme3n1` PM981a-H7 (`S4GSNF0N301379`). ⚠ **Enumeration changed in the rebuild** — boot was `nvme3n1`, is now `nvme2n1`, and the tank disks are NOT contiguous. Match by **serial**, never device name. ⚠ **Mixed capacity**: the 980 is 931.51 GiB vs 953.87 for the other two, and a raidz vdev sizes to its smallest member → ~45 GiB stranded, **1.75 TB usable**. Two slots now EMPTY, which is itself a rail mitigation (see below) |
| NIC1 | Intel **I226-V** 2.5G (`igc` driver, `enp1s0`, MAC `78:55:36:07:25:93`) — data, tagged trunk carrying VLANs 10 / 15 / 20 (sub-interfaces 10.10.10.10 / 10.10.15.10 / 10.10.20.10) |
| NIC2 | Intel **I226-V** 2.5G (`igc` driver, `enp2s0`, MAC `78:55:36:07:25:92`) — management, untagged VLAN 5 (10.10.5.10) |
| OS | TrueNAS Community Edition 25.10.7 (codename Goldeye) |

---

## Network

| Interface | VLAN | IP | Purpose |
|---|---|---|---|
| NIC1 — tagged sub-iface | 10 | 10.10.10.10 | Prod Kube: MinIO S3 (Velero + Longhorn + all backup buckets, :9000) |
| NIC1 — tagged sub-iface | 15 | 10.10.15.10 | Dev Kube: MinIO S3 (Velero + Longhorn + all backup buckets, :9000) |
| NIC1 — tagged sub-iface | 20 | 10.10.20.10 | Home: Plex, torrent UI, SMB general share |
| NIC2 — untagged | 5 | 10.10.5.10 | TrueNAS API/UI, SSH, NUT, MinIO consoles (:9001 prd / :9011 dev), wiki (:8088). PXE/TFTP retired 2026-09-23. (cluster-agent's `:9595` metrics bind on the data-VLAN IPs above, not here — `apps/cluster-agent/docker-compose.yaml`) |

Connected to CRS310:
- `ether7` — tagged trunk, VLANs 10/15/20 (data, NIC1)
- `ether8` — untagged, VLAN 5 (management, NIC2)

Service-to-interface binding is enforced in TrueNAS. Kube backup targets are MinIO S3, which binds only on the data-VLAN IPs (`.10.10` / `.15.10`); Plex + SMB bind only on `.20.10`. So home devices cannot reach Kube backup targets even though NIC1 is physically shared. Note on NFS: the old Longhorn NFS exports were **decommissioned 2026-04-27** (Longhorn → MinIO S3 `longhorn` bucket, kube-infra #26). ⚠ **As of 2026-09-23 NFS has NO shares at all.** `stress-results` was the last one and went with hw-validation in the pool rebuild. The service is still enabled and still binds all three sub-IPs (`config/shares.yaml § nfs.service.bindip`) — left that way deliberately, because turning NFS off is a separate decision that should be reviewable in its own change rather than implied by an empty share list.

---

## Planned Services

| Service | Purpose | Browser URL / endpoint |
|---|---|---|
| TrueNAS UI | NAS management | https://nas.w1.lv/ (10.10.5.10:443, direct) |
| MinIO prd console | S3 admin (prd) | https://minio-prd.w1.lv/ (via Traefik, backend on mgmt VLAN) |
| MinIO dev console | S3 admin (dev) | https://minio-dev.w1.lv/ (via Traefik, backend on mgmt VLAN) |
| MinIO prd S3 API | Cluster backup store (buckets: § setup-minio-buckets.sh) — CNPG Barman WAL+base (giks-db, w1-db), etcd snapshots, Pocket-ID Litestream, cluster-agent state, SMS-gateway dumps, plus `velero`/`longhorn` for the Q170S1 clusters only (until teardown), **+ the GIKS v1 MSSQL chain (`mssql-backups/box-prd`)** | https://s3-prd.w1.lv:9000 (10.10.10.10:9000, direct HTTPS) |
| MinIO dev S3 API | Cluster backup store (buckets: § setup-minio-buckets.sh) — CNPG Barman WAL+base (giks-db, w1-db), etcd snapshots, Pocket-ID Litestream, cluster-agent state, SMS-gateway dumps, plus `velero`/`longhorn` for the Q170S1 clusters only (until teardown) | https://s3-dev.w1.lv:9000 (10.10.15.10:9000, direct HTTPS) |
| cluster-agent | LLM-driven SRE assistant. **P3 Mode A daily digest** live since 2026-05-26: one LLM call per cluster per day at 06:00 EEST examining 24h of alerts + Loki log patterns → 0-N curated Findings filed as issues in [`kube-infra`](https://github.com/guntars-rakitko/kube-infra) (labels `cluster-agent` + `needs-review`); the daily digest-summary goes to [`cluster-agent-digest`](https://github.com/guntars-rakitko/cluster-agent-digest) (renamed from `-sandbox`; routing since the 2026-07-06 graduation — § cluster-agent ops). Runbook: `wiki/docs/runbooks/cluster-agent-runbook.md` | 10.10.10.10:9595/metrics (prd scrapes), 10.10.15.10:9595/metrics (dev scrapes) — data-VLAN per cluster, not mgmt |
| NUT server | UPS monitoring (1x APC Smart-UPS) | 10.10.5.10:3493 |
| SMB general share | Home file storage | 10.10.20.10 |
| Plex / Torrent | (deferred) | VLAN 20 |

> ⚠ **RETIRED 2026-09-23 with the pool rebuild** — MeshCentral, amtctl, PXE
> (server + directory index), homepage, iperf3 and stress-dashboard, plus the
> `stress-results` NFS export. **NFS now has no shares at all.**
> ⚠ MeshCentral and amtctl existed ONLY to KVM into the Q170S1 nodes over Intel
> AMT, and the MS-A2 has **no IPMI / BMC / vPro / AMT** — they are unusable on
> the new estate, not merely unused.
> ⚠ The Traefik dashboard row was ALREADY stale: every Traefik dashboard in the
> homelab was removed **2026-09-13**, so `traefik-nas.w1.lv` has not existed
> since then. It sat here for ten days and caused a permanent false failure in
> the verify matrix.
> Surviving apps: **minio-prd, minio-dev, traefik, wiki, cluster-agent.**

All browser-facing services serve a valid Let's Encrypt `*.w1.lv` cert.
See `docs/tls-runbook.md` for rotation + recovery.

### Policy for adding new services

Decision tree — **apply every time you add an HTTPS endpoint on this network**:

1. **Admin / mgmt UI a human opens in a browser?**
   → Expose through Traefik at `10.10.5.20:443`. Backend plain HTTP on
     mgmt-VLAN IP. Portless URL `<name>.w1.lv`. Add DNS record (via
     `mikrotik-infra/manage.sh` option 5, "Sync DNS static records") pointing at `10.10.5.20`.
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
for multi-instance (minio-prd, minio-dev), `<role>-<NN>` for per-box
(kub-prd-01). All lowercase, hyphen-separated.

---

## Storage Design

Live design — source of truth is `config/storage.yaml` (consumed by
`modules/{pool,datasets,storage_tasks}.py`). Summary:

- **Pool `tank`** — single **3-wide RAIDZ1** vdev (rebuilt 2026-09-23 from
  5-wide), `ashift=12`, **`autotrim=off`** (disabled 2026-07-22 — TRIM bursts
  spike 3.3V-rail current; see § NVMe 3.3V-rail mitigations; enforced by
  `pool.py::ensure_autotrim`). **1.75 TB usable.** The 256 GB PM981 is the
  TrueNAS-installer-managed `boot-pool`, not part of `tank`.
  ⚠ **ZFS cannot remove a device from a raidz vdev** (expansion landed in
  OpenZFS 2.3; shrinking never did) — 5→3 was destroy-and-recreate. Plan and
  full rationale: `docs/superpowers/plans/2026-09-23-nas-pool-rebuild.md`.
  ⚠ Going 5→3 is itself a **3.3V-rail mitigation**: it removes two drives'
  worth of peak *simultaneous* current, which is the documented root cause.
  Measured PS2 draw is now **10.09 W** across three drives (980 2.19 W +
  SM961 4.40 W + PM981a 3.50 W); the figures were measured per drive on
  2026-09-23, not taken from datasheets.
  ⚠ **Mixed models in one vdev is deliberate** — three firmware families and
  power curves, accepted because drive availability won over homogeneity.
  **If a drive fails here, RECORD WHICH ONE**: on a mixed vdev that
  attribution is the only thing that teaches anything.
  ⚠ When replacing a drive, match by **serial, not slot** — the 25.10.7
  upgrade swapped nvme1/nvme2 and the rebuild moved boot from nvme3 to nvme2.
  First scrub on the new vdev: `repaired 0B in 00:00:17 with 0 errors`.
  ⚠ That is NOT evidence the rail fault is resolved — 7 GB in 17 s with temps
  unmoved is nowhere near the forensics' **42-day** statistical bar.
- **Dataset defaults** — `compression=lz4`, `atime=off`, `xattr=sa`,
  `recordsize=128K` (the MinIO data datasets, misnamed `…/velero`, override to `1M`).
- **Dataset tree** (env-first): `tank/kube/{prd,dev}/velero` — ⚠ **MISNAMED**: each is
  MinIO's whole `/data` for that env and holds **every** bucket, not just Velero's; rename to
  `tank/kube/{prd,dev}/minio` is tracked in #152, after the MS-A2 cutover. (Longhorn
  datasets **removed 2026-04-27** — Longhorn → MinIO S3, kube-infra #26),
  `tank/media/{plex,torrent}`, `tank/shared/general`, and `tank/system/*`
  (apps-config/{nut,traefik,wiki}, tls). ⚠ `pxe` and `stress-results` were
  removed 2026-09-23 with their apps.
- **Snapshots** — per-env recursive tasks (prd 14 d / dev 7 d on
  `tank/kube/*`, media weekly ×4 w, shared 14 d, system 7 d).
- **Scrub** Sun 04:00; **SMART** short Sun 02:00 + long first-Sun 03:00.

---

## NVMe 3.3V-rail mitigations

The ME Mini's shared 3.3V M.2 rail sags under peak **simultaneous** NVMe
current and drops a drive off the PCIe bus (`CSTS=0xffffffff`). Confirmed a
platform fault, not the drives (SMART-clean; recurs across drives/slots;
post-RMA "v2" unit still affected; an external 90 W PSU didn't help upstream).
**Total wattage is not the limit — peak current is.** The mitigations below are
declarative and reconciled by the tooling; they are **probability-reducers, not
a cure**. Full incident forensics live in the operator's session notes.

Applied as code (all reversible), by layer:

| Mitigation | Where (source of truth) | Effect |
|---|---|---|
| `zfs_vdev_async_write_max_active=4` (was 10) | `config/tunables.yaml` § tunables → `tunables.py::ensure_tunables` | fewer concurrent NAND-program ops per leaf vdev |
| `zfs_vdev_scrub_max_active=2` (was 3) | same | gentler scrub/resilver current |
| `zfs_txg_timeout=1` (was 5) | same | smaller, more frequent txg write spikes |
| **NVMe PS2 power-state cap** (udev rule `90-nvme-ps2-cap`) | same (type `UDEV`) | caps each drive at its lowest *operational* state (PM981a 8.00→3.50 W, PM9A1 8.49→3.18 W) ≈ **59% less worst-case rail current**. Re-applies on every device `add` (incl. a PCI-rescan recovery, no reboot). `-s` save is rejected by these OEM Samsungs, hence the udev RUN rule. |
| **`autotrim=off`** | `config/storage.yaml` § pool → `pool.py::ensure_autotrim` | removes TRIM/deallocate current bursts on delete (the differentiator on the 2026-07-22 drop). Reclaim space with periodic manual `zpool trim tank`. |
| `nvme_core.default_ps_max_latency_us=0 pcie_aspm=off pcie_port_pm=off` | `config/tunables.yaml` § kernel | pre-existing APST/ASPM disable (kept; reboot-only) |

Deploy: `./manage.sh phase tunables --apply` (ZFS params live immediately;
udev rule fires on next device add / reboot / `udevadm trigger`) and
`./manage.sh phase pool --apply` (autotrim). Verify a drive's cap with
`sudo nvme get-feature /dev/nvmeX -f 0x02` → `Current value:0x00000002`.

**Not a durable fix.** Community evidence: every software mitigation eventually
fails on a full 6-drive DRAM-cached array. There is **no NVMe/power BIOS fix**
(current 16 GB branch = M1V404).

> ⭐ **Read [`docs/nvme-dropout-forensics.md`](docs/nvme-dropout-forensics.md)
> before acting on any of the above.** Kernel-log forensics (2026-08-02) found
> **two distinct failure modes**, which is why single-cause theories kept
> failing to predict the next incident:
> - **Mode A** (4 events, slots 04 + 07) — instant death, *zero* warning,
>   `CSTS=0xffffffff`, **no AER**. Power-like.
> - **Mode B** (1 event) — I/O-timeout cascade ending in
>   `Device not ready; aborting reset, CSTS=0x1`, i.e. the controller is
>   **alive and answering**. A **drive firmware hang**, not a power event.
>   Specific to PM9A1 `S6H2NF0WC37392`, whose fault followed it across a
>   physical reslot → that drive is faulty on evidence.
>   ⚠ Since 2026-09-23 that drive (and slot 04's `S4GRNX0NA00357`) has left
>   the NAS: `…392` is **msa2-dev's Postgres disk by operator decision**
>   (kube-infra msa2 plan D2, which reads the evidence the other way — the
>   rail, not the drive). Do not "retire the faulty drive" without reading
>   the forensics doc's § 2026-09-23.
>
> **Fixing one mode will not fix the other.** That doc also carries the
> recovery ladder (⚠ a warm reboot is **not** enough — the M.2 3.3 V rail
> stays energised, so a latched controller stays hung; use a full power-off),
> the `nvme-drop-capture` cron and how to triage it, the `journalctl` traps
> (`-k` implies `-b`; `truenas_admin` **cannot** join `adm`/`systemd-journal`),
> and a **42-day** statistical evidence bar.
>
> ⚠ **Buy replacement drives on lowest *operational* power state ≤ ~1.5–2 W**
> (`nvme id-ctrl -H` → PS descriptor table) — **not** on "DRAM-less", a rule
> the community's own data contradicts (DRAM-less P310 and 990 EVO Plus both
> appear on the failing side). Our fleet is 6 DRAM-cached OEM Samsungs.

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
which we use). Pinned (tag + digest) to `quay.io/minio/aistor/minio:RELEASE.2026-06-06T02-44-06Z`
in `apps/minio-{prd,dev}/docker-compose.yaml`. ⚠ This said
`RELEASE.2026-05-04T23-02-27Z` until 2026-09-13 — one release behind what the
compose files actually run. Re-read the compose file before quoting a version
here; this section is a mirror, not the source of truth.

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
./scripts/setup-minio-buckets.sh      # 10 buckets per instance (incl. the orphan loki-chunks)
./scripts/setup-minio-users.sh        # service user + readwrite policy
./scripts/setup-minio-lifecycle.sh    # ILM rules
./scripts/setup-minio-encryption.sh   # SSE-S3 default encryption (needs KMS — see script header)
```

All four are idempotent and safe to re-run.

#### setup-minio-buckets.sh

Creates the backup buckets on each MinIO instance — **ten** today, one of
them (`loki-chunks`) an orphan with no consumer (authoritative list = the
`BUCKETS` array in the script; its header still says "nine"):

| Bucket | Consumer |
|---|---|
| `cluster-agent` | cluster-agent — `state.db` nightly backups |
| `etcd-snapshots` | CronJob — `talosctl etcd snapshot` |
| `loki-chunks` | ⚠ **ORPHAN — no consumer.** Loki has kept chunks and index on its local filesystem PVC since 2026-05-26 and never touches S3 (kube-infra `flux-cd/infrastructure/helmreleases/loki.yaml` header). Still in `BUCKETS`, so every MinIO bootstrap recreates it (the 2026-09-23 rebuild did). Removal pending: drop it from `setup-minio-{buckets,encryption}.sh`, confirm the bucket is empty on both instances, then `mc rb`. |
| `longhorn` | Longhorn — volume + system backups (S3 BackupTarget; replaced the old NFS export 2026-04-27) |
| `mssql-backups` | **Legacy GIKS-v1 box** (docker-prd-01) — MSSQL `BACKUP DATABASE TO URL` targets. **Not a K8s-cluster track** — the cluster MSSQL was decommissioned 2026-06-17 (GIKS is on Postgres); only the v1 box still writes here. |
| `postgres-backups` | CloudNativePG — Barman Cloud Plugin WAL + base backups |
| `postgres-backups-w1` | CloudNativePG **w1-db** (web-tracker) — its own bucket, not a prefix. MinIO ILM is bucket-wide, so two retention windows need two buckets; and it makes a `serverName` typo fail into an empty bucket instead of silently landing in the financial chain's prefix. ⚠ An ILM boundary, **not** a credential one — the shared service user has `readwrite` on `s3:*`. |
| `pocket-id-litestream` | Pocket-ID — SQLite Litestream replicas (DR for the OIDC IdP) |
| `sms-gateway-backups` | SMS-gateway appliance — nightly `pg_dump` of the box's `smsgw`+`gammu` DBs (`box-<env>/` prefix) |
| `velero` | Velero — K8s manifest backups |

#### setup-minio-users.sh

Provisions the cluster's service user. **One user per cluster**,
shared across all cluster backup tracks (Velero / Longhorn / Postgres /
etcd-snapshots / …), `readwrite` policy.
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
removal), `pocket-id-secrets` (reads them on dev + msa2-dev only), `velero-minio` +
`longhorn-s3` on the Q170S1 clusters only (`velero-minio` also feeds their
etcd-snapshot CronJob), and `etcd-snapshot-minio` on msa2 only
(`mssql-backup-creds` went with the cluster MSSQL decommission, 2026-06-17).
This script reads the same Doppler keys via `doppler secrets get --plain` to
provision the MinIO user. Single canonical copy, zero drift,
no cross-repo coupling.

⚠ **`infrastructure/{dev,prd}` is shared by each Q170S1 cluster and its msa2
successor** (kube-infra `talos-os/estates.yaml` `doppler_env`; the msa2
per-cluster DopplerSecrets read the same configs). Once an msa2 cluster is
up, a `KUBE_MINIO_*` rotation reaches both it and the old cluster at once —
roll it as one change.

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
| `cluster-agent` (both clusters) | 30 days | cluster-agent `state.db` nightly backups — 30d of history is ample to recover the dedup/state DB; older copies are noise. |
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

Velero / Longhorn / pocket-id-litestream remain intentionally absent (and so
does the orphan `loki-chunks`, which nothing writes) — Litestream prunes its
own replicas, and **Velero and Longhorn must NOT be given an age-based backstop**: Longhorn
backups are incremental block chains where later backups reference blocks
written by earlier ones, so expiring a base by age corrupts every surviving
backup that depended on it, and Velero's TTL controller expects to own
deletion.

#### setup-minio-encryption.sh

Enables **SSE-S3 default encryption** on the buckets in its own `BUCKETS`
list, on both instances (GDPR at-rest encryption, kube-infra #520
Workstream C). ⚠ **That list is NOT the bucket list above:** it has eight
entries and omits `postgres-backups-w1` and `sms-gateway-backups` (the
latter holds the SMS-gateway's member/billing dumps). So this script has
never enabled default encryption on those two; whether they are encrypted
live is unchecked (`mc encrypt info nas-<env>/<bucket>`).
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
`Report` with 0-N actionable Findings. **Routing by issue type (2026-07-06
graduation, LIVE):** individual **Findings (all severities) file in
[`kube-infra`](https://github.com/guntars-rakitko/kube-infra)** — the ops
repo, human-close, labelled `cluster-agent` + `needs-review` (the review
queue); the daily **digest-summary** goes to
[`cluster-agent-digest`](https://github.com/guntars-rakitko/cluster-agent-digest)
(renamed from `-sandbox`, auto-supersedes yesterday's). Controlled by Doppler
`FINDINGS_REPO` / `DIGEST_REPO` (both fall back to `SANDBOX_REPO` in code);
`FINDINGS_MIN_SEVERITY` is an inert severity floor (empty = every finding
files). A finding created before the cutover keeps updating in its own repo
(dispatch comments on the repo embedded in the stored `gh_issue_ref`). Spec
+ rationale (why by-type, not wholesale-move or severity-bridge):
[`docs/superpowers/specs/2026-07-06-cluster-agent-digest-graduation.md`](docs/superpowers/specs/2026-07-06-cluster-agent-digest-graduation.md).
Full reference in `wiki/docs/runbooks/cluster-agent-runbook.md`.

> #### ⚠ The agent's K8s tokens EXPIRE, and expiry is invisible from the outside
>
> The `cluster-agent-readonly` SA tokens (one per cluster, in `flux-system`)
> are TokenRequest tokens with a finite lifetime. The originals were minted
> 2026-05-23 at `--duration=2160h` and **expired 2026-08-21 — the agent then
> produced nothing for 14 days and nobody noticed.** Mode A's first step
> (`alertmanager_history()`) reaches Prometheus/Alertmanager through the
> apiserver `services/proxy` endpoint with that token, so an expired token
> fails every run at step 1, before any LLM call.
>
> Nothing about the failure is visible from outside the process: the
> container stays up, `/health` returns `status: ok` with the scheduler
> arming the next run, and Prometheus keeps scraping successfully. The only
> evidence is `cluster_agent_run_total{status="error"}` climbing while no
> `status="success"` series exists at all.
>
> **Rotate with [`scripts/render-cluster-agent-kubeconfigs.sh`](scripts/render-cluster-agent-kubeconfigs.sh)**
> — it mints, verifies the *actually granted* expiry (never assume the
> requested duration was honoured), proves both the plain API read and the
> `services/proxy` path work, and with `--apply` writes Doppler and
> redeploys. Run it without `--apply` first.
>
> Since 2026-09-04 the silence is covered by `ClusterAgentNoSuccessfulRun`
> / `ClusterAgentRunsFailing` in kube-infra
> `flux-cd/infrastructure/configs/base/prometheus-rules-cluster-agent.yaml`.
> ⚠ Those rules are deliberately built on the run **counter**, not on
> `cluster_agent_last_success_timestamp` — that gauge has no samples in
> exactly this failure mode, so the obvious expression evaluates to an empty
> vector and never fires.
>
> #### ⚠ A Doppler change does NOT reach the container via `app.redeploy`
>
> Doppler is not read at container start. `manage.sh` **renders** the compose
> with secret values substituted and stores that rendered config in TrueNAS
> under `/mnt/.ix-apps/app_configs/cluster-agent/`. `midclt call app.redeploy`
> recreates the container from that **stored** config — so after a Doppler
> write it faithfully redeploys the *old* value.
>
> Measured 2026-09-05, and it cost a wasted rotation cycle: after writing new
> kubeconfigs to Doppler and running `app.redeploy`, a dry-run still reported
> `app_ensured action=update changed=True`. Only after
>
> ```sh
> ./manage.sh phase apps --only cluster-agent --apply
> ```
>
> did it report `changed=False`. Use that for any Doppler-sourced change;
> `app.redeploy` is fine only for a plain restart (e.g. picking up edited
> Python in the code bind-mount).
>
> ⚠ Also do not confirm a restart with `/health` alone — the old container
> keeps serving 200 while the redeploy is queued, so a poll returns "healthy"
> against the process you were trying to replace (observed: `/health` reported
> ok with `uptime_seconds=1061654`, the 12-day-old process, immediately after
> an apply). Wait for `process_start_time_seconds` in `/metrics` to change.
>
> It is a TrueNAS ix-app: there is no `/mnt/tank/cluster-agent` compose dir,
> and `truenas_admin` has no passwordless docker-socket access.

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
4. **Reconciles state.db against live GH issue state** (2026-07-16,
   `modes/daily_digest.py::_reconcile_finding_states`), THEN looks up the
   remaining open dedup_keys from state.db (LLM avoids semantic
   duplicates). The reconcile is load-bearing: finding issues are
   human-close-only, and nothing else teaches state.db that the operator
   closed one — without it, closed findings stay `state='open'` forever
   and the LLM keeps skipping the chronic condition as "already tracked"
   against a dead issue (the 2026-07 "digest cites closed issues as open"
   gap). Spec: `docs/superpowers/specs/2026-07-16-finding-state-reconcile.md`.
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
- `FINDINGS_REPO` / `DIGEST_REPO` / `FINDINGS_MIN_SEVERITY` — Mode A routing
  (2026-07-06 graduation): findings→`kube-infra`, summary→`cluster-agent-digest`,
  inert severity floor (empty = all). Both repos fall back to `SANDBOX_REPO`
  in code. All 3 are in `_DOPPLER_KEYS_PER_APP[cluster-agent]`, so they MUST
  exist in Doppler (empty is fine) or `manage.sh phase apps` fails loud.
- `SANDBOX_REPO` / `LLM_MODEL` — legacy fallback repo (redirects to
  `cluster-agent-digest` post-rename) + model name for `_MODEL_RATES_PER_1M`.
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
- `TRUENAS_NUT_MONPWD` — NUT `upsmon` (monitor) user password; written into upsd.users by TrueNAS at service start, and **required** — without it upsd exits silently (`config/services.yaml` § nut `monuser`). Used by the NAS's own upsmon master and by the in-cluster nut-exporter on every cluster — kub-dev and kub-prd, and the msa2 clusters declare the same (kube-infra `flux-cd/infrastructure/configs/base/nut-exporter.yaml`, `--nut.username=upsmon`). The K8s nodes are NOT NUT clients since 2026-06-02 (Path B). Role: read-only state queries, **no SET/INSTCMD**. ⚠ The clusters read the password from Doppler `infrastructure/shr` → `SHARED_TRUENAS_NUT_MONPWD` (DopplerSecret `nut-credentials`), a separate key that must equal this one — rotate both together, or make one a Doppler reference to the other.
- `TRUENAS_NUT_ADMINPWD` — NUT `upsadmin` user password (operator-side, used for `upsrw` + `upscmd` writes). Role: **SET + INSTCMD ALL**, configured via TrueNAS UI → Services → UPS → Edit → **"Extra Users"** field (pasted from the doctrine block in `wiki/docs/runbooks/ups-operations.md`). Live since 2026-05-28. Apple Passwords mirror: `TrueNAS NUT upsadmin`. Add to **NOPASSWD allowlist** when extending NUT automation — see SSH section below.
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

### SSH + sudo on NAS — what works without operator typing a password

Verified 2026-05-28. Useful to know up-front so you don't waste cycles
debugging "permission denied" responses:

**SSH login** — `ssh truenas_admin@10.10.5.10` works via SSH
**publickey** for any client whose key is in the user's authorized_keys.
The operator's laptop key (`gunrak@mac-giks-migration-20260419`
ED25519) is loaded. `BatchMode=yes` (which disables interactive
password fallback) succeeds → publickey is the only method the server
will accept for this user. Lands as `uid=950(truenas_admin)
groups=950(truenas_admin),544(builtin_administrators)`. `whoami`,
`hostname`, `id`, file reads in `/home/truenas_admin`, anything not
needing root — all work non-interactively.

**`sudo` from a non-interactive SSH session — REQUIRES password** by
default. `truenas_admin` has TrueNAS UI **"Allowed sudo commands: ALL"**
(broad sudoers entry) BUT **"Allowed Sudo Commands (No Password):"
empty** by default. So any `ssh truenas_admin@... 'sudo …'` without a
TTY (no `-t`) returns `sudo: a password is required`. Even `sudo -n`
(non-interactive) fails. To run sudo from automation, the operator
must add specific command paths to the "No Password" field via the
TrueNAS UI → Credentials → Users → `truenas_admin` → Edit.

**Granted NOPASSWD sudo commands** (in TrueNAS UI under "Allowed Sudo
Commands (No Password)"):

```
/usr/bin/docker restart cluster-agent
/usr/bin/docker exec cluster-agent *
/usr/bin/midclt
/usr/bin/upscmd
/usr/bin/upsrw
```

This lets passwordless:
- **Restart + exec into the cluster-agent container** (code-change
  deployment, manual digest fires, health checks).
- **midclt** — any TrueNAS API call from a non-TTY SSH session. Used
  for `ups.config` reads, `service.control`, etc. Without this,
  every API-driven check requires a TTY + password prompt.
- **upscmd / upsrw** — NUT instcmd + variable-write operations
  against the local UPS (e.g. `upscmd test.battery.start.quick`,
  `upsrw -s ups.delay.shutdown=300`). Added 2026-05-28 alongside
  the `upsadmin` NUT user — gives the operator's laptop end-to-end
  ability to tune UPS HID thresholds via Doppler-driven scripts
  without typing the upsadmin password each time.

Scope is intentionally tight — only the specific binaries listed,
nothing else. To extend (e.g. for a new app that needs the same
pattern), add a new line per command — never blanket
`/usr/bin/docker *` or wildcard everything.

**What this enables from automation / future Claude sessions:**

```sh
# Restart cluster-agent (after merging code-only changes — bind-mounted
# source is on disk but uvicorn caches the loaded module).
ssh truenas_admin@10.10.5.10 'sudo docker restart cluster-agent'

# Manual digest fire (verify end-to-end pipeline + costs ~$0.20):
ssh truenas_admin@10.10.5.10 \
  'sudo docker exec cluster-agent /venv/bin/python -c "
import asyncio
from cluster_agent.modes.daily_digest import run_async
print(asyncio.run(run_async(cluster=\"dev\")))
  "'

# Verify in-container code matches latest commit:
ssh truenas_admin@10.10.5.10 \
  'sudo docker exec cluster-agent grep -c "<expected-string>" /app/<file>'
```

**What is NOT granted** (still requires interactive password):

- `sudo` for any other docker container (minio-prd, minio-dev, plex, etc.)
- `sudo` for file ops outside docker (`vi`, `systemctl`, package install, etc.)
- `sudo` for inspecting other apps' source/data

For those, fall back to `ssh -t truenas_admin@... 'sudo …'` from the
operator's terminal — TTY lets sudo prompt for password.

**Future expansion pattern:** when adding a new app that needs
automation-driven docker ops, add a corresponding NOPASSWD line for
`/usr/bin/docker restart <app>` + `/usr/bin/docker exec <app> *`. Same
principle as the per-app Doppler key separation: minimum scope, no
wildcards.

**Per-app keys** (`_DOPPLER_KEYS_PER_APP` in `modules/apps.py`):

- `minio-prd` → `MINIO_ROOT_USER_PRD`, `MINIO_ROOT_PASSWORD_PRD`, `MINIO_KMS_SECRET_KEY_PRD`, `MINIO_AISTOR_LICENSE`
- `minio-dev` → `MINIO_ROOT_USER_DEV`, `MINIO_ROOT_PASSWORD_DEV`, `MINIO_KMS_SECRET_KEY_DEV`, `MINIO_AISTOR_LICENSE` (one shared, org-scoped license key)
- `cluster-agent` → its own Doppler project `cluster-agent/prd` (`_DOPPLER_PROJECT_PER_APP`), not `infrastructure/ops` — key list in § cluster-agent ops

⚠ The dict still carries `amtctl` and `homepage` entries. Both apps were
retired 2026-09-23 and are no longer in `config/apps.yaml`, so those entries
are never read — dead legacy awaiting removal from `modules/apps.py`.

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

## UPS / NUT (canonical home for cluster-wide UPS state)

> ✅ **Path B is LIVE + VALIDATED (kube-infra #611, 2026-06-02).** On a UPS
> low-battery event the **NAS drives the whole shutdown** via `talosctl`, not
> the old NUT-secondary FSD path. What's live + codified in `config/services.yaml`:
> (1) `ups.config.shutdowncmd` = `nas-ups-orchestrator.sh` — fires
> `talosctl shutdown --force` at all 6 nodes (os:operator cred) — plus the two
> MS-A2 boxes since 2026-09-26, ⚠ live only once re-staged (§ *MS-A2 in the
> fan-out* below) — polls them down, then halts the NAS **last**; (2) the **nut-client extension is removed from the
> nodes** (Talos schematic `daef782b`) → they're no longer NUT secondaries;
> (3) `ups.delay.shutdown` = **90**; (4) **`sdtype = 5`** (apcsmart hard hibernate
> `@`) so the #57-hook kill-power cuts + power-cycles **even on mains** (the
> default `S`/`shutdown.return` no-ops on mains). Drill 2026-06-02 (`upsmon -c fsd`):
> orchestrator instant-fired (0 secondaries, no HOSTSYNC wait), 6 nodes down in
> 187s, NAS last, UPS cut + 60s power-cycle, both clusters 3/3 + 0 faulted.
> Plan/drill-log: `kube-infra/docs/superpowers/plans/2026-06-01-ups-shutdown-orchestrator.md`.
> **Still owed:** a real on-battery AC-pull drill (battery-margin) before #611 closes.
> NOTE: some prose below is retained as HISTORY of how we got here.

**Hardware:** 2× APC Smart-UPS SMT750I/SMT750IC on the rack. The
primary UPS (`apc1`) protects the NAS + all 6 K8s nodes + networking
gear; the second UPS is reserved as a hot spare. ⚠ Whether the two MS-A2
boxes (msa2-dev, msa2-prd) are on `apc1` is **not recorded anywhere**
(2026-09-26). The design assumes they are: kube-infra
`docs/msa2-audit/02-design-decisions.md` § 10 gate 7 plans a real UPS drill to
"confirm both new boxes go down" and sizes the load as "~2×65–100 W + NAS 25 W
+ networking 70 W". The orchestrator is built to be safe either way (§ *MS-A2
in the fan-out*); record the answer here once someone looks. Battery replaced
**2026-06-13** (APC RBC7 pair + AP9620 swap, ~3-year expected lifespan;
next due ~2029).

**NUT topology:**

```
TrueNAS (this NAS, 10.10.5.10:3493)
  ├─ runs NUT MASTER (upsd + driver `apcsmart` → /dev/ttyUSB0, sdtype=5)
  │    apcsmart over the UPS DB-9 (LCC) via a USB→RS-232 adapter; gives
  │    the rich telemetry the dashboard needs (output.current, input
  │    min/max, frequency, temperature).
  ├─ upsmon (master mode) — on LB runs SHUTDOWNCMD = the Path-B orchestrator
  │    `/mnt/tank/system/talos/nas-ups-orchestrator.sh`:
  │      talosctl shutdown --force × every node (one call PER NODE) →
  │      poll apid until 0/N → halt NAS LAST
  │      (N = the Q170S1 nodes + each MS-A2 address that ACCEPTED;
  │       .11/.12 only if accepted once kub-prd is gone — rule (d))
  │  + #57 Init/Shutdown hook arms UPS kill-power (upsdrvctl shutdown → `@`)
  └─ upsd.users:
      upsadmin (SET + INSTCMD)  → operator scripts via NOPASSWD sudo
      upsmon   (read-only)       → the in-cluster nut-exporter on every cluster
                                   (kub-dev, kub-prd; msa2 declares the same)
                                   + the NAS's own upsmon master. ⚠ NOT safe
                                   to drop — upsd will not start without monpwd,
                                   and dropping it kills UPS telemetry + alerting
                                   on every cluster

K8s nodes (×6 Q170S1 + the MS-A2 boxes)
  └─ NOT NUT clients. The nut-client extension was stripped from the Talos
     image (schematic daef782b, 2026-06-02). They are shut down out-of-band
     by the NAS orchestrator via `talosctl shutdown --force` (os:operator
     creds staged at /mnt/tank/system/talos/{dev,prd,msa2-dev,msa2-prd}-
     shutdown.talosconfig; an msa2 one only once its Doppler key exists).
```

⚠ **`--force` is load-bearing for TWO reasons, not one (found 2026-07-29).**
The recorded rationale — "skips the drain that burns a 5-min `DrainTimeout`
once etcd quorum is lost" — is **incomplete**. That drain uses the Kubernetes
*eviction* API, and on **both** clusters every node hosts PDBs at
`ALLOWED DISRUPTIONS = 0` (single-replica Prometheus / Alertmanager / Grafana /
Loki / Pocket-ID, the CNPG `giks-primary`, and all three Longhorn
`instance-manager-*`). So the drain hangs to timeout **even on a fully healthy
cluster with quorum intact** — quorum loss is not required. Removing `--force`
would add ~5 min per node to a battery-constrained shutdown. Verified on Talos
v1.13.7: `shutdown --force` still exists, semantics unchanged.

⚠ **On the MS-A2 single-node clusters KEEP `--force` — but the reasons
NARROW, so do not re-derive it from the list above and conclude it is
unnecessary.** Longhorn is dropped, so the three `instance-manager-*` PDBs
vanish. The repo-owned PDBs (loki, pocket-id, coredns, and the giks/health app
PDBs) moved into kube-infra `flux-cd/legacy-q170s1/`, which only the Q170S1
clusters read (kube-infra plan Tasks D1b / D4-move), because `minAvailable: 1`
on a 1-replica workload blocks **every** eviction at n=1; the cloudflared PDB
is declared only in the Q170S1 per-cluster overlays. The **chart-level** PDBs
are gone too: open item 6 in
`kube-infra/flux-cd/clusters/msa2-{prd,dev}/infrastructure.yaml` is CLOSED by
kube-infra plan Task D2 (the shared HelmReleases render no PDB at n=1; the
three-node values moved to `legacy-q170s1/`). What is expected to remain is
CNPG's own `<cluster>-primary` PDB for `giks` and `w1` at `instances: 1` — the
plan deliberately keeps CNPG's PDBs (D4 fold-in M3: `enablePDB: false` NOT
adopted). ⚠ Unverified until `kubectl get pdb -A` on an msa2 cluster at
bring-up. Either way `--force` stays: at n=1 any blocking PDB makes the drain
unfinishable (a single-node drain was measured never to finish on msa2-dev
2026-09-24 — `talosctl upgrade`, left cordoned, no reboot; kube-infra plan Task
C1 Step 5). (Separately, and unrelated to this path: `talosctl upgrade`'s
`--preserve` flag was **deprecated — not removed** — in v1.13; it still parses
and exits 0 with a warning.)

**MS-A2 in the fan-out (2026-09-26; kube-infra plan § Cutover inventory row 15,
and the Part G decision "msa2 boxes are in no NAS UPS fan-out").** With PLP
priced out of the MS-A2 build, this is the only thing between a power cut and a
hard power-off of a single-instance Postgres on a non-PLP drive. The script's
header is the full reasoning; the rules below. ⚠ **Merged ≠ live:** the NAS
runs the copy staged under `/mnt/tank/system/talos/`, so until the re-stage
below has run it still fans out to the six Q170S1 nodes only.

| | Q170S1 (kub-prd, kub-dev) | MS-A2 (msa2-prd, msa2-dev) |
|---|---|---|
| addresses | `.11-.13`, `.14-.16` | **both** of each box's: BUILD then FINAL — prd `10.10.5.17` + `.11`, dev `10.10.5.18` + `.12` (kube-infra `talos-os/estates.yaml`, plan D12) |
| config | `{prd,dev}-shutdown.talosconfig` | `msa2-{prd,dev}-shutdown.talosconfig`, from `TALOS_NAS_SHUTDOWN_CONFIG_MSA2_{PRD,DEV}` (bootstrap.sh menu 10); a cluster with no staged config is **skipped** |
| call | `shutdown --force` (unchanged, byte for byte) | `shutdown --force --wait=false`, capped at 60 s by coreutils `timeout` |
| polled? | always, whatever the rc (unchanged) — except `.11`/`.12` once kub-prd is gone (rule d) | **only if the call returned 0** |

- **Why only-if-accepted.** apid's `:50000` probe is unauthenticated: a box in
  Talos maintenance mode, or one whose PKI no longer matches the staged config,
  answers it for ever, and the poll would burn the whole 300 s backstop on
  battery. A shutdown that returned non-zero was not delivered to a node of
  that cluster (no route / timeout / capped, rc 124 / x509 / maintenance mode),
  so nothing the script does would bring it down — waiting for it only drains
  the battery.
  `--wait=false` is what makes rc mean "delivered": rc=0 means talosctl's
  Version pre-check **and** the Shutdown RPC both succeeded, rc≠0 that the
  shutdown was not delivered. (In talosctl v1.14.0/v1.14.1 the `--wait=false`
  branch first runs `helpers.ClientVersionCheck`, a Version RPC, and returns
  before any Shutdown if it fails — so `os:operator` must keep Version access;
  the `--print-checks` `version` call proves that pre-flight for every pair.)
  With the default `--wait`, a tracker error after a delivered shutdown would
  also be rc≠0.
- **Why both addresses.** The address that is not the box's today is rejected
  (another cluster's CA, or no box at all), so it is a no-op — the script needs
  **no edit at either cutover or at a rollback**. Before prd's cutover
  `.11`/`.12` are kub-prd-01/-02 (polled via the Q170S1 list); after it, `.11`
  is msa2-prd, the old prd config is rejected there, msa2-prd's is accepted,
  and `.11` is polled once (the poll set is de-duplicated). Tests pin all of
  these: `tests/test_ups_orchestrator.py`.
- **Rule (d): a FINAL address follows kub-prd while it is live, and
  only-if-accepted once it is gone.** `.11` and `.12` are in `PRD_NODES` as
  well as in an MS-A2 list. While at least one kub-prd call returned 0 (before
  prd's cutover, and after a rollback) they are polled whatever their rc — the
  Q170S1 rule, unchanged. Once **no** kub-prd call returned 0 (after the
  cutover the old nodes' power cords are out, kube-infra plan D12) each is
  polled only if some call to it returned 0. Without it, from prd's cutover
  until its teardown an msa2 box at `.11`/`.12` that does not accept —
  maintenance mode, config not staged, a stale PKI — was polled to the 300 s
  backstop (LB fires at `battery.runtime.low` 420 s, so that is most of the
  reserve). That box is still not shut down, so re-stage after every menu 10
  and run the printed check all the same.
  ⚠ **Its one change to the live Q170S1 path** — an operator decision, and the
  last commit on its own so it can be dropped: if **every** kub-prd call fails
  while the old nodes are up, `.11`/`.12` are no longer waited for. For a
  broken prd config that changes nothing in practice (`.13` still runs to the
  backstop); for a `--wait` tracker error on all three after the shutdown was
  delivered, the NAS can halt while `.11`/`.12` are still going down — gated
  by `.13` and kub-dev, which shut down in parallel, and followed by `apc1`'s
  90 s `ups.delay.shutdown`. A single failing kub-prd call changes nothing.
  Tests pin both sides (`test_final_address_*`, `test_every_kub_prd_call_*`,
  `test_tracker_error_on_every_kub_prd_call_*`).
- **The cap bounds a hung msa2 call; it does not hide it.** The poll, and so
  the NAS halt, starts only after every shutdown call has returned, so an msa2
  address that connects and then hangs delays it by up to 65 s (60 s + the 5 s
  `--kill-after`). The ~187 s Q170S1 calls hide that today; after the cutovers
  they fail fast and the cap is the delay. 60 s is deliberate: a cap that fires
  on a slow but delivered shutdown reads as rc 124, keeps that box out of the
  poll, and lets the NAS halt (and `apc1` cut power 90 s later) while it is
  still stopping Postgres. An absent box fails in ~3 s and a silently-dropping
  one in ~20 s on their own. A test pins the production default.
- **The Q170S1 path is deliberately unchanged**, including its known weakness
  (a Q170S1 node that rejects its shutdown is still polled to the backstop) —
  it is live until each cutover and the rollback target after it.
- **Is msa2 on `apc1`? Unknown — safe either way** (the design assumes yes: see
  *Hardware* above). On `apc1`: shut down cleanly, and `apc1`'s kill-power
  cycle plus BIOS *AC power loss: Always On* boots it again. Off `apc1` in a
  real outage: already dark, its call fails fast and a dark box reads as down.
  Off `apc1` but still powered (a drill on mains, or a second UPS): shut down
  cleanly and **stays off** until powered on by hand, because its AC never
  drops — an availability cost, never a data one. ⚠ Either way **every Drill A
  (`upsmon -c fsd`) now takes the msa2 clusters down too**: power-cycled back
  up if they are on `apc1`, left off if not. Plan the drill for both.
- **Re-stage** (main session; staging is non-disruptive — `shutdowncmd` is not
  touched). The script uploads whatever **this checkout** holds, so bring
  `main` up to date first — a stale checkout re-stages the old orchestrator
  and every credential check still passes:
  ```sh
  cd ~/github/truenas-infra && git switch main && git pull --ff-only && git log -1 --oneline
  doppler run -p infrastructure -c ops -- ./scripts/setup-talos-shutdown-orchestrator.sh
  ./scripts/setup-talos-shutdown-orchestrator.sh --print-checks   # just the check, any time
  ```
  Then paste the one `ssh -t …` command it prints. Its first line is the
  sha256 of the **staged** orchestrator, which must equal the value printed
  with it (this checkout's): that, not the credential checks, proves the NAS
  runs the merged script. Re-stage after every `bootstrap.sh msa2-<env>` menu
  10 (it re-mints the msa2 config), and after msa2-prd is built (Task H) so
  its config is staged. An msa2 key missing from Doppler is a WARN, not an
  error: that cluster is skipped until the next re-stage.
- **At cutover:** nothing in the orchestrator. Row 15's remaining items stand:
  re-stage `talosctl` at the msa2 version (`TALOSCTL_VERSION`, v1.14.0 today —
  same minor as msa2's v1.14.1), run the printed check, and **re-measure the
  shutdown time** in a drill rather than carrying 187 s.
- **At teardown — a code change with its own PR and tests, not a two-line
  deletion.** Under `set -u` a leftover reference to a deleted list aborts the
  orchestrator **before** it fires the msa2 shutdowns or halts the NAS, and the
  setup script's `check_pairs` fails on a list it cannot read. prd's teardown
  comes first (before dev's cutover); per torn-down cluster `<X>` (`PRD`, then
  `DEV`) remove:
  - `nas-ups-orchestrator.sh`: `<X>_NODES`, `<X>_CFG`, its fire loop, its
    `poll_q170s1 <x>` line, its part of `Q170S1_NODES`, and that msa2
    cluster's BUILD address (`MSA2_<X>_NODES` keeps only the FINAL one). Once
    `PRD_NODES` is gone, `.11`/`.12` are MS-A2-only and rule (a) covers them
    directly;
  - `setup-talos-shutdown-orchestrator.sh`: `<X>` in `check_pairs`'s loop, its
    required-key line (`TALOS_NAS_SHUTDOWN_CONFIG_<X>`), its `base64 -d` line,
    its entry in the initial `CFGS`, and the header lines naming it;
  - `tests/test_ups_orchestrator.py`: that cluster's Q170S1 expectations.
  After the second teardown delete what is left of the Q170S1 block —
  `Q170S1_NODES`, the FINAL-address branch of the rejection log, `live`,
  `poll_q170s1`, the Q170S1 and rule-(d) tests. Run the suite,
  re-stage, run the printed check. The staged `{dev,prd}-shutdown.talosconfig`
  are then unused; the setup script never deletes a file, so remove them from
  the NAS by hand.

⚠ **The staged `talosctl` does NOT track the node version — re-verify after
every Talos upgrade.** `/mnt/tank/system/talos/talosctl` is its own pinned binary.
Same-minor compatibility was **empirically confirmed** on 2026-07-29 (a v1.13.2
client plus the real `os:operator` credential authenticating to a v1.13.7 node),
but a major/minor gap would break the UPS shutdown path **silently**, surfacing
only during a real outage.

> ⚠ **Re-staged 2026-09-23, version not re-measured since.** On 2026-09-22 the
> staged binary was **v1.13.2** against nodes on **v1.14.0** — a full-minor gap.
> The 2026-09-23 pool rebuild destroyed `/mnt/tank/system/talos/` and the whole
> path was re-staged with `setup-talos-shutdown-orchestrator.sh` (pool-rebuild
> plan Task 7), whose `TALOSCTL_VERSION` default is **v1.14.0** — same minor as
> the old estate (v1.14.0) and msa2 (v1.14.1). ⚠ Confirm with the check below
> (and `talosctl version --client` on the NAS) before relying on it; if it still
> reports v1.13.2, the gap is real — re-stage.
> ⚠ msa2 has its own PKI, so it has its own `os:operator` configs
> (`TALOS_NAS_SHUTDOWN_CONFIG_MSA2_{DEV,PRD}`). Since the 2026-09-26 change
> they are staged alongside the Q170S1 pair (once re-staged) and the
> orchestrator targets both of each box's addresses, so **no orchestrator edit
> is due at cutover** — only the talosctl bump in kube-infra cutover row 15
> (§ *MS-A2 in the fan-out*).
>
> ✅ **The CREDENTIALS are fine** — read from Doppler 2026-09-22 they are valid
> `Jun 1 2026 → May 29 2036`. A suspicion that they had expired came from
> `setup-talos-shutdown-orchestrator.sh` documenting `--crt-ttl 720h` (30 days),
> which is NOT what was actually minted. ⚠ That comment was a landmine — the
> MS-A2 rebuild mints a new PKI and forces a re-issue, and anyone following it
> would have created a 30-day credential with **no rotation and no alert**
> (verified: nothing in this repo rotates it, no kube-infra `prometheus-rules-*`
> watches it). Corrected 2026-09-22 to 87600h, with the reasoning written down.

One-line check after any node upgrade (the configs are 0600 root, so it needs
`sudo` — and a TTY, because `talosctl` is not on the NOPASSWD allowlist):

```sh
ssh -t truenas_admin@nas.w1.lv 'sudo /mnt/tank/system/talos/talosctl \
  --talosconfig /mnt/tank/system/talos/dev-shutdown.talosconfig \
  -n 10.10.5.14 --endpoints 10.10.5.14 version --short'   # expect Server: v1.14.x
```

To check **every** (config, address) pair the orchestrator targets in one ssh
and one sudo prompt, run `./scripts/setup-talos-shutdown-orchestrator.sh
--print-checks` on the laptop (no credentials needed) and paste the command it
prints; it reads the pairs from the orchestrator's own inventory and explains
which failures are the designed exclusions.

**Two NUT users — role separation:**

| User | Password key | Perms | Used by | Where defined |
|---|---|---|---|---|
| `upsmon` | `TRUENAS_NUT_MONPWD` (clusters: `infrastructure/shr` `SHARED_TRUENAS_NUT_MONPWD`, must match) | monitor (read-only) | in-cluster nut-exporter (kub-dev, kub-prd; the msa2 clusters declare the same) + the local upsmon master | TrueNAS UI → Services → UPS → Edit → **Monitor User/Password** fields (managed via `ups.config` API; appears in upsd.users at service start) |
| `upsadmin` | `TRUENAS_NUT_ADMINPWD` | `actions = SET`, `instcmds = ALL` | Operator's `upsrw`/`upscmd` invocations | TrueNAS UI → Services → UPS → Edit → **Extra Users** field (`ups.config.extrausers`). Live since 2026-05-28. |

Operator never uses `upsmon` for writes — `upsmon`'s purpose is read-only
telemetry for the nut-exporters and the local master; the upsadmin user keeps
that scope clean.

**UPS HID thresholds (live on UPS firmware, not in NUT config):**

These are stored INSIDE the UPS hardware. A UPS firmware reset or unit
swap silently reverts to APC defaults. Codified in
`config/services.yaml § nut.ups_thresholds` (and reconciled by
`modules/nut.py:ensure_ups_hid_thresholds`):

| Variable | Value | Unit | Why |
|---|---|---|---|
| `ups.delay.shutdown` | **90** | seconds | Time UPS waits after the #57-hook kill-power before killing outputs. Trimmed 450→90 on 2026-06-02 — the Path-B orchestrator confirms every polled node is down (poll 0/N) BEFORE the NAS halts+arms, so this only has to cover the NAS's own final poweroff. ENUM valid: 090/180/.../630/000; 090 is the floor. Writable (`upsrw`, zero-padded). |
| `ups.delay.start` | **60** (1 min) | seconds | Delay before UPS re-enables outputs after utility returns. apcsmart ENUM — write the zero-padded `"060"` (bare `60` → `ERR INVALID-VALUE`). Avoids the boot-shutdown-boot loop on flaky grids. |
| **`sdtype`** | **5** | enum | apcsmart kill-power METHOD: hard hibernate (`@`) ALWAYS, regardless of line state. In `ups.config.options`. The default (0) is status-dependent (`S` on battery / `@` on mains) and the #57 hook's transient `upsdrvctl` can't read status → used `S` → no-op on mains. `5` forces `@` → UPS cuts + auto-returns even on MAINS (validated 2026-06-02). The lever that closed the mains-mid-drain gap. |
| `battery.runtime.low` | **420** (7 min) | seconds | LB trigger (secondary). **READ-ONLY on firmware** — enforced via driver `override.battery.runtime.low = 420` in `ups.config.options`. Lowered 600→420 on 2026-08-01 (#112): the apcsmart runtime estimate jitters 480–1260 s on mains, so 600 fired false `UPSBatteryLow` alerts at full charge. |
| `battery.charge.low` | **60** | percent | LB trigger. **READ-ONLY on firmware** — enforced via driver `override.battery.charge.low = 60` + `ignorelb` in `ups.config.options`. Lowered 75→60 on 2026-06-01 — node shutdown is fast (~187s via the orchestrator) so 60% leaves ample reserve + restores short-outage ride-through. |
| `battery.charge.warning` | 50 | percent | WARN-level notification at 50% (logs only, no shutdown). Default — kept as early-warning signal. |
| TrueNAS `powerdown` | `true` | boolean | Set 2026-05-28. (Path B no longer relies on TrueNAS's own `powerdown` killpower — the #57 Init/Shutdown hook does the kill-power via `upsdrvctl shutdown`/sdtype=5. Left enabled, harmless.) |
| TrueNAS `shutdown` | `LOWBATT` | enum (BATT/LOWBATT) | Use LB as the shutdown trigger, not raw OB. Gives the cluster the full battery runtime envelope before initiating shutdown. |
| TrueNAS `shutdowntimer` | 30 (was 60) | seconds | Grace after LB fires before TrueNAS calls SHUTDOWNCMD on itself. Set 30 on 2026-05-30 to match the DR flow. NB: LOWBATT mode may shut down on the LB signal regardless of this timer — confirm in Drill A. |

**DR shutdown bug (#57) — UPS keeps draining after a real outage:**

Symptom (operator-confirmed 2026-05-30): on a power-loss DR, the cluster
+ NAS shut down on Low-Battery as designed, but the **UPS never cuts its
own outputs** — it keeps powering the dead load until the battery is flat,
then delivers a second uncontrolled outage when utility returns.

Root cause: the apcsmart driver reaches the UPS over `/dev/ttyUSB0`, a
**USB→RS-232 adapter**. During NAS poweroff the kernel removes the USB
device *before* TrueNAS's `powerdown` killpower step runs, so the
`shutdown.return` command is never delivered. (This is the long-standing
"USB teardown" failure mode — it applies to apcsmart-over-USB-serial just
as much as to usbhid-ups, because the transport is still USB.)

`ups.delay.shutdown` / `delay.start` are NOT the problem — the UPS never
gets the command that would start the countdown.

**Attempt 1 (FAILED, 2026-05-30):** arm `shutdown.return` from upsmon's
`SHUTDOWNCMD` via `ups.config.shutdowncmd`. Live drill on dev: nodes + NAS
shut down gracefully, but the **UPS was never armed and drained flat** —
exact bug #57. Retired this approach.

**Why it failed + the right mechanism (NUT issue #2587, systemd `nutshutdown`
hook):** kill-power must run as a *shutdown-time* hook once the NUT driver has
released the USB device — not at FSD/SHUTDOWNCMD time. `upsdrvctl shutdown`
fails "Can't claim USB device" while the driver still holds the port; and
TrueNAS's read-only `/usr/lib/systemd/system-shutdown/` blocks the standard
`nutshutdown` hook.

**Attempt 2 (current — VALIDATED 2026-05-31):** a TrueNAS **Init/Shutdown
Script** (when=SHUTDOWN) that ARMS the UPS during poweroff; `shutdowncmd` stays
cleared so TrueNAS keeps owning the host poweroff. A `upsmon -c fsd` drill
confirmed the UPS cuts power + power-cycles the rack.
- `scripts/nas-ups-shutdown.sh` — arm-only hook: `upscmd shutdown.return`
  (works while upsd is up — driver relays it), fallback `upsdrvctl stop` +
  `upsdrvctl shutdown` (driver released → can claim USB). Logs to
  `/mnt/tank/system/nut/last-shutdown.log` (world-readable — diagnosable
  without journal access, which truenas_admin lacks). Does NOT poweroff.
  **POWERDOWNFLAG guard:** `when=SHUTDOWN` scripts run on EVERY shutdown+reboot,
  so the hook first runs `upsmon -K` and only arms the UPS when the flag is set
  (genuine FSD/low-battery shutdown). A routine reboot/poweroff → flag unset →
  it logs and exits, so it does NOT power-cycle the rack on normal maintenance.
- `scripts/setup-ups-shutdown-hook.sh` — installer (API-based: uploads hook +
  root-only pw file, registers the SHUTDOWN init/shutdown script). ⚠ Since
  Path B it does **NOT** touch `shutdowncmd` (script header) — the Attempt-2
  version cleared it, which would now break the orchestrator. Init/shutdown
  scripts persist in the config DB across reboots + updates, but NOT across a
  TrueNAS reinstall (`docs/recovery.md` § 4).
- `config/services.yaml § nut.shutdowncmd` — ~~**keep empty**~~
  **⚠ SUPERSEDED by Path B (kube-infra #611, 2026-06-02 — see the banner
  at the top of this §).** `shutdowncmd` is now **SET** to
  `/mnt/tank/system/talos/nas-ups-orchestrator.sh` (verify:
  `config/services.yaml` § nut.shutdowncmd). **Do NOT clear it** — an empty
  `shutdowncmd` would break the Path-B DR orchestration. The Attempt-1/
  Attempt-2 prose above is retained only as history of the pre-Path-B
  design (when the Init/Shutdown hook armed the UPS and `shutdowncmd`
  was deliberately empty); the Init/Shutdown hook still arms the UPS
  kill-power, but it now runs *alongside* the orchestrator, not instead
  of a `shutdowncmd`.

Re-validate after changes with the LIGHT drill: `sudo upsmon -c fsd` (sets the
flag → arms) → read `last-shutdown.log`. A plain `reboot` must NOT arm it
(flag unset) — that's the guard's job.

**Severity reality check:** data is NEVER at risk — the graceful shutdown
chain works. Bug #57 only means the battery fully drains + no clean
auto-recovery if mains returns mid-drain. So "accept + document" is a valid
fallback. Other options if init/shutdown also fails: native RS-232 (no USB —
needs hardware the Beelink lacks), or revert apcsmart→usbhid-ups (trades the
rich Grafana telemetry for the standard kill-power path).

**HISTORY — superseded:** charge.low 75→60 (2026-06-01), runtime.low 840→600
(#611) →420 (2026-08-01, #112), delay.shutdown 450→90 (2026-06-02). Current
values are in the table above.
**Thresholds (2026-05-31):** LB `override.battery.charge.low` 50→**75** and
`override.battery.runtime.low` 600→**840** (shutdown starts with ~14 min
reserve, after the drill left the battery at ~5%). `ups.delay.shutdown` was
raised 540→630 then **trimmed to 450** (7.5 min) once node shutdown was fixed
(see below) — it no longer needs slow-node margin. LB triggers (75% / 840s)
left aggressive for reserve. Trade-off: LB at 75% gives up short-outage
ride-through sooner (a ~4-5 min outage triggers shutdown; still rides routine
~90s blips).

**2026-05-31 (history) — slow Talos node shutdown (kube-infra #611).** Root
cause: Talos's shutdown sequence ran an API-dependent pod *drain*
(`CordonAndDrainNode`) that burned a hardcoded 5-min `DrainTimeout` once etcd
quorum was lost — the ~6-8 min "stuck loop" that drained the battery. An
interim node-side NUT `SHUTDOWNCMD=/sbin/poweroff --force` cut node shutdown to
~3.2 min. **Superseded 2026-06-02 by Path B** — the nodes are no longer NUT
clients (kube-infra `talos-os/patches/general.yaml` § NUT removal note); the NAS
orchestrator runs `talosctl shutdown --force` instead (banner at the top of this
§). charge.low was lowered 75→60 on 2026-06-01.

**Service-restart gotcha:** `midclt call service.control RESTART ups` can
leave the service **STOPPED** on TrueNAS 25.10 (observed 2026-05-30 after a
`shutdowntimer` update) — silently losing UPS monitoring. Use STOP + START +
verify instead (`service.query … state == RUNNING`, then `upsc apc1@localhost
ups.status` should answer `OL`, not "stale"). The battery-swap runbook and
`scripts/setup-ups-shutdown-hook.sh` both follow this pattern.

**UPS alert notifications (kube-infra #611, 2026-05-31).** TrueNAS UPS event
emails run through its alert framework (NOT NUT `NOTIFYFLAG`): each event is an
alert class with a level + policy, and the **E-Mail alert service emits at
WARNING and above**. Tuned classes (declared in `config/services.yaml §
nut.alertclasses`, applied via `midclt call alertclasses.update` — NOT
auto-reconciled, re-apply after a NAS rebuild):

| Class | Level | Policy | Why |
|---|---|---|---|
| `UPSOnBattery` | CRITICAL (default) | IMMEDIATELY | power lost → emails. Kept. |
| `UPSOnline` | INFO→**WARNING** | IMMEDIATELY | power restored. Was INFO (silent — below the email threshold) → bumped so "power back" notifies. |
| `UPSBatteryLow` | ALERT | IMMEDIATELY→**HOURLY** | flapped per threshold-crossing during post-outage recharge (email spam) → throttled to ≤1/hr; still signals a genuine low battery. |

Other UPS* classes (Commbad/Commok/Replbatt) kept at defaults. The recharge
flapping was a side-effect of the earlier 75%/840s LB triggers — the battery
dwelt in the LB zone for a while while recharging — and HOURLY caps the email
spam. The triggers have since been lowered (charge.low 60, runtime.low 420 —
see the table above).

**Battery health verification:**
- Last manual quick test: **2026-06-13 (Done and passed)** — establishes baseline for the new RBC7 pair.
- Automatic test interval: APC firmware default (14 days). Attempted to override to monthly via `ups.test.interval` 2026-05-28 — firmware rejected, kept default.
- Manual test command (zero-risk, ~30s):
  ```sh
  export ADMIN_PWD=$(doppler secrets get TRUENAS_NUT_ADMINPWD --project infrastructure --config ops --plain)
  ssh truenas_admin@10.10.5.10 "sudo upscmd -u upsadmin -p '$ADMIN_PWD' apc1@localhost test.battery.start.quick"
  sleep 35
  ssh truenas_admin@10.10.5.10 "upsc apc1@localhost ups.test.result"
  # Expect: "Done and passed"
  ```

**Full operations runbook:** [`wiki/docs/runbooks/ups-operations.md`](https://wiki.w1.lv/runbooks/ups-operations/) — covers all UPS reads/writes, battery replacement procedure, drill A (`shutdown.return`) and drill B (real utility-loss simulation).

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
  render-cluster-agent-kubeconfigs.sh         # rotate the agent's SA tokens
                                              # (mint + verify granted expiry
                                              #  + prove services/proxy works)
```

---

## Related Repos

See the **Related Repositories** section at the top of this file for the full cross-repo map.
