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
`factory.talos.dev` from the cluster's schematic in kube-infra. In outline,
as the pool-rebuild plan states it (§ *Why PXE goes completely*):

1. Open `factory.talos.dev` and paste the schematic:
   - Q170S1 (kub-dev / kub-prd, including a re-image during the MS-A2
     rollback window): `kube-infra/talos-os/schematic.yaml`, at the Talos
     version pinned in `talos-os/patches/q170s1.yaml`.
   - MS-A2 (msa2-dev / msa2-prd): `kube-infra/talos-os/schematic-msa2.yaml`.
2. Download the ISO and write it to a USB stick.
3. Boot the stick at the machine. Nothing remote is left: the MS-A2 has no
   out-of-band management, and the Q170S1 AMT console (amtctl / MeshCentral)
   was retired the same day.

**Runbook:** [Talos USB install](https://wiki.w1.lv/runbooks/talos-usb-install/) (wiki): get
the ISO, write the stick, reset a disk that still holds Talos, boot the stick
at the box into maintenance mode, then hand off to `bootstrap.sh`. Proven on
the MS-A2 (2026-09-24); ⚠ **unproven on a Q170S1**, because the pool-rebuild
plan's Task 9 prerequisite covered only the MS-A2 stick. Prove it on a kub-dev
node before depending on it.

kube-infra `CLAUDE.md` § Install media describes this (rewritten 2026-09-25;
until then its § PXE Boot still presented PXE as the install path).

The other PXE menus (BIOS apply, utilities, live CDs, netboot) have no NAS
replacement. The Q170S1 BIOS is applied from a USB stick built by
`bios-config/usb/prepare-usb.sh` (see bios-config).

## Cleanup

- **Router DHCP options.** Removed from mikrotik-infra `configs/fleet.yaml`
  and the router template on 2026-09-26 (mikrotik-infra#51; pool-rebuild
  plan Task 9b Steps 1–2): mgmt DHCP no longer declares `next-server
  10.10.5.10` / `ipxe.efi` or the `pxe-boot` option set. The live router
  drops them with that PR's delta `--prune` apply (Task 9b Steps 3–5); a
  router audit that still shows them as drift means the apply has not run
  (mikrotik-infra `CLAUDE.md` § PXE Boot).
- ~~**Dead code in this repo.**~~ `apps/pxe/` and `config/talos.yaml` were
  deleted 2026-09-26 (the rest of pool-rebuild plan Task 9a); `git show
  3244103:apps/pxe/` has them.

## History

The full former page is in git history:
`git show 00e582b:docs/pxe-operator.md`.

This file is kept as a tombstone only because the wiki syncs it
(`wiki/sync-map.yaml` → `docs/runbooks/pxe-operator.md`). It is deleted
together with that mapping in one coordinated change — deleting the source
first makes the wiki sync fail.
