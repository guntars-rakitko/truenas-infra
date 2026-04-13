# CLAUDE.md — truenas-infra

Guidance for Claude Code when working in this repository.

---

## Overview

API-driven TrueNAS configuration for a homelab NAS. All configuration is stored in Git and applied via the TrueNAS REST API — no manual UI changes. This ensures the NAS can be fully rebuilt from scripts if needed.

The NAS serves the Kubernetes clusters defined in `guntars-rakitko/kube-infra` and sits on the network managed by `guntars-rakitko/mikrotik-infra`.

---

## Version Policy

**Always check the latest TrueNAS version and API documentation before deploying or configuring anything.** Never rely on cached knowledge. Verify at:
- https://www.truenas.com/docs/
- https://www.truenas.com/docs/api/

---

## Hardware

| Component | Detail |
|---|---|
| Device | Beelink ME Mini 2 |
| CPU | Intel N150 |
| RAM | 12 GB |
| Storage | 6x NVMe slots (3-4x 1TB planned) |
| NIC1 | 2.5G — management (VLAN 5, 10.10.5.10) |
| NIC2 | 2.5G — traffic (VLAN 10, 10.10.10.10) |
| OS | TrueNAS |

---

## Network

| Interface | VLAN | IP | Purpose |
|---|---|---|---|
| NIC1 (mgmt) | 5 | 10.10.5.10 | API access, SSH, web UI |
| NIC2 (traffic) | 10 | 10.10.10.10 | NFS, Longhorn backups, MinIO (Velero) |

Connected to CRS310 traffic switch: ether8 (mgmt, VLAN 5) and ether7 (traffic, VLAN 10).

---

## Planned Services

| Service | Purpose | Access |
|---|---|---|
| NFS server | Longhorn backup target, shared storage | VLAN 10 (10.10.10.10) |
| MinIO | S3-compatible backend for Velero cluster backups | VLAN 10 |
| PXE server | netboot.xyz + Talos OS images for bare-metal boot | VLAN 10 |
| Plex | Media server | VLAN 20 (home) |
| Torrent client | Downloads | VLAN 20 (home) |

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

| Repo | Relationship |
|---|---|
| `guntars-rakitko/kube-infra` | K8s clusters that depend on this NAS (Longhorn backups, Velero, PXE) |
| `guntars-rakitko/mikrotik-infra` | Network config — NAS connected via CRS310 ether7/ether8 |
