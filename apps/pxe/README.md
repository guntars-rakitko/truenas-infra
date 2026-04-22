# Homelab PXE

Zero-dependency PXE/TFTP + HTTP stack. Hand-built iPXE binary, hand-written
menu tree, curated asset cache. No netboot.xyz at runtime.

## What this is

A single Docker container that provides:

- **TFTP on port 69/udp** — serves our custom iPXE binary (`ipxe.efi`) plus
  the menu tree (`menu.ipxe`, `boot.cfg`, `menus/*.ipxe`). Backed by
  `dnsmasq`.
- **HTTP on port 80 (host 8080)** — serves cached distro + utility assets
  mirrored from upstream origins by the `pxe-cache` cronjob. Backed by
  `nginx`.

The iPXE binary is built from upstream iPXE source inside the image (first
container start takes ~5 min), with our own embedded boot script and the
`USB_HCD_USBIO` flag enabled to route USB HID through UEFI's stack
(works around iPXE xHCI keyboard regression on Intel Q170-class hardware).

## Directory layout

```
apps/pxe/
├── docker-compose.yaml          # Custom App definition (single service)
├── README.md                    # this file
├── build/                       # container build context (uploaded to NAS)
│   ├── Dockerfile               # multi-stage: iPXE builder + alpine runtime
│   ├── embed.ipxe               # baked into ipxe.efi — DHCP + chain menu
│   ├── local-general.h          # iPXE feature flag overrides
│   ├── local-usb.h              # USB_HCD_USBIO workaround
│   ├── entrypoint.sh            # launches dnsmasq + nginx
│   └── nginx.conf               # nginx runtime config
├── menu.ipxe                    # top-level Homelab PXE menu
├── boot.cfg                     # runtime vars (site_name, cache_url, …)
├── menus/
│   ├── talos.ipxe               # Talos installer — install / wipe / serial
│   ├── bios.ipxe                # BIOS apply (bios-config MVP)
│   ├── utils.ipxe               # Memtest, GParted, SystemRescue, UEFI Shell
│   ├── linux.ipxe               # Ubuntu Server + Debian netinst
│   └── live.ipxe                # Ubuntu Desktop, Debian Live, SystemRescue
├── pxe-cache.sh                 # mirrors upstream assets to NAS HTTP root
├── talos-updater.sh             # keeps /talos/ up to date with latest release
└── schematic.yaml               # Talos image customization schematic
```

## NAS-side paths

| Path | Mount | Contents |
|---|---|---|
| `/mnt/tank/system/pxe/tftp` | `ro` → `/srv/tftp` | menu tree (menu.ipxe, boot.cfg, menus/*) |
| `/mnt/tank/system/pxe/http` | `ro` → `/srv/http` | cached kernels, initrds, ISOs, bios-apply.img |
| `/mnt/tank/system/apps-config/pxe/build` | build context | Dockerfile + build inputs |
| `/mnt/tank/system/apps-config/pxe` | host-level | pxe-cache.sh + log files |

`ipxe.efi` is baked into the image by the Dockerfile (`COPY --from=builder`),
so it's NOT in the TFTP bind mount — it's compiled fresh from upstream iPXE
source every time the image is built.

## How a PXE boot works

1. Node's UEFI PXE client DHCPs.
   MikroTik responds with `next-server=10.10.5.10`, BOOTP `file=ipxe.efi`,
   and option 67 `bootfile-name=ipxe.efi` (belt + suspenders).
2. Node TFTP-fetches `ipxe.efi` from `10.10.5.10:69/udp`.
3. `ipxe.efi`'s embedded boot script runs:
   - DHCP inside iPXE (same response)
   - `chain tftp://${next-server}/menu.ipxe` → our Homelab menu
4. `menu.ipxe` chains `boot.cfg` (sets `site_name`, `cache_url`), then shows
   the main menu.
5. Operator picks a sub-menu item (Talos, BIOS, etc.). The sub-menu either
   kernel-boots directly from `${cache_url}/…` (nginx on port 8080 serving
   the cached asset) or `sanboot`s a disk image.

## Caching (`pxe-cache.sh`)

Runs weekly via cronjob (registered by `phase apps`). Fetches from upstream
distro origins (never via netboot.xyz):

- Utilities: memtest86plus github, sourceforge (gparted, sysresc),
  tianocore edk2 (UEFI shell), zbm-dev github (ZFSBootMenu).
- Linux installers: archive.ubuntu.com, deb.debian.org.
- Live ISOs: releases.ubuntu.com, cdimage.debian.org.

Idempotent — skips files that already exist with matching size. Log goes to
`/mnt/tank/system/apps-config/pxe/pxe-cache.log`.

## Maintenance

- **Add a new distro** — edit `menus/linux.ipxe` (or `live.ipxe`), add the
  matching `fetch` block to `pxe-cache.sh`, commit, run
  `./manage.sh phase apps --apply`.
- **Update iPXE** — bump the `IPXE_REF` ARG in `build/Dockerfile`, run
  `./manage.sh phase apps --apply`, wait for the image rebuild.
- **Force cache refresh** — SSH to NAS and run
  `/mnt/tank/system/apps-config/pxe/pxe-cache.sh`. Or delete the target
  file and re-run.

## Retiring netboot.xyz

This app replaces the previous `netboot-xyz` Custom App (which wrapped
`ghcr.io/netbootxyz/netbootxyz`). See commit history for the cutover —
old app deleted, new `pxe` app created, MikroTik DHCP bootfile renamed
from `netboot.xyz.efi` to `ipxe.efi`.
