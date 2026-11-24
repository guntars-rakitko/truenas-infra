# CLAUDE.md — truenas-infra

Rules and a map for Claude Code in this repo. The facts live in `docs/`. This file was slimmed on 2026-11-22
(truenas-infra#233); the full old file is `git show 51daa2d:CLAUDE.md`.

## What this repo is

API-driven TrueNAS configuration for a homelab NAS. Almost all configuration is stored in Git and applied through the TrueNAS API (JSON-RPC 2.0 over WebSocket), so the NAS can be rebuilt from scripts. **Two things are deliberately managed out-of-band in the UI** and are NOT reproduced by the tool: (1) the `truenas_admin` operator identity itself (its login + the "Allowed Sudo Commands (No Password)" allowlist that the automation depends on — see [secrets.md](docs/reference/secrets.md) § SSH + sudo on NAS), and (2) a small set of NUT bits (Extra Users, alert classes, the Init/Shutdown hook registration; [ups-nut.md](docs/reference/ups-nut.md)). A clean rebuild therefore re-runs the phases **and** re-applies those UI-managed pieces from their documented recipes.

The NAS serves the Kubernetes clusters defined in `guntars-rakitko/kube-infra` and sits on the network managed by `guntars-rakitko/mikrotik-infra`.
This repo owns the NAS, MinIO and UPS/NUT facts (workspace `CLAUDE.md` § Who owns which facts).

⚠ **This repo is PUBLIC**, the only public repo in the workspace. Never commit a secret, a credential, a key or token
value, or member data, not even in a comment, a test or a doc. Doppler holds the values; this repo names only the keys.

## Map: where each old section is now

`CLAUDE.md § <old name>` references in code, comments and other repos still resolve here. Every old heading, one row
each, is in [docs/README.md](docs/README.md).

| Old section(s) | Now in |
|---|---|
| § Overview | this file, § What this repo is |
| § Related Repositories, § Related Repos | deleted: the workspace `CLAUDE.md` owns the repo list |
| § Wiki maintenance | [docs/reference/wiki-sync.md](docs/reference/wiki-sync.md) |
| § Version Policy, § The REST API is removed in 26.0 | [docs/reference/versions.md](docs/reference/versions.md) |
| § 25.10.3.1 → 25.10.7, the nvme1/nvme2 swap (cited as § Version Policy) | [docs/history/upgrade-25.10.7.md](docs/history/upgrade-25.10.7.md) |
| § Hardware | [docs/reference/hardware.md](docs/reference/hardware.md) |
| § Network | [docs/reference/network.md](docs/reference/network.md) |
| § Planned Services (the retired-services note: [docs/history/retired-services.md](docs/history/retired-services.md)) | [docs/reference/services.md](docs/reference/services.md) |
| § Policy for adding new services, the hostname convention | [docs/runbooks/add-a-service.md](docs/runbooks/add-a-service.md) |
| § Storage Design | [docs/reference/storage.md](docs/reference/storage.md) |
| § NVMe 3.3V-rail mitigations | [docs/reference/nvme-rail.md](docs/reference/nvme-rail.md) |
| § API Configuration Approach | [docs/reference/api.md](docs/reference/api.md) |
| § Object store: MinIO AIStor Free, § MinIO bucket internals, § setup-minio-buckets.sh, § setup-minio-users.sh, § setup-minio-lifecycle.sh, § setup-minio-encryption.sh | [docs/reference/minio.md](docs/reference/minio.md) |
| § cluster-agent | [docs/history/cluster-agent.md](docs/history/cluster-agent.md) |
| § Secrets — Doppler `infrastructure/ops`, § SSH + sudo on NAS, the per-app keys, the admin account | [docs/reference/secrets.md](docs/reference/secrets.md) |
| § UPS / NUT, *The fan-out*, *Re-stage*, the HID thresholds, bug #57 (its superseded thresholds: [docs/history/ups-nut.md](docs/history/ups-nut.md)) | [docs/reference/ups-nut.md](docs/reference/ups-nut.md) |
| § File Structure | this file, § Layout |

## Hard rules

- **Check the latest TrueNAS version and API docs before deploying or configuring anything**, never cached knowledge.
  Running version and links: [versions.md](docs/reference/versions.md). Nothing here may call the REST API
  (`/api/v2.0`, removed in 26.0); the re-check before an upgrade is in the same file.
- **Phases are idempotent and dry-run by default**; `--apply` writes the NAS. Credentials come from Doppler
  `infrastructure/ops` in-process: no `.env` on disk ([api.md](docs/reference/api.md)).
- **Match drives by serial, never by `nvmeN`**: enumeration reshuffles across reboots, upgrades and rebuilds
  ([hardware.md](docs/reference/hardware.md), [storage.md](docs/reference/storage.md)).
- **NVMe dropouts: read [nvme-dropout-forensics.md](docs/nvme-dropout-forensics.md) before acting.** A warm reboot
  is not enough, because the M.2 rail stays energised. Its § Recovery procedure gives the order: PCI remove and
  rescan, then a full power-off, then a reseat. The rail mitigations reduce the odds and are not a cure
  ([nvme-rail.md](docs/reference/nvme-rail.md)). So an upgrade or another write burst is an elevated-risk operation,
  done with physical power access ([upgrade-25.10.7.md](docs/history/upgrade-25.10.7.md)).
- **MinIO: no upgrade to RELEASE.2026-09-19 or later on the Free tier.** It ends the Free tier's encryption, and PUTs
  into encrypted buckets fail. Renovate holds it behind approval; the way forward is a decision
  ([minio.md](docs/reference/minio.md)). AIStor needs its licence file even on the Free tier; naming stays `minio-*`.
- **No age-based ILM rule for a bucket whose client owns deletion** (`pocket-id-litestream`, `loki-chunks`), and none
  for `archive`. `tests/test_minio_setup_scripts.py` guards the bucket, encryption and lifecycle lists
  ([minio.md](docs/reference/minio.md)).
- **Exposing a service:** an admin UI goes through Traefik at `10.10.5.20` (a route in `apps/traefik/routes.yaml`), a
  data-plane API binds on its own VLAN IP at its native port (one with no TLS of its own through a Traefik entry point
  on that IP that no admin UI uses: `devapi`), an infra-plane UI is never proxied, and nothing
  internet-facing goes through mgmt Traefik ([add-a-service.md](docs/runbooks/add-a-service.md)).
- **A new DNS record goes in mikrotik-infra `configs/dns.yaml` only**, plus its wiki `hostnames.md` row. This repo has
  no DNS file since 2026-09-26 ([wiki-sync.md](docs/reference/wiki-sync.md)).
- **Cert rotation redeploys only the apps in `TLS_CONSUMERS`** (today `traefik`). An app that mounts the TLS dir is in
  that list or on the test's exemption list (`tests/test_tls_rotate.py`; [services.md](docs/reference/services.md),
  [tls-runbook.md](docs/tls-runbook.md)).
- **`TRUENAS_NUT_MONPWD` is a MASTER credential, not a read-only one** (`upsmon master` includes FSD, which shuts the
  site down). Only the NAS's own upsmon holds it: never put it in `shr`, a cluster or a remote client. The nut-exporters
  read anonymously; a remote client that needs a login gets its own `nut.extra_users` entry
  ([secrets.md](docs/reference/secrets.md), [ups-nut.md](docs/reference/ups-nut.md)).
- **The NOPASSWD sudo allowlist stays tight**: one line per command, scoped to one binary (and one app), never a
  blanket wildcard such as `/usr/bin/docker *`. `midclt call user.update`
  prints password hashes, so its output goes to `>/dev/null` ([secrets.md](docs/reference/secrets.md)).
- **UPS orchestrator: merged ≠ live.** The NAS runs the copy staged under `/mnt/tank/system/talos/`. Re-stage with
  `scripts/setup-talos-shutdown-orchestrator.sh` from an up-to-date `main`, and check that the staged sha256 equals the
  checkout's. Re-stage after every orchestrator merge, every kube-infra `bootstrap.sh` menu 10 and every Talos upgrade
  (the staged `talosctl` is pinned separately) ([ups-nut.md](docs/reference/ups-nut.md) § *The fan-out*).
- **Never clear `ups.config.shutdowncmd`**: it runs the orchestrator (Path B). `talosctl shutdown --force` is
  load-bearing ([ups-nut.md](docs/reference/ups-nut.md)).
- **Never restart the UPS service with `service.control RESTART ups`**: it can leave it STOPPED. Use STOP, then START,
  then verify `RUNNING` and `upsc apc1@localhost ups.status` = `OL` ([ups-nut.md](docs/reference/ups-nut.md)).
- **UPS HID thresholds live in the UPS firmware**, codified in `config/services.yaml` § nut. A firmware reset or a unit
  swap reverts them, and the alert classes are not auto-reconciled: re-apply both after a swap or rebuild
  ([ups-nut.md](docs/reference/ups-nut.md)).
- **Secrets:** Doppler `infrastructure/ops` is the store; Apple Passwords mirrors the operator-typed break-glass
  ones. Never print a secret value ([secrets.md](docs/reference/secrets.md)).

## Workflow

- Branch work in `~/github/_worktrees/truenas-infra/<topic>` (the workspace `/worktree`), then a PR into `main`.
  `main` is squash-only and requires `agent-review`; merge with `/ship`. There is no CI workflow: run the tests
  yourself (`uv run --extra dev pytest`, plus `ruff` and `mypy` for `src/`, from the same `dev` extra).
- A phase change: run `./manage.sh preflight` and `./manage.sh phase <name>` (a dry run) before the PR, and put the
  diff in its test plan. `--apply` runs after the merge, from an up-to-date `main`, in the main session (workspace
  rule: subagents are read-only on the NAS). Then run `./manage.sh phase verify`.
- Changing what a wiki page reproduces: edit the wiki page in the same commit set, then
  `cd ~/github/wiki && ./tools/deploy.sh --verify` ([wiki-sync.md](docs/reference/wiki-sync.md) has the table).

## Before you touch X, read Y

| Touching | Read first |
|---|---|
| `scripts/nas-ups-*.sh`, `scripts/setup-talos-shutdown-orchestrator.sh`, `scripts/setup-ups-shutdown-hook.sh`, `config/services.yaml` § nut, `modules/nut.py` | [ups-nut.md](docs/reference/ups-nut.md) |
| `apps/minio-*/`, `scripts/setup-minio-*.sh`, the MinIO rule in `renovate.json` | [minio.md](docs/reference/minio.md) |
| `config/storage.yaml`, `config/tunables.yaml`, `modules/{pool,datasets,storage_tasks,tunables}.py`, any drive work | [storage.md](docs/reference/storage.md), [nvme-rail.md](docs/reference/nvme-rail.md), [nvme-dropout-forensics.md](docs/nvme-dropout-forensics.md) |
| `apps/traefik/`, `apps/tls/`, `config/tls.yaml`, a new app in `config/apps.yaml` | [add-a-service.md](docs/runbooks/add-a-service.md), [services.md](docs/reference/services.md), [tls-runbook.md](docs/tls-runbook.md) |
| `config/network.yaml`, `config/shares.yaml` | [network.md](docs/reference/network.md), [wiki-sync.md](docs/reference/wiki-sync.md) |
| Doppler keys, `modules/apps.py` per-app keys, the sudo allowlist | [secrets.md](docs/reference/secrets.md) |
| A TrueNAS upgrade | [versions.md](docs/reference/versions.md), [upgrade-25.10.7.md](docs/history/upgrade-25.10.7.md), [nvme-rail.md](docs/reference/nvme-rail.md) |
| Lost management access, a rebuild | [recovery.md](docs/recovery.md), [runbook.md](docs/runbook.md), `bootstrap/01-bootstrap-notes.md` |

## Layout

```
manage.sh             # entry point; fetches its Doppler keys at startup
config/               # the declared NAS state, one file per domain (apps.yaml: the app registry)
src/truenas_infra/    # the Python CLI; modules/ holds one module per phase
apps/                 # per-app compose files and configs; apps/tls/ the cert rotation
scripts/              # MinIO bootstrap (setup-minio-*.sh), the UPS hook and orchestrator, nvme-drop-capture
tests/                # pytest
docs/                 # reference/, runbooks/, history/, plus the wiki-mapped runbooks at docs/*.md
```

## Where new content goes

Current state goes in the matching `docs/reference/` file, procedures in `docs/runbooks/`, and history in
`docs/history/` or git. None of it goes back into this file. Most top-level `docs/*.md` files (`runbook.md`, `recovery.md`, …;
not the index `docs/README.md`) are mapped into the wiki one by one, in the wiki's `sync-map.yaml`. Delete or rename a
mapped one only together with its wiki mapping, and merge the wiki side first
([wiki-sync.md](docs/reference/wiki-sync.md)).

## Verification bar

A change is done when the tests pass and the phase's dry run shows only the intended `changed=True` lines. After
`--apply`, a repeat dry run shows zero `changed=True` and `./manage.sh phase verify` passes
([verification.md](docs/verification.md)). An orchestrator change is done only once it is re-staged and the sha256
matches.
