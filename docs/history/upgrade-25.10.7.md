# TrueNAS 25.10.7 upgrade, history

> Moved verbatim from CLAUDE.md § 25.10.3.1 → 25.10.7 (2026-09-13) on 2026-11-22, truenas-infra#233.

### 25.10.3.1 → 25.10.7 (2026-09-13)

Taken for the **ZFS 2.3 → 2.3.9** fixes (silent read corruption after block cloning, data
loss on redacted replication send, zvol sync writes not reaching the ZIL) plus NFS server
crash fixes — both clusters mount NFS from here — and kernel 6.12.105.

⚠ **This is an elevated-risk operation on THIS hardware, not a routine patch.** The update
is a multi-GB download+write followed by a reboot, which is exactly the documented trigger
profile for the Beelink ME mini 3.3 V rail defect (see § NVMe 3.3V-rail mitigations): a
sudden write burst onto a cold array. The Jul-22 drop was caused by a 118 MB write. Do it
with physical power access — a warm reboot does NOT clear a hung NVMe controller; only a
full power-off does.

**Outcome: clean.** No drive dropped; all 6 NVMe present after reboot, `tank` ONLINE,
0 errors. API surface verified against 25.10.7 afterwards: `preflight` connects and
`users` / `network` / `shares` / `nut` / `apps` / `tunables` / `storage-tasks` all dry-run
`rc=0` with **zero `changed=True`** — i.e. the upgrade reverted none of our declared config.
Dependent services all returned on their own (minio-prd/dev, cluster-agent both VLANs, wiki,
PXE tftp+http), and both K8s clusters rode through it with Velero BackupStorageLocation
back to `Available` without intervention.

⚠ **Verify drives by SERIAL after any reboot, never by `nvmeN`.** This upgrade reshuffled
enumeration again — `S4GRNX1RB33857` and `S4GRNX0NA00357` swapped nvme1/nvme2. A
device-name comparison would have looked like a missing drive when nothing was wrong.

---
