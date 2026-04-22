# amtctl

Intel AMT power-control + status dashboard for the 6 ASUS Q170S1 K8s
nodes. FastAPI sidecar polls each node's ME firmware via WS-MAN every
60s, caches results in memory, serves:

- **`https://amtctl.w1.lv/`** — static HTML dashboard (per-node panel
  with System / Hardware / Network / Storage + action buttons)
- **`https://amtctl.w1.lv/api/...`** — JSON API consumed by the
  dashboard itself and by Homepage widgets at `https://home.w1.lv/`

## Files

| Path | Role |
|---|---|
| `amt.py` | **Symlink** → `../../../bios-config/amt/amt.py`. Single source of truth for the WS-MAN client; bios-config owns it since it's shared between this dashboard and the fleet-provisioning tool under `bios-config/tools/`. |
| `main.py` | FastAPI app: background poller + endpoints + static UI serve |
| `web/index.html` | Single-file dashboard (vanilla JS + CSS, no build) |
| `nodes.yaml` | Node inventory — 6 `{name, host, role}` entries. Shared with `bios-config` AMT tools (cross-repo read). |
| `secrets.sops.yaml` | AMT admin creds (SOPS-encrypted). Shared with `bios-config` AMT tools. |
| `docker-compose.yaml` | python:3.13-alpine + runtime venv bootstrap |
| `Dockerfile` | Reference (not used — compose does runtime install) |

## What AMT gives us

Confirmed working on Q170S1 + ME 11.8.65:

- Power state (on / soft-off / hard-off / unreachable)
- Full CPU SKU string (`Intel(R) Core(TM) i7-7700T CPU @ 2.90GHz`)
- BIOS + ME firmware version
- Memory modules (per-DIMM: size, bank, manufacturer, part number)
- Network: IP, MAC, subnet mask, gateway, DNS, link state
- Power actions: On / Off (graceful + hard) / Reset, with optional
  one-time boot override (PXE / BIOS Setup / normal)

## What AMT does NOT give us (Q170S1 hardware limitation)

- Temperature / fan RPM / sensors (`CIM_NumericSensor` empty on this
  hardware)
- Live CPU frequency (only base clock from BIOS sticker)
- Per-drive model + serial (only aggregate `MaxMediaSize` via
  `CIM_MediaAccessDevice` — ~381 GB number with no breakdown). To
  match MeshCentral's per-drive inventory view we'd need to implement
  AMT's **legacy Asset SOAP interface** (separate binding from WS-MAN,
  ~100 LOC of new client code).

## Planned: Talos metrics integration

Once the cluster is up (track in `kube-infra/CLAUDE.md` → "Planned
post-bringup work"), this app gains a new endpoint
`/api/nodes/{name}/metrics` that queries Prometheus (scraped from
node-exporter on every node) for:

- CPU % utilization
- Memory % used
- CPU package temperature (`node_hwmon_temp_celsius`)
- Fan RPM (where Q170S1 board reports)
- SMART disk temp + health

The UI renders those rows under each node card alongside the existing
AMT-derived data. When a node is offline, graceful degradation keeps
the AMT-only view working.

See kube-infra CLAUDE.md for the full plan + prerequisites.

## Fleet AMT provisioning (lives in bios-config)

The provisioning side of AMT configuration — declaring canonical
state + a tool to assert it — lives in the sibling `bios-config`
repo, alongside the analogous BIOS NVRAM flow. This dashboard stays
here (it's a NAS-deployed monitoring service); the fleet tools ship
with bios-config because they're "set the hardware to a canonical
state" tools, which is bios-config's whole thesis.

See: `~/Documents/github/bios-config/tools/amt_fleet_{audit,apply}.py`
and `~/Documents/github/bios-config/amt/canonical.yaml`.

The two repos share:
- `amt.py` — lives in bios-config, symlinked into this directory.
- `nodes.yaml` — lives here (dashboard is the primary owner of the
  fleet inventory); bios-config's tools read it cross-repo.
- `secrets.sops.yaml` — lives here with the SOPS age keys;
  bios-config's tools shell out to `sops -d` against this path.

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

## Operator notes

**Add a node:** edit `nodes.yaml`, `./manage.sh phase apps --apply`
(uploads to `/mnt/tank/system/apps-config/amtctl/config/nodes.yaml`),
restart the app (`app.stop` + `app.start` — NOT `app.redeploy`).

**Change AMT creds:** `sops apps/amtctl/secrets.sops.yaml` →
`./manage.sh phase apps --apply` → delete + re-apply to refresh env.

**Debug a probe failure:** hit
`https://amtctl.w1.lv/api/nodes/<name>` for the full JSON dump
including `errors` array. Common AMT quirk: `PT10S` timeouts on some
classes — the client uses `PT60S` but if a node is truly unreachable
it times out there.

**Power-on BIOS gotcha:** if a node accepts AMT power-on but doesn't
physically wake, check BIOS "After AC Power Loss" (must be `Power On`,
not `Always Off`) and "Wake from ME" (must be `Enabled`). Seen on
kub-prd-03 after a CPU swap — BIOS had reverted that setting.

## Security note

AMT admin password is shared across all 6 nodes, stored SOPS-encrypted
in `secrets.sops.yaml`. Rotate via MEBx (Ctrl-P during node POST) when
needed; update this file + re-apply.
