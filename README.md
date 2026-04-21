# truenas-infra

API-driven TrueNAS Community Edition 25.10.3 configuration for the homelab NAS.

All configuration lives in Git and is applied via the TrueNAS JSON-RPC API
(WebSocket). No manual UI changes after bootstrap — the NAS can be rebuilt
end-to-end from these scripts.

## Quick start

```bash
# 1. One-time manual steps (API key, static IP, SSH) — see bootstrap/.
cat bootstrap/01-bootstrap-notes.md

# 2. Put the API key into the encrypted env file.
sops .env.sops

# 3. Bootstrap venv + run preflight.
./manage.sh preflight

# 4. Run a phase (dry-run by default).
./manage.sh phase network
./manage.sh phase network --apply
```

## Documentation

- `CLAUDE.md` — architecture, hardware, network layout, cross-repo contract.
- `docs/plans/zesty-drifting-castle.md` (in `~/.claude/plans/`) — approved bring-up plan.
- `docs/superpowers/specs/` — decision records (storage topology, etc.).
- `docs/runbook.md` — operator guide.
- `docs/verification.md` — post-apply check commands.
- `docs/recovery.md` — what to do if management access is lost.
- `bootstrap/01-bootstrap-notes.md` — the manual UI steps done once before scripts can run.

## Phases

```
./manage.sh list
```

| # | Phase | Purpose |
|---|---|---|
| 1 | `users` | Local users, SSH keys, email alerts |
| 2 | `network` | VLAN sub-interfaces on NIC1 |
| 3 | `tls` | Internal CA + ACME DNS-01 cert |
| 4 | `pool` | RAIDZ1 across 6× NVMe (one-shot, gated) |
| 5 | `datasets` | Nested dataset tree |
| 6 | `storage-tasks` | SMART, scrub, snapshots |
| 7 | `shares` | NFS (prd/dev) + SMB (home) |
| 8 | `nut` | Built-in UPS service |
| 9 | `apps` | netboot-xyz, minio-prd, minio-dev |
| 10 | `verify` | Run the verification matrix |

## Related repositories

| Repo | Scope |
|---|---|
| [kube-infra](https://github.com/guntars-rakitko/kube-infra) | Talos + Kube clusters, Flux, workloads |
| [mikrotik-infra](https://github.com/guntars-rakitko/mikrotik-infra) | Router/switch/VLAN/firewall config |
| [bios-config](https://github.com/guntars-rakitko/bios-config) | ASUS Q170S1 BIOS for Kube nodes |
