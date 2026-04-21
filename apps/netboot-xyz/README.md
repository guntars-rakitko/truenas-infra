# netboot-xyz

PXE/TFTP + HTTP asset server + menu UI for bare-metal boot.

Runs on VLAN 5 (`10.10.5.10`) as a TrueNAS **Custom App**. Serves the 6
Kube nodes during Talos bring-up.

## Files

| File | Purpose |
|---|---|
| `docker-compose.yaml` | Single-container netboot.xyz stack — registered by `phase apps` |
| `schematic.yaml` | Talos factory schematic (nut-client + intel-ucode) |
| `talos-updater.sh` | Reference script that polls factory.talos.dev + GitHub and updates PXE assets |

## Ports

All bound to `10.10.5.10` (the mgmt VLAN IP) only — nothing listens on
the VLAN 10/15/20 sub-interfaces.

| Port | Purpose |
|---|---|
| `10.10.5.10:69/udp` | TFTP |
| `10.10.5.10:8080` | HTTP — Talos kernel/initramfs assets and iPXE menus |
| `10.10.5.10:3000` | Web UI |

## Volumes

| Host path | Container path |
|---|---|
| `/mnt/tank/system/pxe/config` | `/config` |
| `/mnt/tank/system/pxe/assets` | `/assets` |

## Talos auto-updater

The updater is **not** a sidecar container in this compose file — it
runs as a **TrueNAS host-level cronjob** writing into the shared PXE
asset / menu volumes. Rationale and current manual setup steps are in
[`docs/talos-updater-setup.md`](../../docs/talos-updater-setup.md).

The cronjob is **not auto-registered** by `phase apps` yet — the
compacted command exceeds TrueNAS's 1024-char `cronjob.command` limit.
`ensure_talos_updater_cronjob()` exists in `modules/apps.py` but is
gated on a follow-up that writes the script to disk via
`filesystem.put` first.

## Deploy

```sh
./manage.sh phase apps --apply
```

Verify state:

```sh
./manage.sh phase verify
# → check_passed  name='app netboot-xyz'  state=RUNNING
```

Once running, the UI is at `http://10.10.5.10:3000/` and PXE assets are
served from `http://10.10.5.10:8080/`.
