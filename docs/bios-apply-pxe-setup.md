# bios-apply PXE serving — manual setup

## What this is

A menu entry in the homelab netboot.xyz custom menu that applies
canonical ASUS Q170S1 BIOS settings to a node over the network. The
operator picks "Apply canonical BIOS config" from
`https://pxe.w1.lv/` (or the netboot.xyz menu after hitting
`amtctl.w1.lv` → Reset → PXE), the node `sanboot`s a small FAT disk
image served by this NAS, the UEFI Shell inside the image runs
`setup_var.efi` with a committed `settings.txt`, and the node
reboots with the canonical BIOS applied.

The BIOS settings, image builder, and startup.nsh live in the
sibling [`bios-config`](https://github.com/guntars-rakitko/bios-config)
repo. This document covers **the NAS side only** — how the built
`.img` gets served and how the `custom.ipxe` menu entry is wired.

Full architecture: `bios-config/docs/pxe-architecture.md`.

## Why this is manual (for now)

Same reason as the Talos updater
([`talos-pxe-updater`](talos-pxe-updater.md) in the wiki;
`docs/talos-updater-setup.md` in this repo): inline sidecar
automation is fragile, and the full NAS-side cronjob that would
auto-rebuild the `.img` on every new `bios-config` release tag is
deferred until the MVP flow validates end-to-end on real hardware
(Phase D of the PXE rollout plan).

For MVP, the image is built on the operator's laptop and scp'd to the
NAS once per BIOS-settings change. That's roughly every few weeks —
acceptable manual cadence. `phase apps` already handles the
`custom.ipxe` menu wiring automatically on every apply run.

## Layout on the NAS

```
/mnt/tank/system/pxe/
├── assets/                    ← served at http://10.10.5.10:8080/
│   ├── bios-config/
│   │   └── bios-apply.img     ← manually scp'd (this doc)
│   ├── custom.ipxe            ← uploaded by phase apps
│   ├── talos/<version>/       ← managed by talos-updater cronjob
│   └── talos-menu.ipxe        ← managed by talos-updater cronjob
└── config/
    └── menus/
        └── boot.cfg           ← uploaded by phase apps
```

HTTP-served root is `/assets/`. The `custom.ipxe` menu chains to the
bios-apply flow via:

```ipxe
sanboot --keep http://10.10.5.10:8080/bios-config/bios-apply.img
```

## Setup — one-time, per bios-config release

Prereq: `phase apps` has already run — `custom.ipxe` is live and
includes the `bios-apply` menu item.

### 1. Build the image locally

In the sibling `bios-config` clone:

```sh
cd ~/Documents/github/bios-config
./tools/build-bios-apply-img.sh
```

This emits `build/bios-apply.img` (~16 MB FAT16) built from the
committed `scripts/settings.txt`, `scripts/startup.nsh`, and
`tools/bin/{shellx64.efi,setup_var.efi}`. QEMU smoke test:

```sh
qemu-system-x86_64 \
    -machine q35 \
    -drive if=pflash,format=raw,readonly=on,file=/opt/homebrew/share/qemu/edk2-x86_64-code.fd \
    -drive if=pflash,format=raw,file=/tmp/ovmf-vars.fd \
    -drive if=ide,format=raw,file=build/bios-apply.img \
    -nographic -no-reboot -net none -m 512
```

(Copy `/opt/homebrew/share/qemu/edk2-i386-vars.fd` to
`/tmp/ovmf-vars.fd` once to seed a blank vars file.)

### 2. Make the target directory on the NAS

SSH to the NAS:

```sh
ssh admin@10.10.5.10
sudo mkdir -p /mnt/tank/system/pxe/assets/bios-config
sudo chown admin:admin /mnt/tank/system/pxe/assets/bios-config
```

### 3. Copy the image onto the NAS

From the laptop:

```sh
scp ~/Documents/github/bios-config/build/bios-apply.img \
    admin@10.10.5.10:/mnt/tank/system/pxe/assets/bios-config/bios-apply.img
```

### 4. Smoke-test the HTTP serve

From anywhere on the mgmt VLAN:

```sh
curl -I http://10.10.5.10:8080/bios-config/bios-apply.img
# HTTP/1.1 200 OK
# Content-Length: 16777216
# Content-Type: application/x-troff-man    (or similar — MIME doesn't matter)
```

Browse to `https://pxe.w1.lv/` → the main menu → "Custom URL Menu"
→ the custom menu should now show both "Talos: …" and "Apply
canonical BIOS config (bios-config MVP)".

## Operator flow — applying to a node

1. Open `amtctl.w1.lv` → click the target node → **Reset → PXE**.
   The node warm-resets with a one-shot PXE boot override.
2. netboot.xyz appears. Pick **Custom URL Menu**.
3. Pick **Apply canonical BIOS config (bios-config MVP)**.
4. Wait a few seconds — the UEFI Shell loads the image, auto-runs
   `startup.nsh`, and `setup_var.efi` applies the committed settings.
5. The node reboots with the new BIOS config.
6. Verify: AMT KVM into the node during POST and confirm the
   settings are as expected (see MVP verification list in
   `bios-config/docs/pxe-architecture.md`).

## How to iterate on settings

Same cadence as Talos PXE images:

1. Edit `bios-config/scripts/settings.txt` (add a new line in the
   `VAR_NAME:OFFSET=VALUE` syntax) and / or `scripts/startup.nsh`.
2. Commit in `bios-config`.
3. Rebuild locally: `./tools/build-bios-apply-img.sh`.
4. Re-scp per step 3 above.
5. Re-run the node via amtctl → PXE → bios-apply.

No changes to `truenas-infra` are required per settings iteration.
This repo only cares about the custom.ipxe menu + image location.

## Operating notes

- **Where to check if a sanboot fails:** the node's AMT KVM will show
  the iPXE error. First things to look at:
  - `curl -I http://10.10.5.10:8080/bios-config/bios-apply.img` on
    the mgmt VLAN — expect `200 OK`.
  - `ls /mnt/tank/system/pxe/assets/bios-config/` on the NAS.
  - `docker logs netbootxyz` — HTTP access log lines for
    `/bios-config/bios-apply.img`.
- **iPXE pinning:** `netboot-xyz` is pinned at `MENU_VERSION=2.0.89`
  (iPXE 1.21.x) per
  [`ipxe-keyboard-regression`](ipxe-keyboard-regression.md) (wiki)
  — source: `truenas-infra/docs/netboot-xyz-ipxe-keyboard-regression.md`.
  `sanboot` is stable in 1.21. If that pin ever bumps, re-test this
  flow.
- **Recovering from a bad write:** CMOS reset via jumper or battery
  pull. See `bios-config/docs/recovery.md`. The PXE flow can then
  re-apply the canonical BIOS without physical access.

## TODO — automate in-code

Short backlog item on `modules/apps.py`:

1. A new `bios-updater.sh` host-level cronjob that:
   a. Polls `github.com/guntars-rakitko/bios-config` release tags.
   b. Downloads the published `bios-apply.img` artifact (once
      releases start publishing one — or invokes the builder
      locally if Rust / brew aren't on the NAS).
   c. Diffs SHA-256 and swaps the file in place.
2. `cronjob.create` registration mirroring `talos-updater`.

Once that ships, steps 1–3 of the manual setup go away.
