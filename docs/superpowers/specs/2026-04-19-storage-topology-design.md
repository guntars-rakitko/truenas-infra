# NAS Storage Topology — Design

**Date:** 2026-04-19
**Status:** Approved (RAIDZ1)

## Decision: RAIDZ1 across 6x 1TB NVMe

**Usable capacity:** ~4.5 TB (after ZFS padding/overhead)
**Fault tolerance:** Any single drive failure
**Rationale:** Capacity over double-parity was accepted because NAS content (Longhorn backups, Velero/MinIO, Plex media, general storage) is mostly recoverable secondary data. Parity level is immutable after pool creation in OpenZFS — this is a one-way door, documented here so the tradeoff is visible.

## Known risk

One of the six drives is significantly worn. Statistically it is most likely to fail first. With single-fault tolerance the pool has no margin during resilver, so a proactive replacement of this drive is planned in the near term.

## Planned datasets (final layout TBD at pool creation)

| Dataset | Purpose |
|---|---|
| longhorn-backups | Longhorn volume backup target (NFS for Kube) |
| velero-minio | Velero cluster state backups via MinIO S3 |
| plex-media | Plex library |
| general | Catch-all shared storage |

## Operational commitments

- **Scrub schedule:** Weekly (higher cadence than the default, given single-fault tolerance)
- **SMART tests:** Short daily, long weekly
- **Off-NAS backups:** Anything only stored on the NAS (configs, media not available elsewhere) must have an off-box copy
- **Tired drive replacement:** Schedule the replacement during low-activity time and accept that the pool is unprotected for the resilver window
- **Guardrail:** Any drive operation (replace, remove, test) must avoid leaving the pool fully degraded unexpectedly

## Explicitly ruled out

- **RAIDZ2** — more resilient but ~900 GB less usable; rejected in favor of capacity
- **3× 2-way mirrors (striped)** — best random IOPS but least capacity; NVMe IOPS headroom makes mirror-tier perf unnecessary for this workload
