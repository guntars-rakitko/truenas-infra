# UPS and NUT, history

> Moved verbatim from CLAUDE.md § UPS / NUT (canonical home for cluster-wide UPS state) (part) on 2026-11-22, truenas-infra#233.

**HISTORY — superseded:** charge.low 75→60 (2026-06-01), runtime.low 840→600
(#611) →420 (2026-08-01, #112), delay.shutdown 450→90 (2026-06-02). Current
values are in the table above.
**Thresholds (2026-05-31):** LB `override.battery.charge.low` 50→**75** and
`override.battery.runtime.low` 600→**840** (shutdown starts with ~14 min
reserve, after the drill left the battery at ~5%). `ups.delay.shutdown` was
raised 540→630 then **trimmed to 450** (7.5 min) once node shutdown was fixed
(see below) — it no longer needs slow-node margin. LB triggers (75% / 840s)
left aggressive for reserve. Trade-off: LB at 75% gives up short-outage
ride-through sooner (a ~4-5 min outage triggers shutdown; still rides routine
~90s blips).

**2026-05-31 (history) — slow Talos node shutdown (kube-infra #611).** Root
cause: Talos's shutdown sequence ran an API-dependent pod *drain*
(`CordonAndDrainNode`) that burned a hardcoded 5-min `DrainTimeout` once etcd
quorum was lost — the ~6-8 min "stuck loop" that drained the battery. An
interim node-side NUT `SHUTDOWNCMD=/sbin/poweroff --force` cut node shutdown to
~3.2 min. **Superseded 2026-06-02 by Path B** — the nodes are no longer NUT
clients (kube-infra `talos-os/patches/general.yaml` § NUT removal note); the NAS
orchestrator runs `talosctl shutdown --force` instead (banner at the top of this
§). charge.low was lowered 75→60 on 2026-06-01.
