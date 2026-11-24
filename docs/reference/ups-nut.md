# UPS and NUT reference

> Moved verbatim from CLAUDE.md § UPS / NUT (canonical home for cluster-wide UPS state) on 2026-11-22, truenas-infra#233.

## UPS / NUT (canonical home for cluster-wide UPS state)

> ✅ **Path B is LIVE + VALIDATED (kube-infra #611, 2026-06-02).** On a UPS
> low-battery event the **NAS drives the whole shutdown** via `talosctl`, not
> the old NUT-secondary FSD path. What's live + codified in `config/services.yaml`:
> (1) `ups.config.shutdowncmd` = `nas-ups-orchestrator.sh`, which fires
> `talosctl shutdown --force --wait=false` at both MS-A2 boxes (msa2-prd
> `10.10.5.11`, msa2-dev `10.10.5.12`; least-privilege os:operator creds),
> polls the ones that accepted until they are down, then halts the NAS
> **last** (§ *The fan-out* below; until the Q170S1 teardown re-staged it on
> 2026-10-06, kube-infra#1443, it also shut down the six Q170S1 nodes); (2) the **nut-client extension is
> removed from the nodes** (Talos schematic `daef782b`) → they're no longer NUT
> secondaries; (3) `ups.delay.shutdown` = **90**; (4) **`sdtype = 5`** (apcsmart
> hard hibernate `@`) so the #57-hook kill-power cuts + power-cycles **even on
> mains** (the default `S`/`shutdown.return` no-ops on mains). Drill 2026-06-02
> (`upsmon -c fsd`): orchestrator instant-fired (0 secondaries, no HOSTSYNC
> wait), the six Q170S1 nodes down in 187s, NAS last, UPS cut + 60s
> power-cycle, both clusters 3/3 + 0 faulted.
> Plan/drill-log: `kube-infra/docs/superpowers/plans/2026-06-01-ups-shutdown-orchestrator.md`.
> The real on-battery AC-pull drill passed the same day ("Drill 2" in the wiki's
> `ups-operations` § Drill log: mains pulled, `charge.low: 60`, FSD, the
> orchestrated shutdown, cut at ~44 % battery, clean recovery); until 2026-10-06
> this note called it still owed.
> **Still owed:** a drill on the MS-A2 boxes (the 187 s above is the old
> estate's: re-measure, don't carry it), and with it the battery margin at their
> ~163 W load.
> NOTE: some prose below is retained as HISTORY of how we got here.

**Hardware:** 2× APC Smart-UPS SMT750I/SMT750IC on the rack. The
primary UPS (`apc1`) protects the NAS, both MS-A2 boxes (msa2-prd, msa2-dev:
their PSUs moved onto its battery outlets at the cutover, 2026-09-28), the
networking gear and the SMS gateways; the second UPS is reserved as a hot
spare. Measured 2026-09-28 19:06Z with both boxes on it: load ~163 W
(33.95 %), runtime **~26 min** at 100 % (steady range ~150–165 W, 24–27 min).
Before the move, with the six Q170S1 nodes already unplugged: ~72 W, 1.03 h.
The Q170S1 nodes left the rack for good at the teardown (no cold spare,
operator 2026-10-05; kube-infra#1443).
Battery replaced
**2026-06-13** (APC RBC7 pair + AP9620 swap, ~3-year expected lifespan;
next due ~2029).

**NUT topology:**

```
TrueNAS (this NAS, 10.10.5.10:3493)
  ├─ runs NUT MASTER (upsd + driver `apcsmart` → /dev/ttyUSB0, sdtype=5)
  │    apcsmart over the UPS DB-9 (LCC) via a USB→RS-232 adapter; gives
  │    the rich telemetry the dashboard needs (output.current, input
  │    min/max, frequency, temperature).
  ├─ upsmon (master mode) — on LB runs SHUTDOWNCMD = the Path-B orchestrator
  │    `/mnt/tank/system/talos/nas-ups-orchestrator.sh`:
  │      talosctl shutdown --force --wait=false × every node (one call PER
  │      NODE, each capped at 60 s) → poll apid until 0/N → halt NAS LAST
  │      (N = the addresses whose shutdown was ACCEPTED — rule (a))
  │  + #57 Init/Shutdown hook arms UPS kill-power (upsdrvctl shutdown → `@`)
  └─ upsd.users:
      upsadmin (SET + INSTCMD)  → operator scripts via NOPASSWD sudo
      upsmon   (`upsmon master` = LOGIN + MASTER + FSD)
                                 → the NAS's own upsmon ONLY. ⚠ NOT safe to
                                   drop (upsd will not start without monpwd),
                                   and NOT read-only: FSD = site shutdown.
                                   Never hand it to a remote client.
      (no user) anonymous LIST/GET → the in-cluster nut-exporter on every
                                   cluster (since 2026-09-28; before that it
                                   logged in as upsmon)

K8s nodes (the two MS-A2 boxes)
  └─ NOT NUT clients. The nut-client extension was stripped from the Talos
     image (schematic daef782b, 2026-06-02). They are shut down out-of-band
     by the NAS orchestrator via `talosctl shutdown --force` (os:operator
     creds staged at /mnt/tank/system/talos/msa2-{dev,prd}-shutdown.talosconfig).
```

⚠ **`--force` is load-bearing.** Talos's shutdown drain uses the Kubernetes
*eviction* API. Each MS-A2 cluster is ONE node, so there is nowhere to evict
to, and CNPG keeps its own `<cluster>-primary` PDB for `giks` and `w1` at
`instances: 1` (kube-infra keeps CNPG's PDBs on purpose: D4 fold-in M3,
`enablePDB: false` NOT adopted). Read 2026-10-06 on both clusters:
`giks-db/giks-primary` and `w1-db/w1-primary` at `minAvailable: 1`, 0
disruptions allowed; the only other PDB, `cert-manager/trust-manager`, is
`minAvailable: 0`. A single-node drain was measured never to finish on
msa2-dev (2026-09-24, `talosctl upgrade`: left cordoned, no reboot; kube-infra
plan Task C1 Step 5), so without `--force` each box would burn Talos's
hardcoded 5-min `DrainTimeout` on battery. *(On the Q170S1 estate the same
flag also beat etcd-quorum loss and the allowed=0 PDBs of Longhorn,
Prometheus & co., found 2026-07-29: git history.)* Verified on Talos v1.14:
`shutdown --force` exists, semantics unchanged. (Separately, and unrelated to
this path: `talosctl upgrade`'s `--preserve` flag was **deprecated — not
removed** — in v1.13; it still parses and exits 0 with a warning.)

**The fan-out (since the Q170S1 teardown, kube-infra#1443).** With PLP priced
out of the MS-A2 build, this is the only thing between a power cut and a hard
power-off of a single-instance Postgres on a non-PLP drive. The script's header
is the full reasoning; the rules below. ⚠ **Merged ≠ live:** the NAS runs the
copy staged under `/mnt/tank/system/talos/`, so a change here does nothing until
it is re-staged (below). This fan-out (#185) is **live since its re-stage on
2026-10-06**: in the operator's sudo check the staged orchestrator's sha256
matched the checkout's, and both boxes answered `Server: v1.14.1`. Re-staged
2026-10-10 after both boxes moved to Talos v1.14.2 (#197): the same orchestrator
sha256 (`63796603…`), `Client: Talos v1.14.2`, and both boxes answered
`Server: v1.14.2` to the staged `os:operator` configs (operator's sudo check).

| | msa2-prd | msa2-dev |
|---|---|---|
| address | `10.10.5.11` | `10.10.5.12` |
| config | `msa2-prd-shutdown.talosconfig` | `msa2-dev-shutdown.talosconfig` |
| call | `shutdown --force --wait=false`, capped at 60 s by coreutils `timeout` | the same |
| polled? | **only if the call returned 0** | the same |

- **The addresses are each box's FINAL one** (kube-infra `talos-os/estates.yaml`,
  plan D12). Their BUILD addresses (`.17`/`.18`) left the list at the teardown:
  an installed box runs its static FINAL address, and a box in maintenance mode
  (a DHCP lease) accepts no authenticated shutdown anyway.
- **The configs** come from `TALOS_NAS_SHUTDOWN_CONFIG_MSA2_{PRD,DEV}` (kube-infra
  `bootstrap.sh` menu 10), and the setup script requires both since the
  teardown. A config that is not staged makes the orchestrator skip that box and
  log `NOT shut down`.
- **Why only-if-accepted.** apid's `:50000` probe is unauthenticated: a box in
  Talos maintenance mode, or one whose PKI no longer matches the staged config,
  answers it for ever, and the poll would burn the whole 300 s backstop on
  battery. A shutdown that returned non-zero was not delivered to that box (no
  route / timeout / capped, rc 124 / x509 / maintenance mode), so nothing the
  script does would bring it down; waiting for it only drains the battery. ⚠
  That box is then **hard-cut** when apc1 cuts power: the log says `⚠ NOT shut
  down` for it (and `⚠ no node accepted its shutdown` when none did), and the
  printed check after each re-stage must show `Server:` for both boxes.
  `--wait=false` is what makes rc mean "delivered": rc=0 means talosctl's
  Version pre-check **and** the Shutdown RPC both succeeded, rc≠0 that the
  shutdown was not delivered. (In talosctl v1.14.0/v1.14.1 the `--wait=false`
  branch first runs `helpers.ClientVersionCheck`, a Version RPC, and returns
  before any Shutdown if it fails — so `os:operator` must keep Version access;
  the `--print-checks` `version` call proves that pre-flight for both boxes.)
  With the default `--wait`, a tracker error after a delivered shutdown would
  also be rc≠0.
- **The cap bounds a hung call; it does not hide it.** The poll, and so the NAS
  halt, starts only after every shutdown call has returned, so an address that
  connects and then hangs delays it by up to 65 s (60 s + the 5 s
  `--kill-after`). 60 s is deliberate: a cap that fires on a slow but delivered
  shutdown reads as rc 124, keeps that box out of the poll, and lets the NAS
  halt (and `apc1` cut power 90 s later) while it is still stopping Postgres.
  An absent box fails in ~3 s and a silently-dropping one in ~20 s on their own.
  A test pins the production default.
- **Both boxes are on `apc1`** since 2026-09-28 (*Hardware* above). On an outage
  they are shut down cleanly, and `apc1`'s kill-power cycle plus BIOS *AC power
  loss: Always On* boots them again. A box NOT on `apc1` but still powered (a
  drill on mains, a box moved to another supply) is shut down cleanly and
  **stays off** until powered on by hand, because its AC never drops: an
  availability cost, never a data one.
- **Re-stage** (main session; staging is non-disruptive — `shutdowncmd` is not
  touched). The script uploads whatever **this checkout** holds, so bring
  `main` up to date first — a stale checkout re-stages the old orchestrator
  and every credential check still passes:
  ```sh
  cd ~/github/truenas-infra && git switch main && git pull --ff-only && git log -1 --oneline
  doppler run -p infrastructure -c ops -- ./scripts/setup-talos-shutdown-orchestrator.sh
  ./scripts/setup-talos-shutdown-orchestrator.sh --print-checks   # just the check, any time
  ```
  Then paste the one `ssh -t …` command it prints. Its first line is the
  sha256 of the **staged** orchestrator, which must equal the value printed
  with it (this checkout's): that, not the credential checks, proves the NAS
  runs the merged script. Re-stage after every merge to the orchestrator, after
  every `bootstrap.sh msa2-<env>` menu 10 (it re-mints that box's config), and
  after every Talos upgrade (the staged `talosctl`, below).
- **History: the cutover and the teardown.** From 2026-09-26 to the teardown
  the orchestrator shut down both estates: the six Q170S1 nodes (`.11-.16`,
  `{dev,prd}-shutdown.talosconfig`, polled whatever their rc) and both MS-A2
  boxes at BOTH of their addresses (BUILD and FINAL, so no edit was due at
  either cutover or a rollback), with a rule (d) for `.11`/`.12`, which were in
  both lists. Both boxes moved onto `apc1` and it was re-staged at the cutover
  (2026-09-28). The teardown (kube-infra#1443) deleted the Q170S1 lists,
  configs, fire loops and rule (d), the BUILD addresses and the setup script's
  Q170S1 keys, as this section had listed (#185, merged and re-staged
  2026-10-06; the sudo check above passed). git history has the old rules and
  their tests.
  **Removed from the NAS by hand, 2026-10-06**: the setup script never deletes a
  file, so the operator removed the unused `{dev,prd}-shutdown.talosconfig` from
  `/mnt/tank/system/talos/` with sudo
  (`ssh -t truenas_admin@nas.w1.lv 'sudo rm /mnt/tank/system/talos/dev-shutdown.talosconfig /mnt/tank/system/talos/prd-shutdown.talosconfig'`).
  They were os:operator configs against the old clusters' CAs, which could shut
  nothing down. Their Doppler copies, `TALOS_NAS_SHUTDOWN_CONFIG_{DEV,PRD}`,
  unused since the re-stage, were deleted 2026-10-06 (kube-infra#1443 § 3).

⚠ **The staged `talosctl` does NOT track the node version — re-verify after
every Talos upgrade.** `/mnt/tank/system/talos/talosctl` is its own pinned binary
(the setup script's `TALOSCTL_VERSION`, default **v1.14.7**, both MS-A2 boxes'
version since 2026-11-24, kube-infra#2023; v1.14.6 from 2026-11-17, v1.14.4 from 2026-10-18, v1.14.2 from 2026-10-10, v1.14.1 before). Same-minor compatibility was **empirically confirmed** on 2026-07-29
(a v1.13.2 client plus the real `os:operator` credential authenticating to a
v1.13.7 node), but a major/minor gap would break the UPS shutdown path
**silently**, surfacing only during a real outage. The printed check's second
line is the staged binary's `version --client`.

> ✅ **The CREDENTIALS are long-lived on purpose** — the Q170S1 ones read from
> Doppler 2026-09-22 were valid `Jun 1 2026 → May 29 2036`, and menu 10 mints
> the msa2 ones with `--crt-ttl 87600h`. A suspicion that they had expired came
> from `setup-talos-shutdown-orchestrator.sh` documenting `--crt-ttl 720h`
> (30 days), which is NOT what was actually minted. ⚠ That comment was a
> landmine: anyone following it would have created a 30-day credential with
> **no rotation and no alert** (verified: nothing in this repo rotates it, no
> kube-infra `prometheus-rules-*` watches it). Corrected 2026-09-22 to 87600h,
> with the reasoning written down (the setup script's header).

One-line check after any node upgrade (the configs are 0600 root, so it needs
`sudo` — and a TTY, because `talosctl` is not on the NOPASSWD allowlist):

```sh
ssh -t truenas_admin@nas.w1.lv 'sudo /mnt/tank/system/talos/talosctl \
  --talosconfig /mnt/tank/system/talos/msa2-dev-shutdown.talosconfig \
  -n 10.10.5.12 --endpoints 10.10.5.12 version --short'   # expect Server: v1.14.x
```

To check **both** (config, address) pairs the orchestrator targets in one ssh
and one sudo prompt, run `./scripts/setup-talos-shutdown-orchestrator.sh
--print-checks` on the laptop (no credentials needed) and paste the command it
prints; it reads the pairs from the orchestrator's own inventory and says what
each failure means.

**Two NUT users — role separation:**

| User | Password key | Perms | Used by | Where defined |
|---|---|---|---|---|
| `upsmon` | `TRUENAS_NUT_MONPWD` (Doppler `ops` only) | `upsmon master` = LOGIN + MASTER + **FSD**. TrueNAS hardcodes it; this is NOT a read-only user | the local upsmon master ONLY | TrueNAS UI → Services → UPS → Edit → **Monitor User/Password** fields (managed via `ups.config` API; appears in upsd.users at service start) |
| *(none)* | none | anonymous `LIST`/`GET` (NUT needs no login for reads) | the in-cluster nut-exporter on every cluster, since 2026-09-28 | nothing to define. `rmonitor: true` makes upsd listen beyond loopback |
| `upsadmin` | `TRUENAS_NUT_ADMINPWD` | `actions = SET`, `instcmds = ALL` | Operator's `upsrw`/`upscmd` invocations | TrueNAS UI → Services → UPS → Edit → **Extra Users** field (`ups.config.extrausers`). Live since 2026-05-28. |

Operator never uses `upsmon` for writes. It exists only for the local
upsmon master; upsadmin handles writes. **Until 2026-09-28 the doctrine
called `upsmon` "read-only", and that was wrong.** Every cluster's
nut-exporter logged in with it, so any cluster could have sent `FSD apc1`
and powered off the site. Telemetry needs no user at all.

**UPS HID thresholds (live on UPS firmware, not in NUT config):**

These are stored INSIDE the UPS hardware. A UPS firmware reset or unit
swap silently reverts to APC defaults. Codified in
`config/services.yaml § nut.ups_thresholds` (and reconciled by
`modules/nut.py:ensure_ups_hid_thresholds`):

| Variable | Value | Unit | Why |
|---|---|---|---|
| `ups.delay.shutdown` | **90** | seconds | Time UPS waits after the #57-hook kill-power before killing outputs. Trimmed 450→90 on 2026-06-02 — the Path-B orchestrator confirms every polled node is down (poll 0/N) BEFORE the NAS halts+arms, so this only has to cover the NAS's own final poweroff. ENUM valid: 090/180/.../630/000; 090 is the floor. Writable (`upsrw`, zero-padded). |
| `ups.delay.start` | **60** (1 min) | seconds | Delay before UPS re-enables outputs after utility returns. apcsmart ENUM — write the zero-padded `"060"` (bare `60` → `ERR INVALID-VALUE`). Avoids the boot-shutdown-boot loop on flaky grids. |
| **`sdtype`** | **5** | enum | apcsmart kill-power METHOD: hard hibernate (`@`) ALWAYS, regardless of line state. In `ups.config.options`. The default (0) is status-dependent (`S` on battery / `@` on mains) and the #57 hook's transient `upsdrvctl` can't read status → used `S` → no-op on mains. `5` forces `@` → UPS cuts + auto-returns even on MAINS (validated 2026-06-02). The lever that closed the mains-mid-drain gap. |
| `battery.runtime.low` | **420** (7 min) | seconds | LB trigger (secondary). **READ-ONLY on firmware** — enforced via driver `override.battery.runtime.low = 420` in `ups.config.options`. Lowered 600→420 on 2026-08-01 (#112): the apcsmart runtime estimate jitters 480–1260 s on mains, so 600 fired false `UPSBatteryLow` alerts at full charge. |
| `battery.charge.low` | **60** | percent | LB trigger. **READ-ONLY on firmware** — enforced via driver `override.battery.charge.low = 60` + `ignorelb` in `ups.config.options`. Lowered 75→60 on 2026-06-01 — node shutdown is fast (~187s via the orchestrator) so 60% leaves ample reserve + restores short-outage ride-through. |
| `battery.charge.warning` | 50 | percent | WARN-level notification at 50% (logs only, no shutdown). Default — kept as early-warning signal. |
| TrueNAS `powerdown` | `true` | boolean | Set 2026-05-28. (Path B no longer relies on TrueNAS's own `powerdown` killpower — the #57 Init/Shutdown hook does the kill-power via `upsdrvctl shutdown`/sdtype=5. Left enabled, harmless.) |
| TrueNAS `shutdown` | `LOWBATT` | enum (BATT/LOWBATT) | Use LB as the shutdown trigger, not raw OB. Gives the cluster the full battery runtime envelope before initiating shutdown. |
| TrueNAS `shutdowntimer` | 30 (was 60) | seconds | Grace after LB fires before TrueNAS calls SHUTDOWNCMD on itself. Set 30 on 2026-05-30 to match the DR flow. NB: LOWBATT mode may shut down on the LB signal regardless of this timer — confirm in Drill A. |

**DR shutdown bug (#57) — UPS keeps draining after a real outage:**

Symptom (operator-confirmed 2026-05-30): on a power-loss DR, the cluster
+ NAS shut down on Low-Battery as designed, but the **UPS never cuts its
own outputs** — it keeps powering the dead load until the battery is flat,
then delivers a second uncontrolled outage when utility returns.

Root cause: the apcsmart driver reaches the UPS over `/dev/ttyUSB0`, a
**USB→RS-232 adapter**. During NAS poweroff the kernel removes the USB
device *before* TrueNAS's `powerdown` killpower step runs, so the
`shutdown.return` command is never delivered. (This is the long-standing
"USB teardown" failure mode — it applies to apcsmart-over-USB-serial just
as much as to usbhid-ups, because the transport is still USB.)

`ups.delay.shutdown` / `delay.start` are NOT the problem — the UPS never
gets the command that would start the countdown.

**Attempt 1 (FAILED, 2026-05-30):** arm `shutdown.return` from upsmon's
`SHUTDOWNCMD` via `ups.config.shutdowncmd`. Live drill on dev: nodes + NAS
shut down gracefully, but the **UPS was never armed and drained flat** —
exact bug #57. Retired this approach.

**Why it failed + the right mechanism (NUT issue #2587, systemd `nutshutdown`
hook):** kill-power must run as a *shutdown-time* hook once the NUT driver has
released the USB device — not at FSD/SHUTDOWNCMD time. `upsdrvctl shutdown`
fails "Can't claim USB device" while the driver still holds the port; and
TrueNAS's read-only `/usr/lib/systemd/system-shutdown/` blocks the standard
`nutshutdown` hook.

**Attempt 2 (current — VALIDATED 2026-05-31):** a TrueNAS **Init/Shutdown
Script** (when=SHUTDOWN) that ARMS the UPS during poweroff; `shutdowncmd` stays
cleared so TrueNAS keeps owning the host poweroff. A `upsmon -c fsd` drill
confirmed the UPS cuts power + power-cycles the rack.
- `scripts/nas-ups-shutdown.sh` — arm-only hook: `upscmd shutdown.return`
  (works while upsd is up — driver relays it), fallback `upsdrvctl stop` +
  `upsdrvctl shutdown` (driver released → can claim USB). Logs to
  `/mnt/tank/system/nut/last-shutdown.log` (world-readable — diagnosable
  without journal access, which truenas_admin lacks). Does NOT poweroff.
  **POWERDOWNFLAG guard:** `when=SHUTDOWN` scripts run on EVERY shutdown+reboot,
  so the hook first runs `upsmon -K` and only arms the UPS when the flag is set
  (genuine FSD/low-battery shutdown). A routine reboot/poweroff → flag unset →
  it logs and exits, so it does NOT power-cycle the rack on normal maintenance.
- `scripts/setup-ups-shutdown-hook.sh` — installer (API-based: uploads hook +
  root-only pw file, registers the SHUTDOWN init/shutdown script). ⚠ Since
  Path B it does **NOT** touch `shutdowncmd` (script header) — the Attempt-2
  version cleared it, which would now break the orchestrator. Init/shutdown
  scripts persist in the config DB across reboots + updates, but NOT across a
  TrueNAS reinstall (`docs/recovery.md` § 4).
- `config/services.yaml § nut.shutdowncmd` — ~~**keep empty**~~
  **⚠ SUPERSEDED by Path B (kube-infra #611, 2026-06-02 — see the banner
  at the top of this §).** `shutdowncmd` is now **SET** to
  `/mnt/tank/system/talos/nas-ups-orchestrator.sh` (verify:
  `config/services.yaml` § nut.shutdowncmd). **Do NOT clear it** — an empty
  `shutdowncmd` would break the Path-B DR orchestration. The Attempt-1/
  Attempt-2 prose above is retained only as history of the pre-Path-B
  design (when the Init/Shutdown hook armed the UPS and `shutdowncmd`
  was deliberately empty); the Init/Shutdown hook still arms the UPS
  kill-power, but it now runs *alongside* the orchestrator, not instead
  of a `shutdowncmd`.

Re-validate after changes with the LIGHT drill: `sudo upsmon -c fsd` (sets the
flag → arms) → read `last-shutdown.log`. A plain `reboot` must NOT arm it
(flag unset) — that's the guard's job.

**Severity reality check:** data is NEVER at risk — the graceful shutdown
chain works. Bug #57 only means the battery fully drains + no clean
auto-recovery if mains returns mid-drain. So "accept + document" is a valid
fallback. Other options if init/shutdown also fails: native RS-232 (no USB —
needs hardware the Beelink lacks), or revert apcsmart→usbhid-ups (trades the
rich Grafana telemetry for the standard kill-power path).

**Service-restart gotcha:** `midclt call service.control RESTART ups` can
leave the service **STOPPED** on TrueNAS 25.10 (observed 2026-05-30 after a
`shutdowntimer` update) — silently losing UPS monitoring. Use STOP + START +
verify instead (`service.query … state == RUNNING`, then `upsc apc1@localhost
ups.status` should answer `OL`, not "stale"). The battery-swap runbook and
`scripts/setup-ups-shutdown-hook.sh` both follow this pattern.

**UPS alert notifications (kube-infra #611, 2026-05-31).** TrueNAS UPS event
emails run through its alert framework (NOT NUT `NOTIFYFLAG`): each event is an
alert class with a level + policy, and the **E-Mail alert service emits at
WARNING and above**. Tuned classes (declared in `config/services.yaml §
nut.alertclasses`, applied via `midclt call alertclasses.update` — NOT
auto-reconciled, re-apply after a NAS rebuild):

| Class | Level | Policy | Why |
|---|---|---|---|
| `UPSOnBattery` | CRITICAL (default) | IMMEDIATELY | power lost → emails. Kept. |
| `UPSOnline` | INFO→**WARNING** | IMMEDIATELY | power restored. Was INFO (silent — below the email threshold) → bumped so "power back" notifies. |
| `UPSBatteryLow` | ALERT | IMMEDIATELY→**HOURLY** | flapped per threshold-crossing during post-outage recharge (email spam) → throttled to ≤1/hr; still signals a genuine low battery. |

Other UPS* classes (Commbad/Commok/Replbatt) kept at defaults. The recharge
flapping was a side-effect of the earlier 75%/840s LB triggers — the battery
dwelt in the LB zone for a while while recharging — and HOURLY caps the email
spam. The triggers have since been lowered (charge.low 60, runtime.low 420 —
see the table above).

**Battery health verification:**
- Last manual quick test: **2026-06-13 (Done and passed)** — establishes baseline for the new RBC7 pair.
- Automatic test interval: APC firmware default (14 days). Attempted to override to monthly via `ups.test.interval` 2026-05-28 — firmware rejected, kept default.
- Manual test command (zero-risk, ~30s):
  ```sh
  export ADMIN_PWD=$(doppler secrets get TRUENAS_NUT_ADMINPWD --project infrastructure --config ops --plain)
  ssh truenas_admin@10.10.5.10 "sudo upscmd -u upsadmin -p '$ADMIN_PWD' apc1@localhost test.battery.start.quick"
  sleep 35
  ssh truenas_admin@10.10.5.10 "upsc apc1@localhost ups.test.result"
  # Expect: "Done and passed"
  ```

**Full operations runbook:** [`wiki/docs/runbooks/ups-operations.md`](https://wiki.w1.lv/runbooks/ups-operations/) — covers all UPS reads/writes, battery replacement procedure, drill A (`shutdown.return`) and drill B (real utility-loss simulation).

---
