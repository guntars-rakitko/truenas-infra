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

- On a single-disk failure: run `zpool status tank` to identify the failed
  member **by serial** (`nvmeN` names reshuffle across boots), swap the disk,
  then replace it via `pool.replace` (UI: Storage → tank → Manage Devices →
  Replace), choosing the new disk **by serial**. `phase pool` only CREATES the
  pool and is a no-op when `tank` exists, so it cannot do the replace. Record
  the new serial in `config/storage.yaml`, and note which drive failed — the
  vdev mixes three models, so that attribution is the only thing a failure
  teaches (CLAUDE.md § Storage Design).
- **Resilver is the vulnerable window** — no parity during the rebuild.
  Avoid stress on other drives until `zpool status` shows `scan: resilvered`.

## 4. Reinstall from scratch

If you need to rebuild the OS (boot-drive failure, bad upgrade, etc.). This
unit is the post-RMA "v2" and has **no eMMC**: the `boot-pool` lives on the
256 GB PM981 NVMe (CLAUDE.md § Hardware).

1. Boot the TrueNAS installer USB.
2. Install fresh onto the 256 GB PM981 **by serial** `S444NX0N496890` (the
   only ~238 GiB disk) — **never by `nvmeN`**: the 2026-09-23 rebuild moved
   boot from `nvme3n1` to `nvme2n1`, so a device-name pick can wipe a `tank`
   member. **Do NOT touch** the tank members `S649NF1R820750Y`,
   `S34DNX0JA02364`, `S4GSNF0N301379` — they host the `tank` pool and will be
   re-imported. ⚠ The tank holds every cluster backup bucket (MinIO) and the
   GIKS v1 MSSQL chain.
3. Post-install: in the new TrueNAS UI, `Storage → Import Pool → tank`.
   All data, datasets, and snapshots come back intact.
4. Re-run `bootstrap/01-bootstrap-notes.md` (API key is lost; mint a new one).
5. Re-run all phases in order; idempotency ensures they re-apply cleanly
   without destroying the re-imported pool.
6. **Re-arm the UPS shutdown path (Path B + the bug-#57 hook).**
   - `./manage.sh phase nut --apply` (step 5) restores `ups.config`,
     **including** `shutdowncmd=/mnt/tank/system/talos/nas-ups-orchestrator.sh`
     (from `config/services.yaml` § nut.shutdowncmd). That orchestrator is what
     actually shuts the cluster nodes down on an outage (`talosctl shutdown
     --force` per node, NAS last).
   - Then re-register the #57 Init/Shutdown hook, which arms the UPS
     kill-power. The registration lives in the config DB and is lost on
     reinstall; the installer is idempotent and does **not** touch
     `shutdowncmd`:
     ```bash
     # TRUENAS_HOST + TRUENAS_API_KEY come from manage.sh / Doppler infrastructure/ops
     export TRUENAS_NUT_ADMINPWD=$(doppler secrets get TRUENAS_NUT_ADMINPWD \
         --project infrastructure --config ops --plain)
     ~/github/truenas-infra/scripts/setup-ups-shutdown-hook.sh
     ```
   - Confirm `/mnt/tank/system/talos/{talosctl,dev-shutdown.talosconfig,prd-shutdown.talosconfig,nas-ups-orchestrator.sh}`
     survived the pool import — plus `msa2-{dev,prd}-shutdown.talosconfig` for
     each MS-A2 cluster whose `TALOS_NAS_SHUTDOWN_CONFIG_MSA2_*` key exists
     (list names only — never read the configs); if not, re-run
     `doppler run -p infrastructure -c ops -- ./scripts/setup-talos-shutdown-orchestrator.sh`.
     Then prove every credential still authenticates, in one ssh: run
     `./scripts/setup-talos-shutdown-orchestrator.sh --print-checks` and paste
     the command it prints (it lists every config/address pair the orchestrator
     targets and which failures are expected — `CLAUDE.md` § UPS / NUT, *MS-A2
     in the fan-out*).
   - Re-validate the chain with **Drill A** (see
     `wiki/docs/runbooks/ups-operations.md`).

   Without the orchestrator the nodes are **not** shut down on a real outage;
   without the hook the UPS keeps draining until the battery is flat.
