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
| `amt.py` | Async WS-MAN client (httpx + digest auth + SOAP/XML) — also reused by `../../tools/amt_fleet_{audit,apply}.py` |
| `main.py` | FastAPI app: background poller + endpoints + static UI serve |
| `web/index.html` | Single-file dashboard (vanilla JS + CSS, no build) |
| `nodes.yaml` | Node inventory — 6 `{name, host, role}` entries |
| `canonical.yaml` | Fleet-wide AMT configuration (applied by `amt_fleet_apply.py`) |
| `secrets.sops.yaml` | AMT admin creds (SOPS-encrypted) |
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

## Fleet AMT provisioning (canonical state)

Two laptop-run tools under `../../tools/` manage fleet-wide AMT
configuration — like `bios-config` but for AMT/ME settings instead of
BIOS NVRAM.

- **`tools/amt_fleet_audit.py`** — read-only diff across all 6 nodes.
  Shows drift from the canonical in `canonical.yaml`.
- **`tools/amt_fleet_apply.py`** — asserts canonical state. Idempotent
  (Put with read-modify-write + read-back verification). Use `--dry-run`
  to preview; `--apply` to write; `--node <name>` to target one box.

```sh
cd ~/Documents/github/truenas-infra
# Preview fleet drift
uv run --python 3.11 --with httpx --with anyio --with pyyaml \
    python tools/amt_fleet_apply.py --dry-run

# Apply canonical across all 6 nodes
uv run --python 3.11 --with httpx --with anyio --with pyyaml \
    python tools/amt_fleet_apply.py --apply
```

What `canonical.yaml` manages:

| Class | Why |
|---|---|
| `AMT_GeneralSettings` | Identity (HostName/Domain), DDNS disabled, ping/RMCP responses, network-interface state |
| `AMT_EthernetPortSettings` | DHCP on, shared MAC/IP with OS side (transparent AMT on mgmt VLAN) |
| `IPS_KVMRedirectionSettingData` | No legacy 5900/VNC, no OptIn prompt, 120-min KVM session timeout |
| `AMT_TimeSynchronizationService` (method) | Force AMT clock to manager UTC every apply — audit logs get real timestamps |

Findings from the 2026-04-22 discovery:

- `HostOSFQDN` is **not remotely writable** via WS-MAN — only
  LMS-writable. Talos has no LMS agent, so whatever ME has latched
  stays latched. prd-01 has a stale `dev-srv-03.w1.lv` from a prior
  OS install; harmless because effective AMT FQDN resolves correctly
  via `HostName + DomainName + SharedFQDN=true`. `put_singleton` has
  read-back verification that flags silent refusals with ⚠.
- AMT clocks across the fleet were 10-22 years off (prd-01 at 2004,
  rest at 2016). Every apply run force-syncs via
  `SetHighAccuracyTimeSynch` — no cronjob needed unless drift
  reappears.

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
