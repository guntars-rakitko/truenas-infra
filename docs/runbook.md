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
./manage.sh phase tls --apply
./manage.sh phase pool          # DRY RUN — review the disk list
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
