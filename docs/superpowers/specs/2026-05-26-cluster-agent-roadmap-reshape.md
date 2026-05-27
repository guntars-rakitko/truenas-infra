# cluster-agent — roadmap reshape (2026-05-26)

> **STATUS as of 2026-05-27 wrap:** All build work paused. P3 +
> P3.5 are live in production (daily digest + log mining + summary
> issue/email + per-cluster sender + `kub-*` labels). P4 (Mode G —
> backup verification) was brainstormed in a follow-up session but
> NOT spec'd — operator chose to let the live system soak before
> opening a new build. Reserved infrastructure (MinIO/B2 Doppler
> keys, `cluster-agent-tests` ns, mc tool stubs, schema mode slots)
> stays in place. See `wiki/docs/cluster-agent/phase-history.md`
> "P3.5 — Summary delivery + auth fix" and "P4+ — paused" sections
> for the current state. Re-open this doc when ready to plan the
> next mode.

Strategic audit + replan after the P1 soak surfaced that the agent's
original 9-mode design was partially shaped by industry conventions
that don't fit a **solo-operator homelab**. This memo captures what
changed in direction, why, and what's left to build.

Read this AFTER `2026-05-23-cluster-agent-design.md` (the original
spec, still source of truth for the architecture / RBAC / etc.) —
this is an addendum that updates priorities + scope, not a rewrite.

---

## TL;DR

- The original spec's TIME-BASED modes (weekly/monthly) were
  always right for a solo operator. We don't change them.
- Mode A pivoted from 5-min polling → **daily digest at 06:00 EEST**
  after observing that real-time alert mirroring duplicates
  Alertmanager and burns LLM cost without adding synthesis.
- 3 modes we'll explicitly **skip / postpone** because they're
  team-shaped (J auto-merge, F auto-PR, E runbook executor).
- 1 immediate gap to close: **log-pattern mining** (Mode A doesn't
  examine logs that didn't trigger an alert — addressed in P3).

---

## What's live (2026-05-26)

| Phase | Built | Verified | Notes |
|---|---|---|---|
| **P0** Foundation | ✅ | ✅ | RBAC, kubeconfigs, CNPs, sandbox repo, agent container |
| **P1** Mode A (5-min triage) | ✅ | ✅ | Replaced by P2 same day after operator feedback |
| **P2** Mode A daily digest | ✅ | ⏳ pending first scheduled fire (2026-05-27 06:00 EEST) | Pivoted today |

---

## What changed in direction (2026-05-26)

### Mode A — from "real-time alert mirror" to "daily synthesis"

Operator framing (verbatim, conversation 2026-05-26 afternoon):

> "I am more about developing an agent that could help my everyday
> life... I just need the assistant when I'm not behind the computer."

**Implications:**
- Alertmanager already routes alerts to operator email — agent
  doesn't add value by re-emitting them faster
- Agent's value is **synthesis + filtering + action proposal**, not
  speed
- Daily cadence aligns with human cognitive bandwidth (you'd skim a
  morning digest, not 288 per-cluster reports)
- Account-level Anthropic rate limits become a non-issue at 2 calls/day

This direction matches industry practice for AI-augmented SRE tools:
Datadog Watchdog / Bits AI are periodic-analysis, not real-time;
incident.io triggers on incident declaration (human escalation), not
on every alert; PagerDuty AIOps clusters related alerts rather than
trying to be faster than humans.

### Cadence philosophy for the rest of the modes

| Frequency band | Use for | Example modes |
|---|---|---|
| Daily | Synthesis you want with morning coffee | A (alert digest) |
| Weekly | Things that change at week scale | B (proactive scan), G (backup verify) |
| Monthly | Slow-changing reference | H (doctrine drift), E (right-sizing) |
| Event-driven | Nothing — see "what we skip" | I/J Renovate flows |

The original spec already used these bands for B/G/H. The reshape is
moving Mode A from "real-time" into the daily band and dropping the
event-driven band entirely.

---

## Mode-by-mode verdict

### ✅ Mode A — Alert triage (daily digest)
- **Status**: P2 live
- **Cadence**: Daily 06:00 / 06:01 EEST (configurable)
- **Verdict**: Keep. Extend in P3 with log-pattern mining (see
  P3 spec).
- **What it does**: 24h alert history → aggregate → chronicity
  classify → context for chronic only → LLM → 0-N actionable
  findings → GH issues + Grafana annotations.

### 🎯 Mode B — Proactive cluster scan + weekly digest
- **Status**: not built
- **Cadence (original)**: Weekly Mon 09:00 EEST
- **Verdict**: **Build (P5)** — natural home for "leftover" digestible
  signals not covered by Mode A:
  - Certificate expiry warnings (`14 days until X.w1.lv expires`)
  - K8s SA token rotation deadlines
  - License expiry (MinIO AIStor, etc.)
  - Dependency staleness summary (replaces Mode I)
  - Helm chart upstream-vs-current drift
- **Output**: wiki page (`wiki/docs/reports/weekly-YYYY-WW.md`) +
  short email digest
- **Cost**: 1 LLM call/week. Trivial.

### 🎯 Mode D — Change correlation (embedded in A)
- **Status**: partially built (alert context includes some Flux state)
- **Verdict**: **Extend Mode A in P3+** — add "git commits to
  kube-infra touching this namespace in the last 24h" as a context
  block. Helps the LLM say "alert started 30min after PR #XXX merged."
- Already in spec § 4.3 as embedded mode; just needs wiring.

### ⏸ Mode E — Runbook executor (embedded in A)
- **Status**: not built
- **Verdict**: **Skip until runbook catalog exists**. The mode
  assumes a structured catalog of `alert → runbook → step` mappings.
  We have ~3 wiki runbooks and they're not in that structured shape.
  Build the catalog first, mode second. Postpone to P8+.

### ⏸ Mode F — Auto-PR for trivial fixes
- **Status**: not built
- **Verdict**: **Postpone to P6+**. The fix-classification
  ("is this fix trivial?") is high-blast-radius judgment. In a solo
  context, the operator already reviews every PR — agent draft-PRs
  add review cost without much save. Worth revisiting after Mode B
  is live and the digest has shown consistent quality for 1+ month.

### 🎯 Mode G — Backup verification (test-restore)
- **Status**: not built (but P0 created the namespace + RBAC)
- **Cadence**: Weekly Sun 03:00 EEST
- **Verdict**: **Build (P4)** — **highest "sleep better at night"
  value for a solo op**. No team to catch a broken backup before
  you need it. Concrete steps:
  - Pull latest MSSQL `.bak` from MinIO `mssql-backups` bucket
  - Restore into `cluster-agent-tests` namespace
  - Run `SELECT COUNT(*) FROM <known_table>` smoke query
  - Tear down namespace
  - Emit Finding if FAIL (severity=high); silent if PASS

### ⏸ Mode H — Doctrine compliance scan (CLAUDE.md → live state)
- **Status**: not built
- **Cadence**: Monthly 1st 09:00 EEST
- **Verdict**: **Build (P7)** — low priority. Catches "I changed the
  live config but didn't update the CLAUDE.md doc" drift.
  Useful but not urgent — drift is recoverable, not destructive.

### 🔄 Mode I — Renovate PR triage
- **Status**: not built
- **Cadence (original)**: every 2h business hours
- **Verdict**: **Reshape** — fold into Mode B's weekly digest as a
  "12 Renovate PRs open, here are the 3 worth your attention this
  week" section. The original 2h cadence is too noisy for solo op.

### ❌ Mode J — Auto-merge low-risk Renovate
- **Status**: not built
- **Verdict**: **Skip entirely** in solo context. Auto-merge while
  the only operator sleeps = potential silent breakage with nobody
  to catch it before morning. The original spec's 5-layer policy
  was thoughtful but the value proposition is team-shaped. Operator
  reads the digest and merges manually = same effort, much safer.

---

## Reshaped roadmap

### P3 — Log-pattern mining (Mode A extension)
- **Goal**: surface notable log patterns (errors/warnings) that
  didn't trigger an alert
- **Effort**: ~1-2 days
- **Spec**: `specs/2026-05-26-cluster-agent-p3-log-mining.md`
- **Why first**: closes the explicit gap the operator flagged
  ("we check only alerts?"). Smallest change with most operator-
  visible impact.

### P4 — Mode G (backup verification, weekly Sunday)
- **Goal**: weekly test-restore drill
- **Effort**: ~2-3 days
- **Why next**: highest single-operator-safety win
- **Pre-reqs**: P0's `cluster-agent-test-restore` namespace already
  exists; just needs the runner + restore SQL + smoke query

### P5 — Mode B (weekly proactive scan + wiki digest)
- **Goal**: weekly digest covering certs, tokens, licenses,
  dependency staleness, infra drift
- **Effort**: ~2-3 days
- **Pre-reqs**: none

### P6+ — TBD after a month of P3/P4/P5 soak

Decisions deferred:
- Mode F (auto-PR for trivial fixes) — revisit if digest is reliable
- Mode H (doctrine scan) — low-priority; build when bandwidth permits
- Mode E (runbook executor) — needs runbook catalog first
- Anything new — see "extension ideas" below

---

## What we explicitly skip

| Mode | Why skipped |
|---|---|
| **J** (auto-merge) | Solo op shouldn't merge while asleep — no second pair of eyes |
| **F** (auto-PR) | Reviews itself; postponed indefinitely, not killed |
| **E** (runbook executor) | Needs runbook catalog first; postpone |
| Real-time critical-severity escalation | Alertmanager email already covers this |
| On-demand interactive ("call the agent") | Operator has Claude Code at the laptop |
| Multi-cluster / multi-tenant features | Single operator, single org |

---

## Extension ideas (not committed)

Possibilities to consider after P3/P4/P5 land, NOT promises:

- **Cost watchdog** — monthly summary of Anthropic spend vs budget,
  flag unusual cost spikes
- **Slack/Telegram digest delivery** — if email becomes annoying
- **GIKS-application-specific mode** — once GIKS is in production,
  app-layer anomaly detection (member-portal errors, payment failures)
- **Etcd health/snapshot verification** — verify snapshots in MinIO
  are restorable, not just present
- **Talos OS update advisor** — when a new Talos patch lands, the
  digest mentions it + risk assessment
- **Operator-mode flag** — when operator is on vacation, agent
  switches to lighter cadence + only high-severity dispatches

---

## Soak / verify the current state first

Before building P3, the P2 daily digest should fire successfully
for ~7 days. Things we'll learn from that soak:

- Whether the digest is finding REAL actionable items vs creating
  noise issues (operator triage signal)
- Whether the cost estimate (~$0.20-0.50/day) holds in practice
- Whether the prd cluster's chronic restore-test failures resolve
  themselves via the operator's normal ops, or persist
- Whether Anthropic account-level rate limits stay clear at the
  daily cadence

If after 7 days the digest is producing quality output, P3 proceeds.
If it's noisy or low-quality, prompt tuning takes priority over
adding more inputs (log mining).

---

## References

- Original design spec: `specs/2026-05-23-cluster-agent-design.md`
- P0 implementation plan: `plans/2026-05-23-cluster-agent-p0-foundation.md`
- P1 implementation plan: `plans/2026-05-25-cluster-agent-p1-mode-a-enable.md`
- P3 spec: `specs/2026-05-26-cluster-agent-p3-log-mining.md`
- Live runbook: `wiki/docs/runbooks/cluster-agent-runbook.md`
- Phase history: `wiki/docs/cluster-agent/phase-history.md`
