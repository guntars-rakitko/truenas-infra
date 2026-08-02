# Beelink ME Mini — NVMe dropout forensics

**Status:** open. Root cause partially identified; monitoring.
**Last updated:** 2026-08-02.
**Tracking issue:** [truenas-infra#109](https://github.com/guntars-rakitko/truenas-infra/issues/109).

Kernel-log forensics for the recurring NVMe dropouts on `nas.w1.lv`
(Beelink ME Mini, post-RMA "v2" unit — 16 GB, no eMMC, BIOS M1V404).

For ~6 weeks this was diagnosed from inference and community reports. On
2026-08-02 the actual kernel logs were finally read. **They show two distinct
failure modes, not one** — which is why every single-cause theory kept failing
to predict the next incident.

---

## TL;DR

1. **There are two separate problems.** A genuinely faulty drive (PM9A1
   `S6H2NF0WC37392`), *and* a power-like instant-death fault affecting slots
   04 and 07. Fixing one will not fix the other.
2. **PCIe signal integrity is ruled out** — zero AER events in six weeks.
3. **`…392` is a bad drive, on evidence** — its fault followed it across a
   physical reslot, and its last failure was a firmware hang with the device
   still electrically alive.
4. **The remaining fault (slots 04/07) is consistent with the community's
   3.3 V rail theory** but is not independently proven on this unit.

---

## Incident record

`nvmeN` numbering **reshuffles across boots** — always anchor to the PCI
address (stable per physical slot) or the drive serial. Never to `nvmeN`.

| When (UTC) | PCI addr | Drive | Kernel signature | Mode |
|---|---|---|---|---|
| 2026-06-26 03:03 | 07:00.0 | PM9A1 `…392` | `CSTS=0xffffffff PCI_STATUS=0x10` | A |
| 2026-07-06 14:12 | 07:00.0 | PM9A1 `…392` | `CSTS=0xffffffff PCI_STATUS=0x10` | A |
| 2026-07-19 03:09 | 04:00.0 | PM981a `…357` | `CSTS=0xffffffff PCI_STATUS=0xffff` | A |
| 2026-07-22 03:03 | 04:00.0 | PM981a `…357` | `CSTS=0xffffffff PCI_STATUS=0xffff` | A |
| 2026-07-29 02:07→02:10 | 08:00.0 | PM9A1 `…392` | 51 I/O timeouts → `Device not ready; aborting reset, CSTS=0x1` | **B** |

`…392` was physically reslotted 07 → 08 on 2026-07-06. It failed in **both**
slots.

---

## Mode A — instant death (4 events)

```
nvme nvmeN: controller is down; will reset: CSTS=0xffffffff, PCI_STATUS=0x10
nvme nvmeN: Disabling device after reset failure: -19
```

Characteristics:

- **Zero warning.** No preceding I/O timeouts, no aborts, no degradation. The
  device is present and healthy one instant and gone the next.
- `CSTS=0xffffffff` — the controller's register space reads all-ones, i.e. it
  is not responding at all.
- **No AER events** before, during or after.

Two sub-variants, which may matter:

| `PCI_STATUS` | Meaning | Seen at |
|---|---|---|
| `0x10` | PCIe **config space still readable** — the link endpoint survived while the controller core died | slot 07 |
| `0xffff` | Config space also all-ones — the device vanished from the bus entirely | slot 04 |

**Interpretation:** instant failure + no warning + no link errors is the
classic signature of a power event (brownout). It is *not* what a failing
link or a degrading drive looks like — those produce correctable errors,
retries, or timeouts first. This is consistent with (but does not by itself
prove) the community's undersized-3.3 V-rail theory.

## Mode B — firmware hang (1 event, `…392` only)

```
nvme nvme5: I/O tag 884 ... QID 2 timeout, aborting req_op:FLUSH(2)
nvme nvme5: Abort status: 0x0
   ... 51 timeouts / aborts over ~3 minutes ...
nvme nvme5: I/O tag 321 ... timeout, reset controller
nvme nvme5: Device not ready; aborting reset, CSTS=0x1
nvme nvme5: Disabling device after reset failure: -19
```

Characteristics:

- A **3-minute cascade** of I/O command timeouts (FLUSH, READ, WRITE). Aborts
  return status `0x0` — accepted — but the commands never complete.
- The reset then fails with **`CSTS=0x1`**: the controller is **alive and
  answering register reads**. It simply refuses to complete the reset
  handshake.

**Interpretation: this is not a power event.** Dead or unpowered silicon
cannot answer register reads. A drive that responds with `CSTS=0x1` is
powered, on the bus, and internally hung. This is a controller/firmware
fault in the drive itself.

---

## What this rules out

| Hypothesis | Verdict | Evidence |
|---|---|---|
| **PCIe signal integrity / marginal contact** | **Ruled out** | **Zero AER events** in six weeks of logs — no correctable, no uncorrectable, no bad TLP, no link-training failures. A marginal link produces correctable errors long before it drops a device. |
| **ASM2824 PCIe switch erratum** | **Not applicable to this unit** | This unit has a **flat topology**: each NVMe sits directly on its own Alder Lake-N PCH root port (`00:1c.2/.3/.6`, `00:1d.0/.2/.3` → buses 03–08). There is no PCIe switch. `lspci -t` confirms. |
| **A single common cause** | **Ruled out** | Modes A and B are mutually exclusive electrically (device gone vs device responding). |
| **Average power draw** | **Insufficient** | An NVMe operational power-state cap (PS2, ~59 % lower rated draw) was verified live for 9 days and a drop still occurred. Note the interval data is too sparse to say it did nothing (see *Evidence bar*). |
| **The "v2" board revision fixed it** | **False** | This *is* the post-RMA v2 unit (16 GB, no eMMC). Five drops. Corroborated by other owners whose post-fix replacements also still drop. |

## What is still unknown

- Whether Mode A is genuinely a 3.3 V rail brownout. The signature is
  *consistent* with it, but no direct electrical measurement has been taken on
  this unit. The community's evidence (oscilloscope traces showing 3.3 V
  glitching while 12 V stays clean; an external 90 W PSU not helping) comes
  from other people's boards.
- Whether the 3.3 V rail is one shared plane or several per-slot groups. The
  FCC filing's schematics are withheld ("Metadata only"); only low-resolution
  internal photos are public. Counting the DFN power ICs near inductors
  PL1/PL2 with the lid off would settle it.
- Why Mode A produced `PCI_STATUS=0x10` at slot 07 but `0xffff` at slot 04.

---

## Reading the kernel logs

Several non-obvious blockers, all hit during this investigation:

1. **`kernel.dmesg_restrict=1` by default** → `dmesg` is root-only. Now
   relaxed via a `SYSCTL` tunable (`kernel.dmesg_restrict=0`).
2. **`truenas_admin` cannot be added to `adm` or `systemd-journal`.** TrueNAS
   middleware rejects it:
   `membership of this builtin group may not be altered`. This applies to the
   UI as well — it is not an API-only restriction. So `journalctl` is
   unavailable to that account, permanently.
3. **`journalctl -k` implies `-b`** (current boot only). To read across boots:
   ```sh
   journalctl --no-pager --since 2026-06-20 _TRANSPORT=kernel
   ```
   Using `-k` here silently returns only today's log — an easy way to
   conclude "there's nothing there" when there is.
4. **Workaround that works:** a temporary **root cron job** that dumps the
   journal to a world-readable path under `/mnt/tank/system/nvme-diag/`.
   Delete the cron afterwards.

The journal **is** persistent (`/var/log/journal`, ~51 MB, boots back to
2026-06-24), so historical incidents remain recoverable.

---

## Automated capture (armed)

Cron `nvme-drop-capture` runs every minute as root. When
`zpool status -x tank` is not healthy it snapshots to
`/mnt/tank/system/nvme-diag/<UTC-timestamp>/`:

`dmesg` (full + NVMe/PCIe-filtered) · kernel journal (last 45 min) ·
**PCIe AER counters** · `nvme error-log` + `smart-log` per drive · PCIe link
state · controller states · temperatures · load · live ZFS/NVMe tunables ·
UPS state.

One capture per incident, gated by a `.incident-active` marker that clears
when the pool returns to healthy. It does not send mail — TrueNAS's own
`VolumeStatus` alert does that.

Script lives at `/mnt/tank/system/nvme-diag/capture.sh`, staged from
`/home/truenas_admin/nvme-drop-capture.sh`. (`cronjob.create` caps the
command field at 1024 characters, and `/mnt/tank/system/*` is not writable by
`truenas_admin` — hence the staging dance.)

### Triaging the next incident

Read `04-dmesg-nvme-pcie.txt` first and classify:

| Observation | Mode | Means |
|---|---|---|
| No preceding timeouts; `CSTS=0xffffffff` | **A** | Power-like. Check `PCI_STATUS` (`0x10` vs `0xffff`) and `08-pcie-aer-counters.txt`. |
| I/O timeouts first; `CSTS=0x1` on reset | **B** | Drive firmware hang. That drive is faulty. |
| **Any** non-zero AER counter | — | New information — revisit the ruled-out signal-integrity hypothesis. |

**AER baseline as of 2026-08-02: all zero.** NVMe `error_count: 0`.

---

## Evidence bar — how long is long enough

Inter-incident gaps: **10, 13, 3, 7 days** (n = 4, mean ≈ 8.25 d). A 95 %
confidence interval on that mean is roughly **[3.8, 30] days** — the MTBF is
not known within an order of magnitude.

P(zero incidents in *T* days | nothing changed) = exp(−T / 8.25):

| Clean run | P(by chance) | Worth |
|---|---|---|
| 7 days | 43 % | nothing |
| 14 days | 18 % | nothing |
| 21 days | 7.8 % | a hint |
| 30 days | 2.6 % | surprising |
| **42 days** | **0.6 %** | **believe it** |

**Set the bar at 42 days clean.** This retroactively voids most community
"fix" reports (48 hours, 14 hours, "a weekend") — all sit at p > 0.75, i.e.
no evidence at all. It is why the forums *appear* to contain fixes.

Corollary: detecting even a 2× improvement would need ~20 events (~8 months),
so **bundle changes rather than sequencing them**, and extract information
from *which drive / which slot / which mode* each incident hits rather than
from the absence of incidents.

---

## Mitigations in place

Declarative (see `config/tunables.yaml`, `config/storage.yaml`, and
CLAUDE.md § *NVMe 3.3V-rail mitigations*):

- `zfs_vdev_async_write_max_active=4`, `zfs_vdev_scrub_max_active=2`,
  `zfs_txg_timeout=1`
- NVMe **PS2 power cap** via udev rule `90-nvme-ps2-cap` (re-applies on every
  device add, including a PCI-rescan recovery — verified firing after a full
  power cycle)
- `autotrim=off`
- `nvme_core.default_ps_max_latency_us=0 pcie_aspm=off pcie_port_pm=off`

⚠ These are **probability reducers against an under-provisioned rail, not a
cure**, and none of them addresses Mode B at all.

### Known gap

`zfs_vdev_sync_write_max_active` is still at its default of 10, and
`zfs_vdev_max_active` at 1000. ZFS keeps five independent per-vdev queues, so
the async cap above does **not** constrain the sync write path.

---

## Recovery procedure

**A warm reboot is not always enough.** A reboot leaves the M.2 3.3 V rail
energised, so a controller latched into a fault state stays hung — observed
2026-08-01, where the PCIe link was fine (`08:00.0`, trained Gen3) but the
controller never initialised and `/dev/nvme5` never appeared.

Escalate in this order:

1. **PCI remove + rescan**
   ```sh
   echo 1 | sudo tee /sys/bus/pci/devices/0000:0X:00.0/remove
   echo 1 | sudo tee /sys/bus/pci/rescan
   sudo zpool online tank <partuuid>
   ```
2. **Full power-off** — `sudo shutdown -h now`, wait ~30 s for the rail to
   drain, power on. This is what actually cleared the 2026-08-01 hang.
3. **Physical reseat.**

Then `sudo zpool clear tank`. Note a hung controller can **stall boot for
minutes** (kernel NVMe probe retries) — a slow boot after a drop is expected,
not a second fault.

---

## Recommended next steps

1. **Retire `S6H2NF0WC37392`.** Mode B is a drive fault and the evidence is
   specific to this unit. Verify a candidate replacement with
   `nvme id-ctrl -H` and require its **lowest operational power state** to be
   ≤ ~1.5–2 W. (Note: buy on power-state table, **not** on "DRAM-less" —
   DRAM-less drives appear on both sides of the community's stability ledger.)
2. **Watch slot 04.** The Mode A fault is unaffected by that swap.
3. **Keep pulled drives as cold spares** — they are healthy on any host with
   competent 3.3 V regulation.
4. **Settle the rail question for free** — photograph and count the DFN power
   ICs near PL1/PL2 with the lid off. One regulator vs several is decisive and
   determines whether drive placement is a lever at all.

## What not to do

Confirmed dead ends, all with evidence in the community record: a third RMA;
a larger DC brick (12 V is clean); adding an LDO or bulk capacitance to the
3.3 V rail (tried — the pool degraded shortly after); M.2 aux-3.3 V injection
adapters (no such product exists); cooling as a *fix* (one owner reached
NVMe 40 °C with the case off and an external fan and still dropped within
5 minutes); forcing PCIe Gen down; USB-attached NVMe as a pool member;
waiting for firmware (the V40X BIOS branch is frozen at M1V403, March 2026).
