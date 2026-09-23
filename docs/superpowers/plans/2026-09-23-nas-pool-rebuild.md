# NAS Pool Rebuild Implementation Plan

> **For agentic workers:** this is an OPERATIONAL runbook against live hardware, not a code plan. It is executed by the **main session with the operator present**, never by a subagent — CLAUDE.md § *Subagents are READ-ONLY on live clusters and appliances*. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Rebuild `tank` from a 5-wide RAIDZ1 to a 3-wide RAIDZ1, dropping ~1.17 TB of
disposable backup data, retiring **six** apps (pxe, meshcentral, amtctl, homepage,
stress-dashboard, iperf3) —
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

## ✅ Drive allocation — FINAL, measured 2026-09-23

All seven drives were health-checked and wiped individually via USB passthrough on
`10.10.10.19` before allocation. **Every drive passed SMART with zero media errors**; the
differences below are wear history and power draw, not health.

| serial | model | used | written | hours | unsafe | PS2 | → |
|---|---|---|---|---|---|---|---|
| `S649NF1R820750Y` | Samsung 980 | 1% | 9.99 TB | 1,275 | 59 | **2.19 W** | **NAS** |
| `S34DNX0JA02364` | SM961 MLC | 1% | 47.2 TB | 6,151 | 149 | **4.40 W** | **NAS** |
| `S4GSNF0N301379` | PM981a `-H7` | **0%** | 7.37 TB | 3,320 | 64 | **3.50 W** | **NAS** |
| `S444NX0N496890` | PM981 256G | 1% | 4.32 TB | 3,581 | 78 | 3.50 W | *boot-pool* |
| `S6H2NF0WC37390` | PM9A1 | 2% | 16.8 TB | 6,399 | **15** | 3.18 W | **prd — Postgres** |
| `S4GRNX1RB33857` | PM981a `-H1` | **0%** | 8.13 TB | 4,026 | 42 | 3.50 W | **prd — system+apps** |
| `S6H2NF0WC37392` | PM9A1 | 2% | 18.8 TB | 8,579 | ⚠ 51 | 3.18 W | ⚠ **dev — Postgres** |
| `S4GRNX0NA00357` | PM981a `-H1` | 2% | ⚠ 74.8 TB | ⚠ 26,065 | 58 | 3.50 W | **dev — system+apps** |

### Why identical drives in dev and prd

Operator decision, and it is the strongest argument in the allocation: **same model, same
firmware, same power curve in both boxes means dev actually VALIDATES prd** rather than
approximating it. Dev exists as the compensating control for prd having no node-level HA; a
dev that behaves differently at the storage layer compensates for less than it appears to.
⚠ This outranked per-box power optimisation, which was the earlier (weaker) framing.

### ⚠ The dev Postgres drive is the Mode-B unit, knowingly

`S6H2NF0WC37392` carries the firmware-hang history (`CSTS=0x1`, controller alive and
answering; fault followed a physical reslot). SMART reads perfectly clean — **expected, since
SMART cannot see a firmware hang** — with weak corroboration in 51 unsafe shutdowns on 342
cycles against its twin's 15 on 751.

Placed on **dev**, the lowest-stakes slot available. ⚠ If it hangs, dev's Postgres needs a
Barman restore or a DataMigrator re-run rather than a reinstall — hours, not data loss.
**Watch for `Device not ready; aborting reset, CSTS=0x1` with the drive still enumerated.**
That signature means this drive, not the MS-A2.

### ⚠ Which drive holds Postgres is PROVISIONAL until benched

The operator's reasoning for PM9A1-as-database is "more modern, more IO". ⚠ For Postgres the
metric is **fsync latency at QD1**, not IOPS, and those diverge. The only measurement taken —
PM9A1 4.0–4.2 ms vs PM981a 1.38 ms — was confounded three ways (power throttling, Gen3 x1,
the 3.3 V fault) and was retracted. **Confounded means unknown, not reversed.**

✅ **Agreed 2026-09-23: remeasure first, as step one of MS-A2 bring-up.** The `fio` test in
`kube-infra/docs/msa2-hardware-day.md` § 1a runs before Talos is installed, so the role
assignment is not locked until then. If the numbers disagree with the datasheet, swap the
roles — the drives stay in the same box either way, so nothing else changes.
⚠ Do NOT bench over USB. The bridge imposes its own flush semantics; that is precisely what
contaminated the retracted measurement.

### 🚫 Nothing was disposed of

All seven drives are in service. There is no cold spare — a replacement means buying one.

## Inventory — measured 2026-09-23

### Preserve (~1.2 GB total)

| What | Size | Why it cannot be rebuilt from git |
|---|---|---|
| **fresh `GiksDb` backup** | ~261 M | ⚠ **live production.** Taken from the database, not from MinIO. |
| **4 historical `GiksDb` fulls** | ~1 G | ⚠ keeps "restore to further back than today" — see § below |
| `/mnt/tank/system/talos/` | 53 M | ⚠ UPS orchestrator + talosctl + both `os:operator` configs |
| `/mnt/tank/system/tls/` | 140 K | ⚠ wildcard cert — see the LE rate-limit warning |
| `/mnt/tank/system/apps-config/` | 293 M | app state; most is reproducible but copying all of it is cheaper than deciding |
| ~~`/mnt/tank/system/pxe/http/talos/`~~ | — | ⚠ **NOT preserved — PXE is removed entirely, see Task 9** |

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
- ⚠ **`stress-dashboard`** — FOUND 2026-09-23, not in the original retire list. It bind-mounts
  `/mnt/tank/system/stress-results:/data:rw`, the dataset this plan deletes. Left in place it
  would restore pointing at a path that no longer exists.
- ⚠ **`iperf3`** — FOUND 2026-09-23, same omission. Its only purpose is hw-validation's
  `net-mgmt` / `net-data` iperf3 pairs (`hw-validation/README.md`), and hw-validation is
  retired. An always-on listener with no consumer.
- **bios-config** PXE assets (679 K) — ASUS Q170S1 BIOS-as-code, "obsolete for AMD".
- **PXE — ENTIRELY** (15.8 G + the whole service). Decision revised 2026-09-23 from
  "keep Talos images only" to full removal. See **Task 9** for the cross-repo work and
  § *Why PXE goes completely* for the reasoning.

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

## Why PXE goes completely

Decision revised 2026-09-23, after checking what the cached images actually are.

**The cached images are built for the wrong hardware.** Schematic
`daef782be84917c1a01cd56bb73b9ad8057e6d746b0a0c7d34e34d03fb858d43`, read from
factory.talos.dev, contains exactly three extensions:

| extension | status on MS-A2 |
|---|---|
| `siderolabs/intel-ucode` | ⚠ **Intel-only — the MS-A2 is AMD** |
| `siderolabs/iscsi-tools` | ⚠ Longhorn-only — Longhorn is dropped |
| `siderolabs/util-linux-tools` | ⚠ Longhorn-only — Longhorn is dropped |

All three are wrong for the new build. "Keep PXE" was therefore never the free option — it
meant authoring a new AMD schematic, keeping `apps/pxe/schematic.yaml` byte-identical to
`kube-infra/talos-os/schematic.yaml`, and re-verifying a boot path on hardware that has never
PXE-booted.

**PXE is not used for upgrades.** `talosctl upgrade` pulls the installer from the registry.
PXE serves exactly two scenarios: initial install, and re-imaging a dead box. With **two**
nodes and **no BMC/IPMI/AMT** on the MS-A2, both require standing at the machine — where a
USB stick is equivalent.

**It fails where it would be wanted most.** PXE depends on the NAS. In a full-estate rebuild
the NAS is part of the rebuild; a USB stick is not.

**The cache is not what guarantees a correct image.** `factory.talos.dev` builds on demand
from the schematic. The schematic is the source of truth and lives in
`kube-infra/talos-os/schematic.yaml`. ⚠ A local cache is a second copy that can drift — and
`config/talos.yaml` documents that a previous mirror list *did* drift, still listing
`nut-client` months after it was stripped and `gvisor` which was never in the schematic.

**What replaces it:** a runbook paragraph — open factory.talos.dev, paste the schematic,
download the ISO, write the stick. ⚠ **Do this once BEFORE the old estate is torn down**, so
the procedure is proven rather than theoretical.

⚠ **Accepted loss:** the iPXE version-picker menu (boot any of 5 cached Talos versions) and
the `install-wipe` menu entry. `talosctl upgrade --image` covers rollback without re-imaging.

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

⚠ Remove `meshcentral`, `amtctl`, `homepage`, `stress-dashboard`, `iperf3` and `pxe` from
`config/apps.yaml` **before** this runs,
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

- [ ] **Step 4: (removed — PXE is not restored)**

⚠ The original plan restored `pxe/http/talos`. PXE is now removed entirely; nothing is
restored here and `tank/system/pxe` is not recreated. See Task 9.

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

---

## Task 9: Remove PXE entirely — three repos

⚠ **Do this AFTER Task 8 verifies the rebuild**, not during. The pool rebuild already
destroys the PXE data; this task removes the *service, config and router options* so nothing
is left advertising a boot path that no longer exists.

⚠ **Prerequisite, and it is not optional:** prove the USB install path **before** the old
estate is torn down. Open `factory.talos.dev`, paste
`kube-infra/talos-os/schematic.yaml` (with `amd-ucode` for the MS-A2), download the ISO,
write a stick, and boot something from it. A replacement procedure that has never been run is
not a replacement.

### 9a — `truenas-infra`

- [ ] **Step 1: Remove the app and its config**

```bash
cd ~/github/truenas-infra
git rm -r apps/pxe/
git rm config/talos.yaml docs/pxe-operator.md docs/talos-updater-setup.md docs/bios-apply-pxe-setup.md
```

Then edit:
- `config/apps.yaml` — delete the `- name: pxe` entry (lines ~20-24)
- `config/dns.yaml` — delete the `pxe.w1.lv` record (→ 10.10.5.20)
- `config/storage.yaml` — delete the `tank/system/pxe` dataset
- ⚠⚠ **`src/truenas_infra/modules/apps.py` — SCOPE CORRECTED 2026-09-23, this is NOT a small
  edit.** The plan said "remove `load_talos_config()` and the pxe cronjob wiring". Measured:
  **18 top-level functions / ~1,136 lines** of a 1,828-line module reference retired apps —
  `ensure_talos_updater` (202 lines), five `_ensure_pxe_*_via_ctx` helpers,
  `ensure_pxe_menu_files`, `ensure_pxe_build_context`, `_ensure_stress_dashboard_*`,
  `_ensure_homepage_*`, `_ensure_meshcentral_*`, `_ensure_amtctl_*`, plus their call sites in
  `run()`.
  ⚠ Several of those matches are shared infrastructure that merely MENTIONS a retired app in
  a comment or dispatch branch — `ensure_custom_app`, `ensure_file_on_nas`, `run`,
  `_ensure_cluster_agent_config_via_ctx`. Those must NOT be removed. A regex sweep over the
  retired names will delete working code.
  ⚠ Removing the helpers without their call sites **breaks the module import**, so it cannot
  be done incrementally in small commits — it is one atomic change.
  **Attempted and REVERTED 2026-09-23**: too large to land safely at the tail of the rebuild.
  It deserves its own PR with the diff actually reviewed, not folded into a cleanup list.
  ✅ The dead wiring is INERT meanwhile — `talos_updater_cronjob_ensured` and
  `pxe_download_cronjob_ensured` both report `action=noop changed=False`, so nothing is
  created and nothing breaks.

> ⚠ **BEFORE TOUCHING THIS MODULE: the test suite has 5 PRE-EXISTING failures.** Verified
> 2026-09-23 by checking out `ad3080f` (the commit before the rebuild) and re-running — they
> fail identically there, so they are NOT rebuild damage:
> `test_apps.py::test_run_configures_docker_pool_and_apps` (AttributeError),
> `test_network.py::test_run_creates_vlans_and_commits` (AttributeError), and three
> `test_verify.py` cases (StopIteration — exhausted mock side-effect lists).
> ⚠ Anyone refactoring `apps.py` needs this baseline, or they will attribute a pre-existing
> failure to their own change — or worse, dismiss a real regression as "one of the known
> ones". 230 pass.
- `tests/test_apps.py`, `tests/test_verify.py` — remove the PXE cases
- `docs/verification.md` — remove the PXE rows ⚠ (also fix the "Pool healthy … 6 disks" row
  per Task 8 Step 3)

- [ ] **Step 2: ✅ RESOLVED — `pxe.w1.lv` has no Traefik route**

Enumerated 2026-09-23: `apps/traefik/routes.yaml` defines seven routes (mc, minio-prd,
minio-dev, wiki, home, amtctl, stress) and **none of them is `pxe.w1.lv`**. The DNS record
has been pointing at 10.10.5.20 with nothing behind it. Only the DNS side needs removing —
see **9c Step 2**.

- [ ] **Step 3: Apply**

```bash
./manage.sh phase apps          # DRY RUN — expect pxe to disappear from the plan
./manage.sh phase apps --apply
./manage.sh phase dns --apply
```

⚠ Verify the container is actually gone, not merely absent from config:
```bash
ssh truenas_admin@nas.w1.lv "sudo -n midclt call app.query" | grep -i pxe || echo "  ✅ no pxe app"
```

### 9b — `mikrotik-infra` ⚠ the router will NOT converge by itself

- [ ] **Step 1: Edit `configs/fleet.yaml`**

Remove the three PXE keys from the mgmt network:

```yaml
# before
- {subnet: 10.10.5.0/24,  gateway: 10.10.5.1,  dns: 10.10.0.1,
   pxe: true, pxe_bootfile: ipxe.efi, pxe_next_server: 10.10.5.10}
# after
- {subnet: 10.10.5.0/24,  gateway: 10.10.5.1,  dns: 10.10.0.1}
```

…and delete the three option keys below the `networks:` block:

```yaml
option_set_name: pxe-boot
option_name: pxe-bootfile
option_value: "'ipxe.efi'"
```

- [ ] **Step 2: Edit `configs/templates/01-router.rsc.j2`**

Delete the two unconditional emits (lines 16-17):
```
/ip dhcp-server option add code=67 name={{ fleet.dhcp.option_name }} value="{{ fleet.dhcp.option_value }}"
/ip dhcp-server option sets add name={{ fleet.dhcp.option_set_name }} options={{ fleet.dhcp.option_name }}
```
…and the three `net.pxe`-conditional fragments on the `dhcp-server network add` line.

⚠ Steps 1 and 2 must land together — the template dereferences `fleet.dhcp.option_name`
unconditionally, so removing only the fleet.yaml keys makes rendering fail.

- [ ] **Step 3: ⚠⚠ REMOVE THE LIVE OPTIONS BY HAND — the tooling cannot**

`tools/apply_fleet.py` delta mode is **additive-only**. Its own docstring:
*"extras are legitimately preserved, MISSING never is."* So deleting the config removes the
**intent** but leaves the RB5009 still advertising `next-server=10.10.5.10` and
`boot-file-name=ipxe.efi` forever.

The alternative — `--mode full-reset` — **reboots the RB5009, the only router in the house**.
Not worth it for three properties. Remove them manually, in this order (the network
references the option-set, so the reference goes first):

```
/ip dhcp-server network set [find address=10.10.5.0/24] !boot-file-name !dhcp-option-set !next-server
/ip dhcp-server option sets remove [find name=pxe-boot]
/ip dhcp-server option remove [find name=pxe-bootfile]
```

⚠ Use a single SSH ControlMaster session — RouterOS trips `login-failure-limit` on rapid
repeat logins and returns `Permission denied` for 1–5 minutes even with correct credentials.

- [ ] **Step 4: Prove the router and the config agree**

```bash
cd ~/github/mikrotik-infra && ./manage.sh   # audit
```

Expected: **green**, with no live-only extras for `dhcp-server option` / `network`.
⚠ A green audit *before* Step 3 would be the additive-only blind spot, not success — run the
audit only after the manual removal.

- [ ] **Step 5: Confirm a client still gets a lease**

```bash
ssh <router> '/ip dhcp-server lease print where server=mgmt-dhcp'
```
⚠ The mgmt DHCP server itself must keep working — only the boot fields are going.

### 9c — Traefik routes + DNS records

⚠ These cover **all six** retirements, not just PXE. Left behind they are hostnames that
resolve to a proxy with nothing behind them — which fails as a timeout, not a clear error.

- [ ] **Step 1: Delete four Traefik routes** — `apps/traefik/routes.yaml`

| route | service | verdict |
|---|---|---|
| `Host(\`mc.w1.lv\`)` | meshcentral | ❌ delete |
| `Host(\`home.w1.lv\`)` | homepage | ❌ delete |
| `Host(\`amtctl.w1.lv\`)` | amtctl | ❌ delete |
| `Host(\`stress.w1.lv\`)` | stress-dashboard | ❌ delete |
| `Host(\`minio-prd.w1.lv\`)` | minio-prd-console | ✅ keep |
| `Host(\`minio-dev.w1.lv\`)` | minio-dev-console | ✅ keep |
| `Host(\`wiki.w1.lv\`)` | wiki | ✅ keep |

⚠ **`pxe.w1.lv` has NO Traefik route** — confirmed 2026-09-23 by enumerating the file. The
DNS record has been pointing at 10.10.5.20 with nothing serving it. That resolves the
open question flagged in 9a Step 2: the record is dangling, so only the DNS side needs work.

Traefik drops from 7 routes to 3. ⚠ Update its `config/apps.yaml` description too —
"mgmt-plane reverse proxy (mc/pxe/minio-*/traefik-nas)" names two hosts that no longer exist.

- [ ] **Step 2: Delete the retired-app DNS records** — `config/dns.yaml`

```
mc.w1.lv        10.10.5.20   MeshCentral UI
pxe.w1.lv       10.10.5.20   ⚠ dangling — no route ever existed
home.w1.lv      10.10.5.20   Homepage dashboard
amtctl.w1.lv    10.10.5.20   AMT power control dashboard
stress.w1.lv    10.10.5.20   hw-validation report viewer
```

- [ ] **Step 3: ⚠ Decide the six node AMT records**

`config/dns.yaml` carries `kub-{prd,dev}-0{1,2,3}.w1.lv`, all commented **"AMT (mgmt NIC)"**.
The MS-A2 has no AMT, so the *purpose* is gone for all six — but the *names* are not
symmetric:

- `kub-prd-02/03` and `kub-dev-02/03` — ❌ **delete.** Those nodes cease to exist.
- ⚠ `kub-prd-01` and `kub-dev-01` — **decide, do not delete by reflex.** The MS-A2 boxes take
  those IPs and keep those cluster names (`ETCD_NODE_1: 10.10.5.11` / `10.10.5.14`). The
  records may be worth **re-pointing and re-commenting** as plain node addresses rather than
  removed. ⚠ Deleting them silently is the wrong default — something may resolve them.

- [ ] **Step 4: Apply, and note this tool DOES converge**

```bash
cd ~/github/truenas-infra && ./manage.sh phase apps --apply    # traefik picks up routes.yaml
cd ~/github/mikrotik-infra && ./manage.sh                      # DNS sync
```

✅ **Unlike the DHCP path, DNS removal works.** `tools/sync_dns.py` computes
`to_remove = [r for name, r in managed_live.items() if name not in managed_desired]`
(line 203) and emits `/ip dns static remove [find name=…]` (line 254). Deleting a record
from `dns.yaml` really does delete it from the router.

⚠ **But only for records it owns.** The idempotency key is the exact comment
`managed-by-claude`; records with any other comment (or none) are *preserved and reported,
never removed*. Before trusting the sync, confirm the records being deleted actually carry
it:

```bash
ssh <router> '/ip dns static print detail where name~"mc.w1.lv|pxe.w1.lv|home.w1.lv|amtctl.w1.lv|stress.w1.lv"'
```

⚠ Any of those lacking `comment=managed-by-claude` must be removed by hand — same as the
DHCP options in 9b Step 3.

> ⚠ **Two tools in the same repo, two different behaviours — do not generalise either.**
> `sync_dns.py` is **convergent** (adds *and* removes). `apply_fleet.py` delta is
> **additive-only** (*"extras are legitimately preserved, MISSING never is"*). Assuming the
> fleet behaviour for DNS would leave you hand-deleting records that were already gone;
> assuming the DNS behaviour for DHCP leaves the router advertising a dead boot server
> forever. 9b Step 3 exists precisely because of that asymmetry.

- [ ] **Step 5: Verify nothing dangles**

```bash
for h in mc pxe home amtctl stress; do printf "%-8s " "$h"; dig +short $h.w1.lv | head -1 || true; echo; done
for h in wiki minio-prd minio-dev traefik-nas nas; do printf "%-12s " "$h"; dig +short $h.w1.lv | head -1; done
```

Expected: the first five return nothing; the survivors still resolve.
⚠ Check the survivors too — a botched sync that removes too much reads identically to a
successful one if you only look at what you meant to delete.

### 9d — docs across repos

- [ ] `truenas-infra/CLAUDE.md` — Planned Services table (PXE/TFTP row), § Network
      (`.10` description), § File Structure, and the § Wiki maintenance matrix rows for
      `apps/pxe/pxe-download.sh` and `docs/bios-apply-pxe-setup.md`
- [ ] `mikrotik-infra/CLAUDE.md` — § PXE Boot (delete), the `.10` and `.20` IP descriptions,
      the related-repos row calling truenas-infra the "PXE server", and the § manage.sh line
      mentioning PXE. ⚠ Its § PXE Boot already documents that the router fallback never
      worked (M-H3) — that whole discussion goes with it.
- [ ] `kube-infra` — ⚠ keep `talos-os/schematic.yaml`; it is now the **only** copy, which
      removes the byte-identical sync hazard rather than creating one. Update it for AMD
      (`amd-ucode`, drop `iscsi-tools` + `util-linux-tools`) and record the new schematic ID.
- [ ] `wiki` — `docs/runbooks/pxe-operator.md` and `docs/runbooks/bios-apply-pxe-setup.md` are
      auto-synced from truenas-infra; removing the sources orphans them. Remove the entries
      from `wiki/sync-map.yaml`, delete the pages, and prune `pxe.w1.lv` from
      `docs/architecture/hostnames.md` + `docs/reference/links.md`. Then:
      `cd ~/github/wiki && ./tools/deploy.sh --verify`
- [ ] `bios-config` — ⚠ already "obsolete for AMD" per the MS-A2 audit. Out of scope here;
      retire it in its own change rather than by implication.

### 9e — verification

- [ ] **No orphan DNS:** `dig +short pxe.w1.lv` returns nothing (or NXDOMAIN)
- [ ] **No orphan boot advertisement:** a fresh DHCP lease on VLAN 5 carries no
      `next-server` / `boot-file-name`
- [ ] **The replacement works:** a USB stick written from the AMD schematic boots a machine
      into Talos maintenance mode ⚠ — proven, not assumed
- [ ] **No orphan hostnames:** the five retired records return nothing from `dig`, and the
      survivors (`wiki`, `minio-prd`, `minio-dev`, `traefik-nas`, `nas`) still resolve —
      ⚠ check both halves, not just what you meant to delete
- [ ] **Traefik serves exactly three routes** and its dashboard at `traefik-nas.w1.lv` is up
- [ ] **Nothing else regressed:** `./manage.sh phase verify` on truenas-infra, and the wiki
      deploys clean


## Abort points

| After | If this fails | Do |
|---|---|---|
| Task 1 Step 2 | `RESTORE VERIFYONLY` does not return valid | **STOP.** No copy of production exists. Diagnose before anything else. |
| Task 4 Step 1 | rescue set incomplete | **STOP.** Everything after this is irreversible. |
| Task 5 Step 3 | a serial is missing | **STOP.** A drive did not survive the power cycle — this is the 3.3 V fault, and the pool has not been created yet, so nothing is lost. |
| Task 5 Step 4 | pool creation drops a drive | Power-cycle fully, re-verify serials, retry once. Twice means a drive is bad. |
| Task 8 Step 1 | v1's chain has not resumed | Do **not** delete the laptop rescue copies. Fix the `BACKUP ... TO URL` target first. |
| Task 9 prerequisite | the USB install path has not been proven | **STOP.** Do not remove PXE until its replacement has actually booted a machine. |
| Task 9b Step 3 | the manual RouterOS removal errors | Leave the options in place — they are inert once the NAS stops answering. ⚠ Do NOT reach for `--mode full-reset` to force it; that reboots the only router in the house. |

---

## Self-review

**Survivors, stated explicitly so the retire list is falsifiable:** after this plan the NAS
runs **five** apps — `minio-prd`, `minio-dev`, `traefik`, `wiki`, `cluster-agent` — down from
eleven. `plex` and `qbittorrent` remain declared but `enabled: false` with empty datasets;
⚠ decide whether to drop the entries entirely rather than carrying dead config.
⚠ `traefik`'s own description ("mgmt-plane reverse proxy for mc/pxe/minio-*/traefik-nas") goes
stale here — two of its four routes are being deleted. Prune `routes.yaml` and the matching
`config/dns.yaml` records for `mc.w1.lv` and `pxe.w1.lv` in the same change.

**Coverage:** every item in the measured inventory is either preserved (Tasks 1–3),
destroyed by Task 5, or explicitly retired with a reason. The operator's five decisions —
no MinIO backup preservation, fresh DB backup instead, retire meshcentral/amtctl/homepage,
PXE removed **entirely** (revised 2026-09-23 from Talos-only), per-env MinIO kept,
3× 1 TB raidz1 — are all reflected.

**⚠ The PXE removal's load-bearing finding:** `tools/apply_fleet.py` delta mode is
additive-only (*"extras are legitimately preserved, MISSING never is"*), so deleting the
router config removes the intent but **not** the live options. Task 9b Step 3 removes them by
hand rather than reaching for `--mode full-reset`, which reboots the only router in the house.
A green audit run *before* that manual step would be the blind spot, not success.

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
