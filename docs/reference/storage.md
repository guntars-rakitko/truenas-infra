# Storage reference

> Moved verbatim from CLAUDE.md § Storage Design on 2026-11-22, truenas-infra#233.

## Storage Design

Live design — source of truth is `config/storage.yaml` (consumed by
`modules/{pool,datasets,storage_tasks}.py`). Summary:

- **Pool `tank`** — single **3-wide RAIDZ1** vdev (rebuilt 2026-09-23 from
  5-wide), `ashift=12`, **`autotrim=off`** (disabled 2026-07-22 — TRIM bursts
  spike 3.3V-rail current; see § NVMe 3.3V-rail mitigations; enforced by
  `pool.py::ensure_autotrim`). **1.75 TB usable.** The 256 GB PM981 is the
  TrueNAS-installer-managed `boot-pool`, not part of `tank`.
  ⚠ **ZFS cannot remove a device from a raidz vdev** (expansion landed in
  OpenZFS 2.3; shrinking never did) — 5→3 was destroy-and-recreate. Plan and
  full rationale: `docs/superpowers/plans/2026-09-23-nas-pool-rebuild.md`.
  ⚠ Going 5→3 is itself a **3.3V-rail mitigation**: it removes two drives'
  worth of peak *simultaneous* current, which is the documented root cause.
  Measured PS2 draw is now **10.09 W** across three drives (980 2.19 W +
  SM961 4.40 W + PM981a 3.50 W); the figures were measured per drive on
  2026-09-23, not taken from datasheets.
  ⚠ **Mixed models in one vdev is deliberate** — three firmware families and
  power curves, accepted because drive availability won over homogeneity.
  **If a drive fails here, RECORD WHICH ONE**: on a mixed vdev that
  attribution is the only thing that teaches anything.
  ⚠ When replacing a drive, match by **serial, not slot** — the 25.10.7
  upgrade swapped nvme1/nvme2 and the rebuild moved boot from nvme3 to nvme2.
  First scrub on the new vdev: `repaired 0B in 00:00:17 with 0 errors`.
  ⚠ That is NOT evidence the rail fault is resolved — 7 GB in 17 s with temps
  unmoved is nowhere near the forensics' **42-day** statistical bar.
- **Dataset defaults** — `compression=lz4`, `atime=off`, `xattr=sa`,
  `recordsize=128K` (the MinIO data datasets, misnamed `…/velero`, override to `1M`).
- **Dataset tree** (env-first): `tank/kube/{prd,dev}/velero` — ⚠ **MISNAMED**: each is
  MinIO's whole `/data` for that env and holds **every** bucket, not just Velero's; rename to
  `tank/kube/{prd,dev}/minio` is tracked in #152, after the MS-A2 cutover. (Longhorn
  datasets **removed 2026-04-27** — Longhorn → MinIO S3, kube-infra #26),
  `tank/media/{plex,torrent}`, `tank/shared/general`, and `tank/system/*`
  (apps-config/{nut,traefik,wiki,bambuddy}, tls). ⚠ `pxe` and `stress-results` were
  removed 2026-09-23 with their apps.
- **Snapshots** — per-env recursive tasks (prd 14 d / dev 7 d on
  `tank/kube/*`, media weekly ×4 w, shared 14 d, system 7 d).
  ⚠ **Not live:** none of these tasks exists since the 2026-09-23 rebuild, and `tank` has
  no snapshots (read 2026-11-12). The block left `config/storage.yaml` in e3d5663: #222.
- **Scrub** Sun 04:00; **SMART** short Sun 02:00 + long first-Sun 03:00.

---
