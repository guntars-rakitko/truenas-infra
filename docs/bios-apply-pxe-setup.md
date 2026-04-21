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

### 1. Build the image locally

In the sibling `bios-config` clone:

```sh
cd ~/Documents/github/bios-config
./tools/build-bios-apply-img.sh
```

This emits `build/bios-apply.img` (~16 MB FAT16) built from the
committed `scripts/settings.txt`, `scripts/startup.nsh`, and
`tools/bin/{shellx64.efi,setup_var.efi}`. Optional QEMU smoke test:

```sh
qemu-system-x86_64 \
    -machine q35 \
    -drive if=pflash,format=raw,readonly=on,file=/opt/homebrew/share/qemu/edk2-x86_64-code.fd \
    -drive if=pflash,format=raw,file=/tmp/ovmf-vars.fd \
    -drive if=ide,format=raw,file=build/bios-apply.img \
    -nographic -no-reboot -net none -m 512
```

(Copy `/opt/homebrew/share/qemu/edk2-i386-vars.fd` to
`/tmp/ovmf-vars.fd` once to seed a blank vars file. QEMU will report
`Error writing variable: No variable with specified name found` — that
is expected, the `Setup` NVRAM variable only exists on real Q170S1
firmware.)

### 2. Upload + activate via phase apps

Back in `truenas-infra`:

```sh
cd ~/Documents/github/truenas-infra
./manage.sh phase apps --apply
```

`phase apps` does three things for this flow (see
`src/truenas_infra/modules/apps.py::_ensure_netboot_menu_files_via_ctx`):

1. Re-uploads `apps/netboot-xyz/custom.ipxe` — keeps the menu entry in sync.
2. Re-uploads `../bios-config/build/bios-apply.img` (if present locally)
   to `/mnt/tank/system/pxe/assets/bios-config/bios-apply.img` via the
   TrueNAS REST API (filesystem.put). **No SSH needed.**
3. Logs a sha256 prefix of the uploaded image so you can eyeball diffs
   between runs.

The image upload overwrites the remote file unconditionally on every
`--apply` because the FAT image is a fixed 16 MB regardless of the
embedded scripts' content — the default size-based idempotency can't
tell "startup.nsh changed" from "nothing changed". Dry-run
(`./manage.sh phase apps`, no --apply) reports `would_upload changed=True`
with a "dry-run — size-based idempotency unreliable" note, so the
distinction is explicit.

### 3. Smoke-test the HTTP serve

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

1. Edit `bios-config/scripts/startup.nsh` (MVP: inline CLI args to
   `setup_var.efi` — `settings.txt` is human-readable reference only;
   see the "Why CLI args, not stdin" note in
   `bios-config/scripts/startup.nsh` for why).
2. Commit in `bios-config`.
3. Rebuild locally: `./tools/build-bios-apply-img.sh`.
4. Re-upload via `cd truenas-infra && ./manage.sh phase apps --apply`.
5. Re-run the node via amtctl → PXE → bios-apply.

No source changes to `truenas-infra` are required per settings
iteration — phase apps just picks up the newer `.img`.

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

Once that ships, the operator only runs `./tools/build-bios-apply-img.sh`
on their laptop and the NAS auto-ingests. For now, step 2 above
(`./manage.sh phase apps --apply`) is the single manual push.
