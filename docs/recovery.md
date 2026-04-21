# Recovery — if management access is lost

The NAS is only reachable on `10.10.5.10` (VLAN 5 management). All automation
goes through that IP. If it becomes unreachable, you have two escape hatches:

## 1. 60-second auto-rollback (first defence)

Every `interface.commit()` starts a 60-second timer. If `interface.checkin()`
isn't called before the timer expires, TrueNAS automatically reverts the
pending network change.

So: if a `phase network --apply` breaks connectivity, do nothing for 60
seconds. The NAS reverts to the previous state and management returns.
Re-read the dry-run output before trying again.

## 2. Console / HDMI (second defence)

The Beelink ME Mini 2 has an HDMI port and USB for keyboard.

1. Plug a monitor + keyboard directly into the NAS.
2. Log in as `root` at the console.
3. Drop to a shell (option on the TrueNAS console menu).
4. Inspect and fix network config:

   ```bash
   midclt call interface.query | jq '.[] | {name, state, aliases}'
   midclt call interface.update <id> '{"ipv4_dhcp": true}'     # force DHCP back
   midclt call interface.commit
   midclt call interface.checkin
   ```

5. Alternatively, from the console menu use option "Reset Network
   Configuration" (TrueNAS offers this at the boot menu / console menu).

## 3. Disk pool recovery

The `tank` pool is RAIDZ1 — tolerates a single disk failure.

- On a single-disk failure: run `zpool status tank` to identify, swap disk,
  then trigger `pool.replace` (or re-run `phase storage` with the new serial).
- **Resilver is the vulnerable window** — no parity during the rebuild.
  Avoid stress on other drives until `zpool status` shows `scan: resilvered`.

## 4. Reinstall from scratch

If you need to rebuild the OS (eMMC failure, bad upgrade, etc.):

1. Boot the TrueNAS installer USB.
2. Apply the eMMC install quirk (see memory `emmc_install_quirk.md`):
   ```bash
   sed -i 's/tries=30/tries=200/' /usr/lib/python3/dist-packages/truenas_installer/install.py
   ```
3. Install fresh. **Do not touch the NVMe drives** — they host the `tank`
   pool and will be re-imported.
4. Post-install: in the new TrueNAS UI, `Storage → Import Pool → tank`.
   All data, datasets, and snapshots come back intact.
5. Re-run `bootstrap/01-bootstrap-notes.md` (API key is lost; mint a new one).
6. Re-run all phases in order; idempotency ensures they re-apply cleanly
   without destroying the re-imported pool.
