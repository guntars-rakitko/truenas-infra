# NVMe rail reference

> Moved verbatim from CLAUDE.md § NVMe 3.3V-rail mitigations on 2026-11-22, truenas-infra#233.

## NVMe 3.3V-rail mitigations

The ME Mini's shared 3.3V M.2 rail sags under peak **simultaneous** NVMe
current and drops a drive off the PCIe bus (`CSTS=0xffffffff`). Confirmed a
platform fault, not the drives (SMART-clean; recurs across drives/slots;
post-RMA "v2" unit still affected; an external 90 W PSU didn't help upstream).
**Total wattage is not the limit — peak current is.** The mitigations below are
declarative and reconciled by the tooling; they are **probability-reducers, not
a cure**. Full incident forensics live in the operator's session notes.

Applied as code (all reversible), by layer:

| Mitigation | Where (source of truth) | Effect |
|---|---|---|
| `zfs_vdev_async_write_max_active=4` (was 10) | `config/tunables.yaml` § tunables → `tunables.py::ensure_tunables` | fewer concurrent NAND-program ops per leaf vdev |
| `zfs_vdev_scrub_max_active=2` (was 3) | same | gentler scrub/resilver current |
| `zfs_txg_timeout=1` (was 5) | same | smaller, more frequent txg write spikes |
| **NVMe PS2 power-state cap** (udev rule `90-nvme-ps2-cap`) | same (type `UDEV`) | caps each drive at its lowest *operational* state (PM981a 8.00→3.50 W, PM9A1 8.49→3.18 W) ≈ **59% less worst-case rail current**. Re-applies on every device `add` (incl. a PCI-rescan recovery, no reboot). `-s` save is rejected by these OEM Samsungs, hence the udev RUN rule. |
| **`autotrim=off`** | `config/storage.yaml` § pool → `pool.py::ensure_autotrim` | removes TRIM/deallocate current bursts on delete (the differentiator on the 2026-07-22 drop). Reclaim space with periodic manual `zpool trim tank`. |
| `nvme_core.default_ps_max_latency_us=0 pcie_aspm=off pcie_port_pm=off` | `config/tunables.yaml` § kernel | pre-existing APST/ASPM disable (kept; reboot-only) |

Deploy: `./manage.sh phase tunables --apply` (ZFS params live immediately;
udev rule fires on next device add / reboot / `udevadm trigger`) and
`./manage.sh phase pool --apply` (autotrim). Verify a drive's cap with
`sudo nvme get-feature /dev/nvmeX -f 0x02` → `Current value:0x00000002`.

**Not a durable fix.** Community evidence: every software mitigation eventually
fails on a full 6-drive DRAM-cached array. There is **no NVMe/power BIOS fix**
(current 16 GB branch = M1V404).

> ⭐ **Read [`docs/nvme-dropout-forensics.md`](../nvme-dropout-forensics.md)
> before acting on any of the above.** Kernel-log forensics (2026-08-02) found
> **two distinct failure modes**, which is why single-cause theories kept
> failing to predict the next incident:
> - **Mode A** (4 events, slots 04 + 07) — instant death, *zero* warning,
>   `CSTS=0xffffffff`, **no AER**. Power-like.
> - **Mode B** (1 event) — I/O-timeout cascade ending in
>   `Device not ready; aborting reset, CSTS=0x1`, i.e. the controller is
>   **alive and answering**. A **drive firmware hang**, not a power event.
>   Specific to PM9A1 `S6H2NF0WC37392`, whose fault followed it across a
>   physical reslot → that drive is faulty on evidence.
>   ⚠ Since 2026-09-23 that drive (and slot 04's `S4GRNX0NA00357`) has left
>   the NAS: `…392` is **msa2-dev's Postgres disk by operator decision**
>   (kube-infra msa2 plan D2, which reads the evidence the other way — the
>   rail, not the drive). Do not "retire the faulty drive" without reading
>   the forensics doc's § 2026-09-23.
>
> **Fixing one mode will not fix the other.** That doc also carries the
> recovery ladder (⚠ a warm reboot is **not** enough — the M.2 3.3 V rail
> stays energised, so a latched controller stays hung; use a full power-off),
> the `nvme-drop-capture` cron and how to triage it, the `journalctl` traps
> (`-k` implies `-b`; `truenas_admin` **cannot** join `adm`/`systemd-journal`),
> and a **42-day** statistical evidence bar.
>
> ⚠ **Buy replacement drives on lowest *operational* power state ≤ ~1.5–2 W**
> (`nvme id-ctrl -H` → PS descriptor table) — **not** on "DRAM-less", a rule
> the community's own data contradicts (DRAM-less P310 and 990 EVO Plus both
> appear on the failing side). Our fleet is 6 DRAM-cached OEM Samsungs.

---
