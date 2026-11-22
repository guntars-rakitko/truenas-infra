# Hardware reference

> Moved verbatim from CLAUDE.md § Hardware on 2026-11-22, truenas-infra#233.

## Hardware

| Component | Detail |
|---|---|
| Device | Beelink ME Mini (post-RMA "v2" unit — 16 GB, no eMMC). ⚠ **The 3.3V-rail defect is NOT fixed on this unit.** Beelink's post-2025-09-08 inductor change was supposed to cure it; it did not — the NVMe drops recurred 2026-07-19 and 2026-07-22 (both the same drive `S4GRNX0NA00357` in slot 04, at ~03:00 UTC under the nightly write burst). Root cause is the shared 3.3V M.2 rail sagging under peak *simultaneous* NVMe current (community-corroborated; an external 90 W PSU + added regulator did not help upstream). Mitigations are software probability-reducers, NOT a cure — see § NVMe 3.3V-rail mitigations. Note: this unit has a **flat PCIe topology** (each NVMe on its own ADL-N PCH root port, no ASM2824 switch), so the ASM2824 link-training erratum does not apply here. |
| CPU | Intel N150 (4 E-cores, no HT) |
| RAM | 16 GB LPDDR5 (soldered; reads as 16.0 GB via `system.info` — earlier "12 GB" spec was wrong) |
| Storage | 6× M.2 NVMe slots, **4 populated since the 2026-09-23 rebuild** — 1× 256 GB PM981 boot (`nvme2n1`, S/N `S444NX0N496890`) + **3× 1 TB in RAIDZ1 tank**: `nvme0n1` Samsung 980 (`S649NF1R820750Y`), `nvme1n1` SM961 (`S34DNX0JA02364`), `nvme3n1` PM981a-H7 (`S4GSNF0N301379`). ⚠ **Enumeration changed in the rebuild** — boot was `nvme3n1`, is now `nvme2n1`, and the tank disks are NOT contiguous. Match by **serial**, never device name. ⚠ **Mixed capacity**: the 980 is 931.51 GiB vs 953.87 for the other two, and a raidz vdev sizes to its smallest member → ~45 GiB stranded, **1.75 TB usable**. Two slots now EMPTY, which is itself a rail mitigation (see below) |
| NIC1 | Intel **I226-V** 2.5G (`igc` driver, `enp1s0`, MAC `78:55:36:07:25:93`) — data, tagged trunk carrying VLANs 10 / 15 / 20 (sub-interfaces 10.10.10.10 / 10.10.15.10 / 10.10.20.10) |
| NIC2 | Intel **I226-V** 2.5G (`igc` driver, `enp2s0`, MAC `78:55:36:07:25:92`) — management, untagged VLAN 5 (10.10.5.10) |
| OS | TrueNAS Community Edition 25.10.7 (codename Goldeye) |

---
