# Beelink ME Mini — RMA draft

**Context:** NVMe controllers on slots 1–3 drop off the PCIe bus under sustained
write load (initial ZFS pool creation). Root cause per community diagnosis:
undersized 3.3 V rail + ASM2824 PCIe switch link-training bug. Beelink has
acknowledged this and silently revised the board in units manufactured after
**8 September 2025**.

See [`docs/superpowers/specs/2026-04-20-beelink-nvme-defect.md`](#) if/when
written for the full incident record. This file is the RMA request draft only.

**First draft:** 2026-04-20

---

## Where to submit (in order of likely success)

1. Beelink BBS thread on the ME Mini 3.3 V issue (already on their radar):
   https://bbs.bee-link.com/d/8746-me-mini-having-failures-with-nvme-drives
2. Email `support@bee-link.com` with the text below.
3. Amazon return (if bought there and within return window) — fastest, skip Beelink.
4. Credit-card chargeback — last resort; cite "defective as delivered, manufacturer acknowledges defect."

---

## Fields to replace before sending

Search-and-replace these before submitting:

- `<FILL IN — see sticker on bottom of unit>` — serial number
- `<FILL IN — see date code on sticker or box>` — manufacturing date
- `<FILL IN>` — order / invoice number
- `<Amazon / bee-link.com / other>` — purchase source
- `<FILL IN>` — purchase date
- `<country>` — your country for shipping
- `<your name>` / `<email>` / `<phone if needed>` — contact info

How to find the manufacturing date:

- Bottom sticker on the unit — format typically `YYYY/MM/DD` or `YYWW` (week
  code, e.g. `2534` = 2025 week 34 = mid-August). Post-Sept-2025 = `2537`
  onwards, or `2025/09/08+`.
- Original box label.
- Serial prefix sometimes encodes month.

---

## Draft (copy-paste)

```
Subject: ME Mini — NVMe disconnect (3.3V rail defect, pre-Sept-2025 unit)

Hello Beelink support team,

I am reporting a known hardware defect on my Beelink ME Mini (Intel N150, 6x NVMe) and requesting a replacement unit.

UNIT DETAILS
- Model: Beelink ME Mini (6-bay NVMe NAS edition)
- Serial number: <FILL IN — see sticker on bottom of unit>
- Manufacturing date: <FILL IN — see date code on sticker or box>
- Order / invoice number: <FILL IN>
- Purchased from: <Amazon / bee-link.com / other>
- Purchase date: <FILL IN>

SYMPTOM
From a fresh install with six healthy NVMe drives, during the very first sustained write (initial ZFS pool creation), the NVMe controllers on slots 1-3 dropped off the PCIe bus:

  nvme nvme0: controller is down; will reset: CSTS=0xffffffff, PCI_STATUS=0xffff
  nvme nvme0: Does your device have a faulty power saving mode enabled?
  nvme 0000:04:00.0: Unable to change power state from D3cold to D0, device inaccessible
  nvme nvme0: Disabling device after reset failure: -19

The same pattern repeated on nvme1 and nvme2. Slots 4-6 had zero errors.

WHY I BELIEVE THIS IS THE KNOWN 3.3V RAIL / ASM2824 DEFECT
The failure is localised to the first three slots, under sustained-write load, with no errors on the remaining slots — exactly matching the pattern Beelink has acknowledged in these community threads:

- https://forums.truenas.com/t/using-beelink-me-mini-with-6-nvme-drives-only-4-are-useable-in-truenas-scale/47306 (80+ posts, canonical report)
- https://bbs.bee-link.com/d/8746-me-mini-having-failures-with-nvme-drives
- https://bbs.bee-link.com/d/6768-reboot-fails-to-detect-all-nvme-drives-on-me-mini--needs-full-shutdown
- https://forum.level1techs.com/t/beelink-me-mini-unstable-as-nas/240975

Other users with the same model report that units manufactured after 8 September 2025 no longer exhibit this issue, and that Beelink has provided replacement units in individual cases.

WHAT I HAVE ALREADY TRIED (TO RULE OUT SOFTWARE CAUSES)
- Applied the community-recommended kernel workaround via kernel_extra_options:
  nvme_core.default_ps_max_latency_us=0 pcie_aspm=off pcie_port_pm=off
- Verified all six drives are individually healthy (SMART OK, correct capacities, no prior pool membership).
- Running a recent, supported OS (TrueNAS Community Edition 25.10.3) with stock drivers.

REQUEST
I am requesting a replacement unit from a post-8-September-2025 production run (with the revised 3.3V rail / PCIe switch routing). Please advise on:

1. Whether my serial number is within the affected production range.
2. RMA process and shipping labels (I am based in <country>).
3. Estimated turnaround so I can plan around NAS downtime.

Happy to provide dmesg logs, zpool status, or any additional diagnostics you need.

Thank you — the Beelink ME Mini is otherwise a fantastic form factor and I want to make it work.

Best regards,
<your name>
<email>
<phone if needed>
```

---

## Evidence to attach if Beelink asks

1. `dmesg` excerpt showing the `controller is down … D3cold to D0` lines
2. `zpool status tank` showing per-disk error counts localised to slots 1–3
3. `nvme list` showing all 6 drives present with different models (pre-empts a "bad batch of drives" deflection)
4. Photo of the bottom sticker on the unit (serial, date code)
5. Photo of the original box label

---

## Status

- [ ] Manufacturing date confirmed (pre- or post-8-Sept-2025)
- [x] RMA submitted (2026-04-20, via `support@bee-link.com` email, sent without evidence — Beelink to request if needed)
- [ ] Beelink first response received
- [ ] Replacement unit shipped
- [ ] Replacement unit arrived + pool rebuilt cleanly
