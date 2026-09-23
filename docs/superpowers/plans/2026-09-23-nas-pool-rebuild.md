# NAS Pool Rebuild Implementation Plan

> **For agentic workers:** this is an OPERATIONAL runbook against live hardware, not a code plan. It is executed by the **main session with the operator present**, never by a subagent — CLAUDE.md § *Subagents are READ-ONLY on live clusters and appliances*. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Rebuild `tank` from a 5-wide RAIDZ1 to a 3-wide RAIDZ1, dropping ~1.17 TB of
disposable backup data, removing four retired apps, and shrinking PXE from 15 G to 610 M —
while preserving the only irreplaceable thing on the box: a restorable copy of the **live
GIKS v1 production database**.

**Architecture:** ZFS cannot remove a device from a raidz vdev (expansion landed in OpenZFS
2.3; shrinking never did), so 5→3 is necessarily destroy-and-recreate. Everything on `tank`
is therefore lost by definition; the plan is a preserve list, a destroy, and a rebuild from
git + Doppler.

**Tech Stack:** TrueNAS 25.10.7 · ZFS · MinIO AIStor · `manage.sh` phase dispatcher · `mc` · MSSQL

**Related:** `kube-infra/docs/MSA2-CONSOLIDATION.md` (why cluster backups are disposable) ·
`docs/nvme-dropout-forensics.md` (the 3.3 V rail fault) · `CLAUDE.md` § *NVMe 3.3V-rail mitigations*

---

## ⚠ Drive selection — REVISED 2026-09-23

Operator decision: **only `S4GSNF0N301379` stays**; all four other tank members come out and
two drawer drives go in.

| role | model | serial | note |
|---|---|---|---|
| ✅ new tank | PM981a `-000H7` | `S4GSNF0N301379` | characterised; PS2 cap known (8.00→3.50 W) |
| ✅ new tank | **SM961 1 TB** | *(record on install)* | ⚠ **power table UNKNOWN** — see § below |
| ✅ new tank | **Samsung 980 1 TB** | *(record on install)* | ⚠ **power table UNKNOWN** — see § below |
| boot-pool | PM981 256 GB | `S444NX0N496890` | untouched, installer-managed |
| ⏏ out | PM981a `-000H1` | `S4GRNX0NA00357` | dropped twice (Jul 19/22, Mode A) |
| ⏏ out | PM981a `-000H1` | `S4GRNX1RB33857` | clean — **keep as the spare** |
| ⏏ out | PM9A1 | `S6H2NF0WC37390` | healthy — MS-A2 candidate |
| 🚫 out | PM9A1 | `S6H2NF0WC37392` | **faulty, never reuse** |

3 × 1 TB raidz1 → **~1.8 TB usable**, the stated target.

🚫 **`S6H2NF0WC37392` must never be reused — not here, not in an MS-A2.** It is the **Mode B**
failure in `docs/nvme-dropout-forensics.md`: an I/O-timeout cascade ending in
`Device not ready; aborting reset, CSTS=0x1` — the controller **alive and answering**, i.e. a
drive firmware hang rather than a power event — and **the fault followed it across a physical
reslot**. It is the one drive in this box attributable to the drive rather than the platform.

✅ **`S4GRNX1RB33857` has no incident history** — keep it as the cold spare rather than
shelving it indiscriminately. If a new drive misbehaves during bring-up, it is the known-good
1 TB replacement already in hand.

### ⚠⚠ MANDATORY before pool creation: characterise the two new drives

The estate's rail mitigation is a **per-model** udev rule (`90-nvme-ps2-cap`) that pins each
drive to PS2. CLAUDE.md records what that buys, model by model — PM981a **8.00 → 3.50 W**,
PM9A1 **8.49 → 3.18 W**. ⚠ **Nobody has ever read the power table on an SM961 or a 980 in this
estate.** PS2 on those parts may be a different wattage, or not an operational state at all.

This box's defining fault is the shared 3.3 V rail sagging under **simultaneous peak current**,
and the new peak is the sum of three curves — two of them currently unknown. Reading them is
minutes of work and it must happen **before** `phase pool --apply`, because pool creation is
the highest-current moment in this entire plan (the forensics record drops happening *"during
the very first sustained write (initial ZFS pool creation)"*).

```bash
# per new drive, once physically installed:
sudo nvme id-ctrl -H /dev/nvmeXn1 | grep -iE "^ps |Relative|Max Power|Non-Operational"
sudo nvme get-feature /dev/nvmeXn1 -f 0x02      # confirm the cap actually applied
```

Record for each drive: **the lowest OPERATIONAL state and its wattage**, and whether PS2 is
that state. CLAUDE.md's own buying rule is *"lowest operational power state ≤ ~1.5–2 W"*.

⚠ **If PS2 is not an operational state on a drive, the udev rule silently does nothing for it**
— `nvme set-feature -f 0x02` would either error or select a non-operational state the
controller will not hold under load. Verify with `get-feature`, not by assuming the rule ran.
⚠ **Do not judge these drives on "DRAM-less."** The 980 is DRAM-less/HMB, but CLAUDE.md
explicitly warns that heuristic is contradicted by the community's own data (DRAM-less P310
and 990 EVO Plus both appear on the failing side). The power table is the real test.

### What is NOT known about these two drives, stated plainly

- **No fsync measurement exists for either.** ⚠ The audit's `SM961 3.44 ms/flush` figure is
  for the **256 GB `MZVPW256HEGL`** in the prd nodes — a different part. Do not transfer it;
  that is the same cross-part inference that invalidated the NAS PM9A1 ranking.
  For a bulk-sequential backup target this matters far less than it would for Postgres, so it
  is not a blocker — but it is unmeasured, not good.
- **No thermal history.** The SM961 is a Polaris-controller MLC part and runs warm; the ME
  mini has six M.2 slots in a small chassis. Watch `nvme smart-log` temperatures through the
  first scrub.
- **Mixed models in one vdev** is a deliberate acceptance, not an oversight: three different
  power curves, three different firmware families, three different failure modes. Homogeneity
  would have been the safer default; the operator has chosen availability of drives over it.
  ⚠ Record which drive fails first if one does — with a mixed vdev, that attribution is the
  only way to learn anything.

## Inventory — measured 2026-09-23

### Preserve (~1.2 GB total)

| What | Size | Why it cannot be rebuilt from git |
|---|---|---|
| **fresh `GiksDb` backup** | ~261 M | ⚠ **live production.** Taken from the database, not from MinIO. |
| **4 historical `GiksDb` fulls** | ~1 G | ⚠ keeps "restore to further back than today" — see § below |
| `/mnt/tank/system/talos/` | 53 M | ⚠ UPS orchestrator + talosctl + both `os:operator` configs |
| `/mnt/tank/system/tls/` | 140 K | ⚠ wildcard cert — see the LE rate-limit warning |
| `/mnt/tank/system/apps-config/` | 293 M | app state; most is reproducible but copying all of it is cheaper than deciding |
| `/mnt/tank/system/pxe/http/talos/` | 610 M | the only PXE content being kept |

### Destroy (~1.17 TB)

| What | prd | dev |
|---|---|---|
| `longhorn` | 231 G | 394 G |
| `etcd-snapshots` | 94 G | 56 G |
| `postgres-backups` | 56 G | 16 G |
| `mssql-backups` (after extraction) | 27 G | — |
| `loki-chunks` | 2.3 G | 2.9 G |
| `velero`, `postgres-backups-w1`, `pocket-id-litestream`, `sms-gateway-backups`, `cluster-agent` | ~0.5 G | ~0.6 G |
| `pxe/http/{extras,hw-validation,bios-config}` | 15.2 G | |

⚠ **Naming trap.** The *bucket* `velero` is 292 M. The *dataset* `tank/kube/*/velero` is
1.18 TB and contains **every** bucket, including GIKS v1. "Destroy the velero backups" and
"destroy the velero dataset" are wildly different statements — be explicit in every command.

### Retire entirely (operator decision, 2026-09-23)

- **meshcentral** (254 M) + **amtctl** (39 M) — AMT/vPro KVM into Q170S1 nodes. ⚠ The MS-A2
  has **no IPMI / BMC / vPro / AMT**, so these have no function after the migration.
- **homepage** (230 K) — cosmetic dashboard.
- **hw-validation** PXE assets (218 M) + `stress-results` dataset — Phase A scaffolding that
  never ran, whose `amtctl → PXE` trigger does not exist on the new hardware.
- **bios-config** PXE assets (679 K) — ASUS Q170S1 BIOS-as-code, "obsolete for AMD".
- **PXE `extras`** (15 G) — ubuntu-desktop/server ×3, systemrescue, gparted, clonezilla,
  shredos. ⚠ Keep a note that this removes the only netboot path; confirm how an MS-A2 gets
  re-imaged before relying on its absence.

### Keep as-is

**Per-env MinIO stays.** Two instances on two VLANs means dev CI credentials cannot reach
prd's GIKS backups. The B2 single-account comparison does not transfer — B2 is off-site DR
with Object-Lock, write-mostly, a different threat model. Collapsing to one instance would
put production backups within reach of the CI substrate to save one container.

---

## ⚠ The historical fulls, and why they are worth 1 GB

Destroying `mssql-backups` destroys a **90-day** restore window (2026-06-25 → today, daily
fulls + 30-minute `.trn` logs). After this rebuild you can restore only to the moment of the
fresh backup in Task 1.

That is fine for the realistic case ("restore yesterday") and wrong for one specific case:
**a corruption discovered later that started weeks ago.** Four historical fulls at ~250 MiB
each cover that for ~3% of the data. Take them.

⚠ They are **fulls only, no log chain** — so they restore to their own timestamp, not to an
arbitrary point. That is the trade being made knowingly.

---

## Task 1: Fresh GIKS v1 backup — the only step that touches production

**Host:** `docker-prd-01` (`10.10.10.19`, VLAN 10), MSSQL under `/opt/stacks/mssql/`

- [ ] **Step 1: Take a COPY_ONLY full via the mechanism v1 already uses**

⚠ **MinIO is still UP at this point** — it is not stopped until Task 4. So use `TO URL`, the
exact path v1 exercises every day, rather than `TO DISK` + a file retrieval. This needs **no
SSH access to `docker-prd-01`**, whose login is not documented anywhere in the estate, and it
uses a credential that is already proven working by the daily chain.

Connect to `Server=10.10.10.19,1433` — the connection string is in Doppler as
`infrastructure/dev` → `MIGRATOR_SOURCE_CONNECTION_STRING` (the SA password also lives on the
box at `/opt/stacks/giks-apps/.env`). Then:

```sql
BACKUP DATABASE GiksDb
  TO URL = 'https://s3-prd.w1.lv:9000/mssql-backups/box-prd/GiksDb/full/GiksDb-prepool-20260923.bak'
  WITH COPY_ONLY, COMPRESSION, CHECKSUM, STATS = 10;
```

⚠ `COPY_ONLY` is not optional — without it this becomes a new differential base and disturbs
the chain v1 is still writing. `CHECKSUM` makes the file self-verifying.

⚠ Reuse the existing `CREDENTIAL` that v1's nightly job already uses for this URL prefix; do
not mint a new one. If the exact URL form differs on the box, copy it from the existing job
rather than guessing — a `TO URL` typo fails at write time, which is safe, but a wrong
container/prefix silently lands it somewhere you will not think to look.

- [ ] **Step 2: Prove it is restorable, not merely present**

```sql
RESTORE VERIFYONLY
  FROM URL = 'https://s3-prd.w1.lv:9000/mssql-backups/box-prd/GiksDb/full/GiksDb-prepool-20260923.bak'
  WITH CHECKSUM;
```

Expected: `The backup set on file 1 is valid.`
⚠ A 261 MiB object that will not restore is not a backup. Do not proceed past this step on an
object-exists check alone.

- [ ] **Step 3: Pull it down and copy to TWO destinations**

```bash
mkdir -p ~/giks-v1-rescue
mc cp nas-prd/mssql-backups/box-prd/GiksDb/full/GiksDb-prepool-20260923.bak ~/giks-v1-rescue/
mc stat nas-prd/mssql-backups/box-prd/GiksDb/full/GiksDb-prepool-20260923.bak | grep -i etag
shasum -a 256 ~/giks-v1-rescue/GiksDb-prepool-20260923.bak
```

Then copy to a **second destination** — external drive, iCloud, or B2 — and compare the
sha256 **at the destination**, not just at the source.

⚠ From this moment until Task 8 Step 1 confirms v1's own chain has resumed, these copies are
the **only** backup of the live production database. One copy on one laptop is not enough for
the duration of a pool rebuild.

> **Alternative if `TO URL` cannot be used** (credential missing, or MinIO already stopped):
> `BACKUP DATABASE GiksDb TO DISK = '/var/opt/mssql/backup/GiksDb-prepool-20260923.bak'`
> with the same `WITH` clause, then `RESTORE VERIFYONLY FROM DISK`, then retrieve the file
> from the box. ⚠ That path needs shell access to `docker-prd-01`, which this plan cannot
> specify because the login is undocumented — establish it before relying on this branch.

---

## Task 2: Extract the historical fulls

- [ ] **Step 1: Pull four fulls spread across the window**

```bash
mkdir -p ~/giks-v1-rescue/historical
for d in 2026-06-25 2026-07-25 2026-08-24 2026-09-22; do
  mc cp "nas-prd/mssql-backups/box-prd/GiksDb/full/$(mc ls nas-prd/mssql-backups/box-prd/GiksDb/full/ | grep "$d" | awk '{print $NF}')" \
    ~/giks-v1-rescue/historical/
done
ls -la ~/giks-v1-rescue/historical/
```

Expected: four `.bak` files, ~241–261 MiB each.
⚠ If a date has no full (the ILM window may have moved), pick the nearest available rather
than skipping — the point is spread across the 90 days, not those exact dates.

- [ ] **Step 2: Verify each one**

Restore-verify each against MSSQL, or at minimum confirm the sha256 matches what MinIO
reports via `mc stat`. ⚠ Four unverified files are four hypotheses.

---

## Task 3: Pull the keep-list off the NAS

- [ ] **Step 1: Copy the four directories**

```bash
mkdir -p ~/nas-rescue
scp -r truenas_admin@nas.w1.lv:/mnt/tank/system/tls          ~/nas-rescue/
scp -r truenas_admin@nas.w1.lv:/mnt/tank/system/apps-config  ~/nas-rescue/
scp -r truenas_admin@nas.w1.lv:/mnt/tank/system/pxe/http/talos ~/nas-rescue/pxe-talos
du -sh ~/nas-rescue/*
```

⚠ `/mnt/tank/system/talos/` holds two `0600 root` talosconfigs that `scp` as
`truenas_admin` **cannot read**. They do not need copying — they are regenerated from
Doppler (`TALOS_NAS_SHUTDOWN_CONFIG_{DEV,PRD}`) by Task 7. Copy the directory for the
orchestrator script and note that the two configs will be absent.

- [ ] **Step 2: Record the TLS cert's expiry before the rebuild**

```bash
openssl x509 -in ~/nas-rescue/tls/*.crt -noout -subject -enddate
```

⚠ **Do not plan on re-issuing this cert.** Let's Encrypt limits 5 per week **per exact
identifier set**, and the NAS is an invisible additional holder of `*.w1.lv`. A rebuild that
forces a re-issue can leave the whole estate without certs. Restoring the saved copy avoids
the request entirely.

---

## Task 4: Quiesce

- [ ] **Step 1: Confirm the rescue set is complete and verified**

⚠ **ABORT POINT.** Do not proceed unless Task 1 Step 2 returned "backup set is valid",
Task 1 Step 3 has two copies with matching sha256, and Task 3 Step 1 shows non-zero sizes.

- [ ] **Step 2: Stop the apps**

```bash
for a in minio-prd minio-dev wiki pxe meshcentral amtctl homepage cluster-agent; do
  ssh truenas_admin@nas.w1.lv "sudo -n midclt call app.stop $a" || echo "  $a: not running / already stopped"
done
```

⚠ From here GIKS v1's `BACKUP ... TO URL` has no target and will fail every 30 minutes.
That is expected and is the window Task 1 exists to cover. Note the wall-clock time.

---

## Task 5: Destroy and rebuild the pool

- [ ] **Step 1: Export the pool**

```bash
# ⚠ Confirm the pool id first — do not trust the literal below.
ssh truenas_admin@nas.w1.lv "sudo -n midclt call pool.query" | python3 -c "
import json,sys
for x in json.load(sys.stdin): print('id=%s %s %s' % (x.get('id'), x.get('name'), x.get('status')))
"
# verified 2026-09-23: id=1 tank ONLINE
ssh truenas_admin@nas.w1.lv 'sudo -n midclt call pool.export 1 '"'"'{"destroy": false, "cascade": true}'"'"''
```

- [ ] **Step 2: Power off, swap drives**

⚠ **FULL POWER-OFF, not a reboot.** `docs/nvme-dropout-forensics.md`: the M.2 3.3 V rail
stays energised across a warm reboot, so a latched controller stays hung.

**Remove four:** `S4GRNX0NA00357`, `S4GRNX1RB33857`, `S6H2NF0WC37390`,
`S6H2NF0WC37392`. **Leave** `S4GSNF0N301379` and the boot drive `S444NX0N496890`.
**Install** the SM961 1 TB and the Samsung 980 1 TB.

⚠ Label `S6H2NF0WC37392` as faulty before it goes in a drawer, or it will be indistinguishable
from the three healthy pulls in six months. ⚠ Keep `S4GRNX1RB33857` findable — it is the
known-good 1 TB cold spare if a new drive misbehaves during bring-up.

- [ ] **Step 3: Verify the surviving drives by SERIAL, never by device name**

```bash
ssh truenas_admin@nas.w1.lv "sudo -n midclt call disk.query" | python3 -c "
import json,sys
for x in json.load(sys.stdin):
    if (x.get('name') or '').startswith('nvme'):
        print(x['name'], x.get('serial'), x.get('model'))
"
```

Expected exactly four: `S4GSNF0N301379`, the **SM961**, the **980**, and
`S444NX0N496890` (boot). ⚠ Record the two new serials into `config/storage.yaml`'s slot map
in the same commit — an undocumented serial is the thing that makes the next incident
unattributable.
⚠ **Enumeration reshuffles across reboots** — the 25.10.7 upgrade already swapped nvme1/nvme2.
A device-name check here will lie to you.

- [ ] **Step 4: Update the topology in git, then create the pool**

Edit `config/storage.yaml` § pool: 5-wide → 3-wide, and correct the slot map comment to the
three surviving serials. Then:

⚠ **GATE: do not run this until § *MANDATORY before pool creation* is done** — both new
drives' power tables read, PS2 confirmed applied via `get-feature`, and the results written
into `CLAUDE.md` § NVMe 3.3V-rail mitigations.

```bash
cd ~/github/truenas-infra
./manage.sh phase pool                              # DRY RUN — review the disk list
./manage.sh phase pool --apply --confirm=CREATE-TANK
```

⚠ **Keep every 3.3 V mitigation in place for this step.** The forensics record the drops
happening *"during the very first sustained write (initial ZFS pool creation)"* — pool
creation is the single highest-current moment in this whole plan. Do not relax anything
until Task 8.

- [ ] **Step 5: Recreate datasets, tasks, shares**

```bash
./manage.sh phase datasets --apply
./manage.sh phase storage-tasks --apply
./manage.sh phase shares --apply
```

⚠ Remove `tank/system/stress-results` from `config/storage.yaml` first — hw-validation is
retired, and recreating the dataset only to delete it later is churn.

---

## Task 6: Restore apps and data

- [ ] **Step 1: Restore TLS before anything that needs a cert**

```bash
scp -r ~/nas-rescue/tls truenas_admin@nas.w1.lv:/mnt/tank/system/
./manage.sh phase tls --apply
```

- [ ] **Step 2: Redeploy the surviving apps**

```bash
./manage.sh phase apps --apply
```

⚠ Remove `meshcentral`, `amtctl` and `homepage` from `config/apps.yaml` **before** this runs,
or they will be faithfully redeployed. Their per-app Doppler keys (`AMT_USER`, `AMT_PASSWORD`,
the `HOMEPAGE_VAR_*` mappings) should be dropped from `_DOPPLER_KEYS_PER_APP` in
`src/truenas_infra/modules/apps.py` in the same change — CLAUDE.md warns that a key listed
there but absent in Doppler makes `phase apps` fail loud.

- [ ] **Step 3: Recreate the MinIO buckets, users, lifecycle, encryption**

```bash
./scripts/setup-minio-buckets.sh
./scripts/setup-minio-users.sh
./scripts/setup-minio-lifecycle.sh
./scripts/setup-minio-encryption.sh
```

⚠ Order matters and is documented in CLAUDE.md. ⚠ `setup-minio-encryption.sh` SKIPs cleanly
if KMS is not yet configured — a skip is not a success; re-run it once KMS is live.

- [ ] **Step 4: Restore the PXE Talos assets**

```bash
scp -r ~/nas-rescue/pxe-talos truenas_admin@nas.w1.lv:/mnt/tank/system/pxe/http/talos
```

⚠ Do NOT restore `extras/`, `hw-validation/` or `bios-config/`. Prune the iPXE menu
(`apps/pxe/pxe-genmenu.sh` auto-lists from `extras/{utils,distros,live}/*.iso`, so an empty
tree yields an empty menu) and drop the now-dead `hw-validation.ipxe` entry from `tftp/`.

- [ ] **Step 5: Push the GIKS v1 rescue set back**

```bash
mc cp ~/giks-v1-rescue/GiksDb-prepool-20260923.bak \
  nas-prd/mssql-backups/box-prd/GiksDb/full/
mc cp --recursive ~/giks-v1-rescue/historical/ \
  nas-prd/mssql-backups/box-prd/GiksDb/full/
mc ls nas-prd/mssql-backups/box-prd/GiksDb/full/
```

Expected: five `.bak` objects.
⚠ Keep the laptop copies until Task 8 confirms v1's own chain has resumed. Do not delete the
rescue set because the upload "looked fine".

---

## Task 7: Re-stage the UPS shutdown path

⚠ **This is not optional and it is easy to forget.** `/mnt/tank/system/talos/` was destroyed
with the pool, taking `talosctl`, both `os:operator` configs and the orchestrator with it.
With PLP priced out of the MS-A2 build, this path is the compensating control protecting
Postgres from an unclean shutdown.

- [ ] **Step 1: Re-stage**

```bash
cd ~/github/truenas-infra
doppler run -p infrastructure -c ops -- ./scripts/setup-talos-shutdown-orchestrator.sh
```

- [ ] **Step 2: Verify the binary landed intact**

```bash
curl -fsSL https://github.com/siderolabs/talos/releases/download/v1.14.0/sha256sum.txt \
  | grep 'talosctl-linux-amd64$'
ssh truenas_admin@nas.w1.lv 'sha256sum /mnt/tank/system/talos/talosctl; /mnt/tank/system/talos/talosctl version --client'
```

Expected: checksums match, client reports `v1.14.0`.

- [ ] **Step 3: Verify the credential still authenticates**

```bash
ssh -t truenas_admin@nas.w1.lv 'sudo /mnt/tank/system/talos/talosctl \
  --talosconfig /mnt/tank/system/talos/prd-shutdown.talosconfig -n 10.10.5.11 version'
```

Expected `Server: v1.14.0`. ⚠ Needs an interactive password — `truenas_admin`'s NOPASSWD
allowlist covers `midclt`/`upscmd`/`upsrw`, not `talosctl`.

- [ ] **Step 4: Confirm `shutdowncmd` still points at the orchestrator**

```bash
ssh truenas_admin@nas.w1.lv "sudo -n midclt call ups.config" | python3 -m json.tool | grep -i shutdowncmd
```

Expected: `/mnt/tank/system/talos/nas-ups-orchestrator.sh`.
⚠ An empty `shutdowncmd` breaks Path-B DR orchestration entirely — `config/services.yaml`
§ nut carries a standing warning not to clear it.

---

## Task 8: Verify, then consider relaxing the mitigations

- [ ] **Step 1: GIKS v1's own chain has resumed**

```bash
mc ls nas-prd/mssql-backups/box-prd/GiksDb/log/ | tail -3
```

Expected: a `.trn` newer than the rebuild window. ⚠ **This is the step that ends the
single-copy exposure from Task 1.** Only after this is confirmed should the laptop rescue
copies be considered redundant — and even then, keep them until the next full cycle.

- [ ] **Step 2: Pool health**

```bash
ssh truenas_admin@nas.w1.lv 'zpool status tank; zpool list tank'
```

Expected: `raidz1-0 ONLINE`, three members, 0 errors, ~1.8 T usable.

- [ ] **Step 3: The verification matrix**

```bash
cd ~/github/truenas-infra && ./manage.sh phase verify
```

⚠ Update `docs/verification.md` first — its "Pool healthy" row expects **6 disks**, which
will now be wrong and would read as a failure.

- [ ] **Step 4: Wiki + PXE**

```bash
cd ~/github/wiki && ./tools/deploy.sh --verify
curl -s -o /dev/null -w "%{http_code}\n" http://10.10.5.10:8080/
```

- [ ] **Step 5: ⚠ Only now consider relaxing the 3.3 V mitigations**

The box goes from 6 drives to 4, cutting peak simultaneous current — the actual root cause.
But relax **one at a time, with a soak window**, or a recurrence cannot be attributed.

| Mitigation | Recommendation |
|---|---|
| **NVMe PS2 power cap** (udev) | ⚠ **Keep permanently.** Free, and the single largest reduction (~59% worst-case rail current). |
| `zfs_txg_timeout=1` | First candidate to relax — restore to 5, soak 2 weeks. |
| `zfs_vdev_async_write_max_active=4` | Second — restore to 10, soak 2 weeks. |
| `zfs_vdev_scrub_max_active=2` | Third. |
| `autotrim=off` | ⚠ **Last.** TRIM current bursts were the differentiator on the 2026-07-22 drop. |

⚠ The statistical bar in `nvme-dropout-forensics.md` is **42 days**. A quiet fortnight is
not evidence the fault is gone.

- [ ] **Step 6: Update the docs in the same change set**

`CLAUDE.md` § Hardware (6 NVMe → 4), § Storage Design (5-wide → 3-wide, drop the removed
drives), the MinIO bucket table if any bucket is retired, and `docs/verification.md`.
Then deploy the wiki — `CLAUDE.md` is auto-synced to `docs/projects/truenas-infra.md`.

---

## Abort points

| After | If this fails | Do |
|---|---|---|
| Task 1 Step 2 | `RESTORE VERIFYONLY` does not return valid | **STOP.** No copy of production exists. Diagnose before anything else. |
| Task 4 Step 1 | rescue set incomplete | **STOP.** Everything after this is irreversible. |
| Task 5 Step 3 | a serial is missing | **STOP.** A drive did not survive the power cycle — this is the 3.3 V fault, and the pool has not been created yet, so nothing is lost. |
| Task 5 Step 4 | pool creation drops a drive | Power-cycle fully, re-verify serials, retry once. Twice means a drive is bad. |
| Task 8 Step 1 | v1's chain has not resumed | Do **not** delete the laptop rescue copies. Fix the `BACKUP ... TO URL` target first. |

---

## Self-review

**Coverage:** every item in the measured inventory is either preserved (Tasks 1–3),
destroyed by Task 5, or explicitly retired with a reason. The operator's five decisions —
no MinIO backup preservation, fresh DB backup instead, retire meshcentral/amtctl/homepage,
PXE Talos-only, per-env MinIO kept, 3× 1 TB raidz1 — are all reflected.

**Known unknowns, surfaced rather than assumed:**
- whether the MS-A2 can be re-imaged without the PXE `extras` tree — flagged in § Retire,
  not resolved here
- ⚠ the SM961's and 980's **power tables are unread**, which is why § *MANDATORY before pool
  creation* gates `phase pool --apply` rather than trusting the udev rule to have applied
- ⚠ **no fsync measurement exists for either new drive**, and the audit's `SM961 3.44 ms`
  figure is for the 256 GB `MZVPW256HEGL`, a different part — explicitly not transferred
- mixing three drive models in one vdev is an accepted trade, recorded as such, not an
  oversight; the plan asks for the first failure to be attributed because that is the only
  way a mixed vdev teaches anything
- ⚠ `setup-minio-encryption.sh` SKIPs without KMS, and a skip reads like a pass

**Deliberately NOT in this plan:** relaxing the 3.3 V mitigations during the rebuild (Task 8
Step 5 gates it behind verification), and any change to the MS-A2 migration sequencing.
