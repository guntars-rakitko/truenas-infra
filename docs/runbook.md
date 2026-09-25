# Runbook

Operator guide for `truenas-infra`. Step-by-step execution order after
bootstrap.

## Before the first `manage.sh` invocation

Complete `bootstrap/01-bootstrap-notes.md`. That's the only manual
piece; everything else is scripted.

## Normal bring-up

```bash
./manage.sh preflight           # sanity check: API reachable, auth works
./manage.sh phase users --apply
./manage.sh phase network       # DRY RUN first
./manage.sh phase network --apply
./manage.sh phase tunables      # DRY RUN — NVMe 3.3V-rail mitigations
./manage.sh phase tunables --apply
# REBOOT here so the kernel args (nvme_core.default_ps_max_latency_us=0
# pcie_aspm=off pcie_port_pm=off) and the PS2 udev cap take effect, then on
# EACH NVMe drive, BEFORE phase pool:
#   sudo nvme get-feature /dev/nvmeX -f 0x02   → Current value:0x00000002
./manage.sh phase tls --apply
./manage.sh phase pool          # DRY RUN — review the disk list (match by SERIAL)
./manage.sh phase pool --apply --confirm=CREATE-TANK
./manage.sh phase datasets --apply
./manage.sh phase storage-tasks --apply
./manage.sh phase shares --apply
./manage.sh phase nut --apply
./manage.sh phase apps --apply
./manage.sh phase verify
```

Always dry-run first; every phase computes and logs a diff of what it
would change. Only pass `--apply` after reading the diff.

⚠ **`phase tunables` + the reboot + the PS2 check are mandatory before pool
creation** — pool creation is the highest-current moment for the ME Mini's
shared 3.3 V M.2 rail, and these are the mitigations for its drive dropouts.
See CLAUDE.md § NVMe 3.3V-rail mitigations (and the 2026-09-23 pool-rebuild
plan, which made this a gate).

## Safety flags

- **`--dry-run`** (default) — no writes; logs intended calls.
- **`--apply`** — actually change state.
- **`--only <item>`** — run one sub-item of a phase (e.g. `--only minio-prd` in `phase apps`).
- **`--confirm=CREATE-TANK`** — required to run `phase pool` against an
  empty NAS. If `tank` already exists, phase pool is a no-op.

## When a phase fails

- Logs are in `logs/truenas-infra-<timestamp>.log` (JSON lines).
- The phase exits non-zero; the CLI has not touched anything past the
  failure point (ordering inside each module is linear).
- Re-run is safe: every `ensure_*` is idempotent and will pick up where
  the previous attempt stopped.

## Recovery if management access is lost

See `docs/recovery.md`.
