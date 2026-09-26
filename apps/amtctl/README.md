# amtctl — RETIRED 2026-09-23; only `nodes.yaml` remains

The amtctl app (an Intel AMT power-control and status dashboard for the six
ASUS Q170S1 K8s nodes, at `amtctl.w1.lv`) was retired with the NAS pool
rebuild in truenas-infra#149: it is not in `config/apps.yaml`, and its compose
file, FastAPI app, web UI and the `amt.py` symlink were deleted afterwards.
The MS-A2 boxes that replace the Q170S1 nodes have no IPMI, BMC, vPro or AMT
at all, so it cannot come back on the new estate.

⚠ **This directory stays, holding `nodes.yaml`, because another repo reads
it.** `bios-config`'s four AMT tools (`tools/amt_fleet_audit.py`,
`amt_fleet_apply.py`, `amt_audit_passwords.py`, `amt_config_discovery.py`)
resolve `../truenas-infra/apps/amtctl/` through the sibling-clone convention:
three read the fleet inventory from `nodes.yaml`, and all of them exit if the
directory is missing. bios-config's `CLAUDE.md` says the same: do not delete
it until the inventory has moved there. Once it has, delete this directory.

Nothing in this repo reads `nodes.yaml` any more.

## Fleet AMT provisioning (lives in bios-config)

The provisioning side of AMT configuration — declaring canonical
state + a tool to assert it — lives in the sibling `bios-config`
repo, alongside the analogous BIOS NVRAM flow. The fleet tools ship
with bios-config because they're "set the hardware to a canonical
state" tools, which is bios-config's whole thesis.

See: `~/github/bios-config/tools/amt_fleet_{audit,apply}.py`
and `~/github/bios-config/amt/canonical.yaml`.

What the two repos share now:
- `nodes.yaml` — lives here; bios-config's tools read it cross-repo.
- AMT admin creds — Doppler `infrastructure/ops` → `AMT_USER` +
  `AMT_PASSWORD`, read by bios-config at runtime. Nothing in this repo
  reads them any more.

Discovery findings (2026-04-22 session; see bios-config for details):

- `HostOSFQDN` is **not remotely writable** via WS-MAN — only
  LMS-writable. Talos has no LMS agent, so whatever ME has latched
  stays latched. prd-01 has a stale `dev-srv-03.w1.lv` from a prior
  OS install; harmless because effective AMT FQDN resolves via
  `HostName + DomainName + SharedFQDN=true`. `put_singleton` has
  read-back verification that flags silent refusals with ⚠.
- AMT clocks across the fleet were 10-22 years off (prd-01 at 2004,
  rest at 2016). Every apply run force-syncs via
  `SetHighAccuracyTimeSynch`.

## Q170S1 notes still worth keeping (rollback window)

**Power-on BIOS gotcha:** if a node accepts AMT power-on but doesn't
physically wake, check BIOS "After AC Power Loss" (must be `Power On`,
not `Always Off`) and "Wake from ME" (must be `Enabled`). Seen on
kub-prd-03 after a CPU swap — BIOS had reverted that setting.

**AMT admin password** is shared across all 6 nodes; canonical value lives
in Doppler `infrastructure/ops` → `AMT_PASSWORD`. Rotate via MEBx
(Ctrl-P during node POST) when needed, then update the Doppler key.

The full former README (dashboard, API, operator notes):
`git show 3244103:apps/amtctl/README.md`.
