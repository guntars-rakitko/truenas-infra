# Verification matrix

Run these after `./manage.sh phase verify --apply` to confirm the NAS is
serving everything it should, and nothing it shouldn't.

## Network

| What | Command | Expected |
|---|---|---|
| UI on mgmt | `curl -skI https://10.10.5.10/` | 200/302 |
| VLAN 10 up | from prd Kube node: `ping -c3 10.10.10.10` | 0% loss |
| VLAN 15 up | from dev Kube node: `ping -c3 10.10.15.10` | 0% loss |
| VLAN 20 up | from home LAN: `ping -c3 10.10.20.10` | 0% loss |
| SSH on mgmt | `ssh svc-automation@10.10.5.10 whoami` | `svc-automation` |

## Storage

| What | Command | Expected |
|---|---|---|
| Pool healthy | `ssh admin@10.10.5.10 zpool status tank` | `ONLINE`, 6 disks |
| All datasets present | `ssh admin@10.10.5.10 zfs list -r tank` | all 13 datasets |
| SMART schedule | `midclt call smart.test.query` | 6 tasks, 1/disk |
| Scrub schedule | `midclt call pool.scrub.query` | 1 task, weekly |
| Snapshot schedule | `midclt call pool.snapshottask.query` | 7 tasks |

## Shares

| What | Command | Expected |
|---|---|---|
| NFS prd mountable | from prd node: `mount -t nfs 10.10.10.10:/mnt/tank/kube/prd/longhorn /mnt/t` | success |
| NFS dev mountable | from dev node: `mount -t nfs 10.10.15.10:/mnt/tank/kube/dev/longhorn /mnt/t` | success |
| NFS VLAN-20 isolated | from VLAN 20: `showmount -e 10.10.20.10` | connection refused |
| SMB `general` listed | `smbclient -L //10.10.20.10` | `general` share present |
| SMB Kube isolated | from Kube node: `smbclient -L //10.10.10.10` | connection refused |

## Apps

| What | Command | Expected |
|---|---|---|
| netboot.xyz HTTP | `curl http://10.10.5.10:8080/` | HTML, title `netboot.xyz` |
| TFTP | `tftp 10.10.5.10 -c get netboot.xyz.kpxe /tmp/x && ls -la /tmp/x` | non-zero size |
| MinIO prd | `mc alias set prd https://10.10.10.10:9000 … && mc ls prd` | no error |
| MinIO dev | `mc alias set dev https://10.10.15.10:9000 … && mc ls dev` | no error |
| MinIO VLAN-20 isolated | `curl --connect-timeout 3 http://10.10.20.10:9000` | refused/timeout |

## UPS / NUT

| What | Command | Expected |
|---|---|---|
| Service running | `midclt call service.query '[["service","=","ups"]]'` | `state=RUNNING` |
| Reachable from Kube | from Kube node (VLAN 5): `upsc apc1@10.10.5.10` | live UPS status |
| Not reachable from home | from VLAN 20: `nc -zv 10.10.5.10 3493` | refused (firewall) |

## TLS

| What | Command | Expected |
|---|---|---|
| Valid cert chain | `curl -I https://nas.w1.lv/` | 200, valid chain |
| Internal CA exported | `test -f docs/nas-internal-ca.pem && openssl x509 -in docs/nas-internal-ca.pem -noout -subject` | CN line printed |
