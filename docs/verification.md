# Verification matrix

Run these after `./manage.sh phase verify --apply` to confirm the NAS is
serving everything it should, and nothing it shouldn't.

## Network

| What | Command | Expected |
|---|---|---|
| UI on mgmt | `curl -skI https://10.10.5.10/` | 200/302 |
| VLAN 10 up | from a debug pod in the prd cluster (Talos nodes have no shell): `ping -c3 10.10.10.10` — or the `cluster-data-plane` MinIO blackbox probe in that cluster's Prometheus | 0% loss / probe success |
| VLAN 15 up | from a debug pod in the dev cluster: `ping -c3 10.10.15.10` — or the dev cluster's `cluster-data-plane` MinIO probe | 0% loss / probe success |
| VLAN 20 up | from home LAN: `ping -c3 10.10.20.10` | 0% loss |
| SSH on mgmt | `ssh svc-automation@10.10.5.10 whoami` | `svc-automation` |

## Storage

| What | Command | Expected |
|---|---|---|
| Pool healthy | `ssh truenas_admin@10.10.5.10 zpool status tank` | `ONLINE`, **3 disks** — rebuilt 5-wide → 3-wide raidz1 on 2026-09-23 |
| All datasets present | `ssh truenas_admin@10.10.5.10 zfs list -r tank` | every dataset in `config/storage.yaml` present (20 under `tank`) |
| SMART | not verifiable via the API on 25.10 (`smart.*` was removed; `phase storage-tasks` logs a skip) — on the NAS: `sudo smartctl -H /dev/nvmeX` for each of the 4 NVMe drives (match by serial) | `PASSED` on all 4 |
| Scrub schedule | `midclt call pool.scrub.query` | 1 task, weekly |
| Snapshot schedule | `midclt call pool.snapshottask.query` | 7 tasks |

## Shares

| What | Command | Expected |
|---|---|---|
| NFS exports | `showmount -e 10.10.10.10` | empty — NFS has had no shares since 2026-09-23 (the Longhorn exports went 2026-04-27, `stress-results` with the pool rebuild) |
| NFS VLAN-20 isolated | from VLAN 20: `showmount -e 10.10.20.10` | connection refused |
| SMB `general` listed | `smbclient -L //10.10.20.10` | `general` share present |
| SMB Kube isolated | from a debug pod in a cluster (Talos nodes have no shell): `smbclient -L //10.10.10.10` | connection refused |

## Apps

| What | Command | Expected |
|---|---|---|
| MinIO prd | `mc alias set prd https://10.10.10.10:9000 … && mc ls prd` | no error |
| MinIO dev | `mc alias set dev https://10.10.15.10:9000 … && mc ls dev` | no error |
| MinIO VLAN-20 isolated | `curl --connect-timeout 3 http://10.10.20.10:9000` | refused/timeout |

## UPS / NUT

| What | Command | Expected |
|---|---|---|
| Service running | `midclt call service.query '[["service","=","ups"]]'` | `state=RUNNING` |
| Reachable from Kube | each cluster's Prometheus: `up{job="nut-exporter"}` and `network_ups_tools_ups_status` (the Talos nodes are not NUT clients since 2026-06-02 and have no shell) | target UP, `ups_status` series present |
| Not reachable from home | from VLAN 20: `nc -zv 10.10.5.10 3493` | refused (firewall) |

## TLS

| What | Command | Expected |
|---|---|---|
| Valid cert chain | `curl -I https://nas.w1.lv/` | 200, valid chain |
