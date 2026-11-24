# Network reference

> Moved verbatim from CLAUDE.md § Network on 2026-11-22, truenas-infra#233.

## Network

| Interface | VLAN | IP | Purpose |
|---|---|---|---|
| NIC1 — tagged sub-iface | 10 | 10.10.10.10 | Prod Kube: MinIO S3 (every backup bucket, :9000) |
| NIC1 — tagged sub-iface | 15 | 10.10.15.10 | Dev Kube: MinIO S3 (every backup bucket, :9000); Traefik's `devapi` (:8000, `bambuddy-api-dev.w1.lv`: the Bambuddy API paths print-shop calls, since 2026-11-24) |
| NIC1 — tagged sub-iface | 20 | 10.10.20.10 | Home: Plex, torrent UI, SMB general share |
| NIC2 — untagged | 5 | 10.10.5.10 | TrueNAS API/UI, SSH, NUT, MinIO consoles (:9001 prd / :9011 dev), wiki (:8088), Bambuddy (:8000). PXE/TFTP retired 2026-09-23. |

Connected to CRS310:
- `ether7` — tagged trunk, VLANs 10/15/20 (data, NIC1)
- `ether8` — untagged, VLAN 5 (management, NIC2)

Service-to-interface binding is enforced in TrueNAS. Kube backup targets are MinIO S3, which binds only on the data-VLAN IPs (`.10.10` / `.15.10`); Plex + SMB bind only on `.20.10`. So home devices cannot reach Kube backup targets even though NIC1 is physically shared. Note on NFS: the old Longhorn NFS exports were **decommissioned 2026-04-27** (Longhorn → MinIO S3 `longhorn` bucket, kube-infra #26). ⚠ **As of 2026-09-23 NFS has NO shares at all.** `stress-results` was the last one and went with hw-validation in the pool rebuild. The service is still enabled and still binds all three sub-IPs (`config/shares.yaml § nfs.service.bindip`) — left that way deliberately, because turning NFS off is a separate decision that should be reviewable in its own change rather than implied by an empty share list.

---
