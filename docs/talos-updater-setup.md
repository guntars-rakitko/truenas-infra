# Talos PXE updater — RETIRED 2026-09-23

> ⚠ **Retired.** This page described a nightly NAS cronjob that cached Talos
> kernel + initramfs images for PXE boot and rendered an iPXE version-picker
> menu. **None of it exists any more.** PXE was removed entirely with the
> 2026-09-23 NAS pool rebuild (truenas-infra #146 plan, #149): the
> `talos-updater` cronjob was deleted from the NAS (21dfb97), its code
> (`ensure_talos_updater`, `TalosUpdaterConfig`) was deleted (b7262d5), and
> the `tank/system/pxe` dataset went with the pool. Reasons:
> `docs/superpowers/plans/2026-09-23-nas-pool-rebuild.md` § *Why PXE goes
> completely*.

## What replaces it

**Talos upgrades never used this.** `talosctl upgrade` pulls the installer
image from the registry (`factory.talos.dev/installer/<schematic ID>:<version>`);
nothing on the NAS is involved.

**Talos installs and re-images now boot a USB ISO**, built on demand at
`factory.talos.dev` from the cluster's schematic in kube-infra — there is no
local image cache:

- Q170S1 (kub-dev / kub-prd, including a re-image during the MS-A2 rollback
  window): `kube-infra/talos-os/schematic.yaml`, at the version pinned in
  `talos-os/patches/q170s1.yaml`.
- MS-A2 (msa2-dev / msa2-prd): `kube-infra/talos-os/schematic-msa2.yaml`.

See kube-infra CLAUDE.md for the install-media procedure. ⚠ Someone has to be
at the machine: the MS-A2 has no out-of-band management, and the Q170S1
remote-console path (amtctl / MeshCentral) was retired the same day.

## History

The full former page is in git history:
`git show 00e582b:docs/talos-updater-setup.md`.

This file is kept as a tombstone only because the wiki syncs it
(`wiki/sync-map.yaml` → `docs/runbooks/talos-pxe-updater.md`). It is deleted
together with that mapping in one coordinated change (pool-rebuild plan
Task 9a) — deleting the source first makes the wiki sync fail.
