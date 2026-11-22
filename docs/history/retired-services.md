# Retired NAS services, history

> Moved verbatim from CLAUDE.md § Planned Services (part) on 2026-11-22, truenas-infra#233.

> ⚠ **RETIRED 2026-09-23 with the pool rebuild** — MeshCentral, amtctl, PXE
> (server + directory index), homepage, iperf3 and stress-dashboard, plus the
> `stress-results` NFS export. **NFS now has no shares at all.**
> ⚠ MeshCentral and amtctl existed ONLY to KVM into the Q170S1 nodes over Intel
> AMT, and the MS-A2 has **no IPMI / BMC / vPro / AMT** — they are unusable on
> the new estate, not merely unused.
> amtctl's last leftover, `apps/amtctl/` (`nodes.yaml`, kept only for
> bios-config's AMT tools), was deleted 2026-10-06 when bios-config was archived:
> `git show 2f0f76b:apps/amtctl/README.md`.
> ⚠ The Traefik dashboard row was ALREADY stale: every Traefik dashboard in the
> homelab was removed **2026-09-13**, so `traefik-nas.w1.lv` has not existed
> since then. It sat here for ten days and caused a permanent false failure in
> the verify matrix.
> Surviving apps: **minio-prd, minio-dev, traefik, wiki** (+ **bambuddy**, added 2026-11-12). cluster-agent, the fifth until
> 2026-10-17, left the NAS for the clusters (§ cluster-agent).
