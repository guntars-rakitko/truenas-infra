# bios-apply PXE serving — RETIRED 2026-09-23

> ⚠ **Retired.** This page described how the NAS served the Q170S1
> `bios-apply.img` over PXE, reached via `amtctl.w1.lv` → Reset → PXE.
> **The procedure cannot run any more:** PXE, amtctl and the
> `tank/system/pxe` dataset were all removed with the 2026-09-23 NAS pool
> rebuild (truenas-infra #146 plan, #149), and the menu-wiring code
> (`_ensure_pxe_menu_files_via_ctx`) was deleted.

## What replaces it

- **The MS-A2 rollback does not need it.** The old Q170S1 nodes already carry
  the canonical BIOS; nothing in the MS-A2 migration or its rollback window
  re-applies it.
- **If a Q170S1 ever needs its BIOS re-applied** (reset, RMA), use the
  bios-config offline path: a USB stick built by `bios-config/usb/prepare-usb.sh`
  (applies `scripts/settings.txt`), booted at the machine with a monitor and
  keyboard. There is no remote console since amtctl and MeshCentral were
  retired the same day. See bios-config's own docs.
- **Talos re-installs** boot a USB ISO: open `factory.talos.dev`, paste the
  cluster's schematic from kube-infra (`talos-os/schematic.yaml`, at the Talos
  version pinned in `talos-os/patches/q170s1.yaml`, for a Q170S1;
  `talos-os/schematic-msa2.yaml` for an MS-A2), download the ISO, write the
  stick and boot it at the machine
  (`docs/superpowers/plans/2026-09-23-nas-pool-rebuild.md` § *Why PXE goes
  completely*). ⚠ No written runbook exists yet (a wiki `talos-usb-install`
  page is planned). kube-infra CLAUDE.md § PXE Boot still presents PXE as the
  install path and is stale until it is rewritten, so do not follow it.

## History

The full former page is in git history:
`git show 00e582b:docs/bios-apply-pxe-setup.md`.

This file is kept as a tombstone only because the wiki syncs it
(`wiki/sync-map.yaml` → `docs/runbooks/bios-apply-pxe-setup.md`). It is
deleted together with that mapping in one coordinated change (pool-rebuild
plan Task 9a) — deleting the source first makes the wiki sync fail.
