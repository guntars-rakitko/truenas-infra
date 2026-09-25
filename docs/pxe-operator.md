# PXE operator runbook — RETIRED 2026-09-23

> ⚠ **Retired.** The NAS PXE service (`homelab-pxe` Custom App: TFTP `:69` +
> HTTP `:8080` on `10.10.5.10`, iPXE menus, ISO cache under
> `/mnt/tank/system/pxe/`) was removed entirely with the 2026-09-23 NAS pool
> rebuild (truenas-infra #146 plan, #149). The app, its `pxe-download`
> cronjob and the `tank/system/pxe` dataset are gone. **Nothing answers a PXE
> boot on this network.** Reasons: `docs/superpowers/plans/2026-09-23-nas-pool-rebuild.md`
> § *Why PXE goes completely*.

## What replaces it

**Talos installs and re-images boot a USB ISO**, built on demand at
`factory.talos.dev` from the cluster's schematic in kube-infra:

- Q170S1 (kub-dev / kub-prd, including a re-image during the MS-A2 rollback
  window): `kube-infra/talos-os/schematic.yaml`, at the version pinned in
  `talos-os/patches/q170s1.yaml`.
- MS-A2 (msa2-dev / msa2-prd): `kube-infra/talos-os/schematic-msa2.yaml`.

See kube-infra CLAUDE.md for the install-media procedure. ⚠ The pool-rebuild
plan (Task 9 prerequisite) requires the USB path to be proven on real hardware
before the old estate is torn down.

The other PXE menus (BIOS apply, utilities, live CDs, netboot) have no NAS
replacement. The Q170S1 BIOS is applied from a USB stick built by
`bios-config/usb/prepare-usb.sh` (see bios-config).

## Still pending

- **Router DHCP options.** mikrotik-infra `configs/fleet.yaml` still declares
  `pxe: true` / `pxe_bootfile: ipxe.efi` / `pxe_next_server: 10.10.5.10` on
  `10.10.5.0/24` plus the `pxe-boot` option set, so mgmt DHCP may still
  advertise a boot server that nothing serves (inert, but misleading; whether
  the live router still carries it is unchecked). Removal: pool-rebuild plan
  Task 9b.
- **Dead code in this repo.** `apps/pxe/` and `config/talos.yaml` are still
  in the tree; removal is pool-rebuild plan Task 9a.

## History

The full former page is in git history:
`git show 00e582b:docs/pxe-operator.md`.

This file is kept as a tombstone only because the wiki syncs it
(`wiki/sync-map.yaml` → `docs/runbooks/pxe-operator.md`). It is deleted
together with that mapping in one coordinated change — deleting the source
first makes the wiki sync fail.
