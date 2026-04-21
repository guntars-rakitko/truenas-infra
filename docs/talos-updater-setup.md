# Talos PXE updater — manual setup

## What this is

A daily job that:

1. Registers our Talos schematic (`nut-client` + `intel-ucode`) with
   `factory.talos.dev` and caches the schematic ID.
2. Polls `github.com/siderolabs/talos` for the latest stable release tag.
3. Downloads `vmlinuz` + `initramfs` for that version into
   `/mnt/tank/system/pxe/assets/talos/<version>/`.
4. Renders an iPXE menu entry at
   `/mnt/tank/system/pxe/config/menus/remote/talos.ipxe`
   that points Talos PXE boot at the latest images.

The netboot.xyz container (on VLAN 5, `10.10.5.10`) serves those assets
over HTTP on `:8080` and includes the `talos.ipxe` menu entry in its boot
menu tree.

## Why this is manual (for now)

The updater logic was originally designed as a sidecar container inside
the netboot.xyz compose stack. That turned out fragile — the inline shell
script in the compose `command:` heredoc couldn't reliably see the
`${VAR}` environment it depended on, and the sidecar failed with
`mkdir: can't create directory ''` in live tests.

The sensible fallback is a TrueNAS host-level cronjob, but TrueNAS's
`cronjob.command` field is limited to **1024 characters** and the
compacted shell pipeline is ~1200. Splitting it across `filesystem.put`
(drop the script on disk) + a short cronjob that just calls it is
straightforward and will be automated later; for now it's a one-time
manual step.

Everything except the cronjob registration is already committed to this
repo and reproducible:

| Artifact | Path |
|---|---|
| Reference script | `apps/netboot-xyz/talos-updater.sh` |
| Schematic | `apps/netboot-xyz/schematic.yaml` |
| Dataset | `tank/system/apps-config/talos-updater` (created by `phase datasets`) |
| Asset output dir | `tank/system/pxe/assets/talos/` (created by `phase datasets`) |
| Menu output dir | `tank/system/pxe/config/menus/remote/` (created by netboot.xyz) |

## Setup — one-time

Prereq: `phase datasets` and `phase apps` have already run successfully;
`netboot-xyz` is in state `RUNNING`.

### 1. Drop the script and schematic on the NAS

SSH to the NAS (`ssh admin@10.10.5.10`) and copy the two files from this
repo to the host:

```sh
sudo mkdir -p /mnt/tank/system/apps-config/talos-updater
sudo install -m 0755 /path/to/truenas-infra/apps/netboot-xyz/talos-updater.sh \
    /mnt/tank/system/apps-config/talos-updater/talos-updater.sh
sudo install -m 0644 /path/to/truenas-infra/apps/netboot-xyz/schematic.yaml \
    /mnt/tank/system/apps-config/talos-updater/schematic.yaml
```

(Or scp the files in.) The script lives on ZFS, so it's preserved across
TrueNAS upgrades.

### 2. Run once to populate assets

```sh
sudo SCHEMATIC_FILE=/mnt/tank/system/apps-config/talos-updater/schematic.yaml \
     ASSETS_DIR=/mnt/tank/system/pxe/assets/talos \
     MENU_DIR=/mnt/tank/system/pxe/config/menus/remote \
     STATE_FILE=/mnt/tank/system/apps-config/talos-updater/state \
     UPDATE_INTERVAL=1 \
     /mnt/tank/system/apps-config/talos-updater/talos-updater.sh
```

(The script's default `UPDATE_INTERVAL` is 86400s; set it to `1` for the
one-off run so the inner loop fires immediately. Kill with Ctrl-C after
you see `Update cycle complete for Talos vX.Y.Z`.)

Verify assets landed:

```sh
ls /mnt/tank/system/pxe/assets/talos/
# → v1.8.3/  (or whatever the latest tag is)

ls /mnt/tank/system/pxe/assets/talos/v1.8.3/
# → vmlinuz-amd64  initramfs-amd64.xz

cat /mnt/tank/system/pxe/config/menus/remote/talos.ipxe
# → #!ipxe ... kernel http://10.10.5.10:8080/talos/v1.8.3/vmlinuz-amd64 ...
```

### 3. Register the daily cronjob (TrueNAS UI)

**System → Advanced → Cron Jobs → Add:**

| Field | Value |
|---|---|
| Description | `talos-updater` |
| Command | `/mnt/tank/system/apps-config/talos-updater/talos-updater.sh` |
| Run as User | `root` |
| Schedule | Daily, `03:00` |
| Hide Standard Output | yes |
| Hide Standard Error | no |
| Enabled | yes |

Equivalent one-shot via `midclt` (from SSH on the NAS):

```sh
midclt call cronjob.create '{
  "enabled": true,
  "description": "talos-updater",
  "command": "/mnt/tank/system/apps-config/talos-updater/talos-updater.sh",
  "user": "root",
  "schedule": {"minute": "0", "hour": "3", "dom": "*", "month": "*", "dow": "*"}
}'
```

The description `talos-updater` is the idempotency key used by
`apps.ensure_cronjob()` — if the automation ever takes over, it will
detect the existing entry and leave it alone.

## Operating notes

- **Where to check if a boot fails:** on the NAS, `tail -f
  /var/log/cron` shows the cronjob invocation. The script itself logs
  to stdout (captured by cron if you leave stderr visible).
- **How to force a refresh:** delete
  `/mnt/tank/system/apps-config/talos-updater/state`, then run the
  script. It will re-register the schematic (idempotent — same YAML ⇒
  same schematic ID) and re-download.
- **Changing the schematic:** edit `schematic.yaml` on the NAS (and in
  this repo — keep them in sync), delete the `state` file, run the
  script. A new schematic ID will be issued.
- **PXE client URL:** Talos clients iPXE-chain to
  `http://10.10.5.10:8080/menus/remote/talos.ipxe`. This path is fixed
  by netboot.xyz's remote-menu convention.

## TODO — automate in-code

Short backlog item on `modules/apps.py`:

1. `filesystem.put` — write `talos-updater.sh` + `schematic.yaml` from
   `apps/netboot-xyz/` into `/mnt/tank/system/apps-config/talos-updater/`
   via the API. Diff on SHA-256 of file contents for idempotency.
2. `cronjob.create` with a short `command` that just invokes the
   on-disk script. Already implemented as `ensure_talos_updater_cronjob`
   — unblocked once step 1 lands.

Once that ships, delete the manual steps above and replace with a
pointer to `./manage.sh phase apps --apply`.
