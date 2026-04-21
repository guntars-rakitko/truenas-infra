# netboot.xyz + iPXE 2.0.0 USB-keyboard regression

## TL;DR

`apps/netboot-xyz/docker-compose.yaml` pins `MENU_VERSION=2.0.89` (the last
netboot.xyz release bundling **iPXE 1.21.x**). Do not bump to 3.x until
the upstream iPXE USB regression is fixed. If you do — the K8s nodes'
USB keyboards stop working in the iPXE menu (even via AMT KVM), which
turns Talos bring-up into a very frustrating time.

## Symptom

On Intel Q170-class UEFI firmware (e.g. our ASUS Q170S1 K8s nodes):

1. BIOS POSTs, Intel Boot Agent does TFTP of `netboot.xyz.efi` → fine.
2. iPXE loads, shows banner, fetches `menu.ipxe` from NAS TFTP → fine.
3. iPXE renders the menu.
4. **Keyboard is completely dead.** Not arrow keys, not Enter, not even
   Ctrl-B (break to shell). Same behaviour over AMT KVM virtual
   keyboard (so it's not physical-USB weirdness — the keystrokes reach
   the host via ME firmware but iPXE never sees them).

Keyboard in the actual BIOS setup screens works. The break is specifically
in the iPXE runtime environment.

## Root cause

- netboot.xyz 2.0.89 (Nov 2025) was the last release shipping iPXE
  **1.21.x**.
- netboot.xyz 3.0.0 (Jan 2026) bumped bundled iPXE to **2.0.0**.
- iPXE 2.0.0's USB stack, when running as a UEFI payload, detaches
  UEFI's built-in USB HID driver and tries to drive the xHCI controller
  itself — works on some chipsets, fails silently on others. Q170 is in
  the "fails silently" group.
- Confirmed by the iPXE maintainer in
  [ipxe/discussions/308](https://github.com/ipxe/ipxe/discussions/308)
  and reported for netboot.xyz specifically in
  [netbootxyz/discussions/868](https://github.com/orgs/netbootxyz/discussions/868).

The iPXE-author-documented fix is a rebuild with `USB_HCD_USBIO`
enabled in `src/config/usb.h` (delegates USB I/O back to UEFI). That's
a proper long-term fix but needs a custom iPXE build pipeline. Until we
have that, **pinning to 2.0.89 is the pragmatic fix**.

## Why the pin works

netboot.xyz 2.0.89:
- `netboot.xyz.efi`: 1,124,352 bytes, `sha1 18e9e5307f1cdfb970103c9c9a5d968e334e8470`
- iPXE 1.21.x — no xHCI driver-detach bug.

netboot.xyz 3.0.1 (the "latest" trap):
- `netboot.xyz.efi`: 1,176,576 bytes, `sha1 1825aa6f0d479f8c3ecce6c6e3b5b0ac95c482f9`
- iPXE 2.0.0 — broken.

## Container mechanics (gotcha)

The `ghcr.io/netbootxyz/netbootxyz` container's `init.sh` downloads the
boot files **only if `/config/menus/remote/menu.ipxe` is missing**.
Changing `MENU_VERSION` on an existing install has no effect — the
cached files stay. To actually re-download:

```sh
# 1. Stop the app
midclt call app.stop netboot-xyz

# 2. Clean the cache
rm -rf /mnt/tank/system/pxe/config/{menus,endpoints.yml,menuversion.txt}

# 3. Start — init.sh runs and pulls MENU_VERSION fresh
midclt call app.start netboot-xyz
```

(We do this from the manage.sh flow via a temp cronjob + `filesystem.put`
because the TrueNAS API doesn't expose a generic shell-exec.)

## How to verify the pin is in force

```sh
(echo "binary"; echo "get netboot.xyz.efi /tmp/nbx"; echo "quit") | tftp 10.10.5.10
shasum /tmp/nbx
# Expect: 18e9e5307f1cdfb970103c9c9a5d968e334e8470  (2.0.89)
# NOT:    1825aa6f0d479f8c3ecce6c6e3b5b0ac95c482f9  (3.0.1)
```

Also — the **MikroTik router's `/ipxe/` fallback TFTP** must be pinned
to matching binaries. `mikrotik-infra/manage.sh → 14) Update PXE images`
currently pulls from `https://boot.netboot.xyz/ipxe/` which serves the
latest — **don't run it blindly; we manually push 2.0.89 assets.** The
router's directory listing should show `netboot.xyz.efi` at **1098.0
KiB** (2.0.89), not 1149.0 KiB (3.0.1).

## When to revisit

Unpin (remove the `MENU_VERSION` env var and `rm` the cache) once any of
these lands:

1. iPXE upstream fixes the xHCI detach bug for Q170-class chipsets.
2. netboot.xyz ships a build with `USB_HCD_USBIO` enabled.
3. We build our own iPXE in-repo (considered — see `scripts/build-ipxe.sh`
   TODO).

Track:
- [ipxe/ipxe#308](https://github.com/ipxe/ipxe/discussions/308)
- [netbootxyz/netboot.xyz issue tracker](https://github.com/netbootxyz/netboot.xyz/issues?q=keyboard)
