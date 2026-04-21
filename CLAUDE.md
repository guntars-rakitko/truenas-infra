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

**Always read the CLAUDE.md of every related repo before making cross-cutting changes.** Common shared concerns:
- **IP plan / VLAN design** — canonical in `mikrotik-infra` (router is source of truth); referenced here
- **Hardware inventory** — each repo describes its own devices; update all when adding/removing
- **PXE / NUT / MinIO services** — live here on the NAS; referenced by `kube-infra` and `bios-config`
- **Secrets** — same age key across all repos (see SOPS section)

Local clones live at `/Users/gunrak/Documents/github/{kube-infra,mikrotik-infra,truenas-infra,bios-config}`.

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
| Device | Beelink ME Mini |
| CPU | Intel N150 |
| RAM | 12 GB |
| Storage | 6x NVMe slots (3-4x 1TB planned) |
| NIC1 | 2.5G — data, tagged trunk carrying VLANs 10 / 15 / 20 (sub-interfaces 10.10.10.10 / 10.10.15.10 / 10.10.20.10) |
| NIC2 | 2.5G — management, untagged VLAN 5 (10.10.5.10) |
| OS | TrueNAS 25.10.3 (Community Edition) |

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

| Service | Purpose | Bound to |
|---|---|---|
| NFS (prd) | Longhorn backup target for the prd Kube cluster | VLAN 10 (10.10.10.10) |
| NFS (dev) | Longhorn backup target for the dev Kube cluster | VLAN 15 (10.10.15.10) |
| MinIO (prd) | S3 backend for Velero prd cluster backups | VLAN 10 (10.10.10.10) |
| MinIO (dev) | S3 backend for Velero dev cluster backups | VLAN 15 (10.10.15.10) |
| PXE / TFTP server | netboot.xyz + Talos OS images for bare-metal boot (all 6 Kube nodes) | VLAN 5 (10.10.5.10) |
| NUT server | UPS monitoring (2x APC Smart-UPS); all Kube nodes run NUT client for graceful shutdown | VLAN 5 (10.10.5.10) |
| Plex | Media server | VLAN 20 (10.10.20.10) |
| Torrent client | Downloads | VLAN 20 (10.10.20.10) |
| SMB general share | Home file storage | VLAN 20 (10.10.20.10) |

---

## Storage Design (TBD)

To be defined during Phase 1 hardware setup. Expected:
- ZFS pool across available NVMe drives
- Datasets for: Longhorn backups, Velero/MinIO, Plex media, general storage
- Snapshot schedule on critical datasets

---

## API Configuration Approach

All configuration is applied via TrueNAS REST API using scripts in `scripts/`. The `.env` file provides API credentials.

**Pattern:**
1. Scripts read `.env` for `TRUENAS_HOST` and `TRUENAS_API_KEY`
2. Each script targets a specific domain (pools, shares, apps, network)
3. Scripts are idempotent — safe to re-run
4. A top-level `configure.sh` runs all scripts in order (or provides an interactive menu)

---

## Secrets — SOPS + age

Credentials are encrypted with SOPS + age and committed as `.env.sops`. The plaintext `.env` is gitignored.

- **Age private key location:** `~/Library/Application Support/sops/age/keys.txt`
- **Encrypt:** `sops encrypt -i .env.sops`
- **Decrypt to stdout:** `sops decrypt .env.sops`
- **Edit in place:** `sops .env.sops` (opens in `$EDITOR`, re-encrypts on save)

Same age key used across all infra repos (kube-infra, mikrotik-infra, truenas-infra).

---

## File Structure

```
CLAUDE.md
.sops.yaml            # SOPS encryption rules (age public key)
.env                  # Plaintext credentials (not in Git, .gitignore'd)
.env.sops             # SOPS-encrypted credentials (safe to commit)
.env.example          # Template showing required variables
scripts/
  (TBD — will contain API configuration scripts as requirements are defined)
configure.sh          # Interactive menu or orchestrator (TBD)
```

---

## Related Repos

See the **Related Repositories** section at the top of this file for the full cross-repo map.
