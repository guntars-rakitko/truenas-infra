# cluster-agent — design spec

| | |
|---|---|
| **Status** | Draft, awaiting operator review |
| **Author** | Operator + Claude (brainstorming session 2026-05-23) |
| **Repo** | `truenas-infra` (implementation), cross-cutting (kube-infra, wiki, etc.) |
| **Implementation plan** | TBD — created by `writing-plans` skill after spec approval |

---

## 1. Overview

`cluster-agent` is a scheduled LLM-driven SRE assistant running as a docker
container on the NAS. It complements (not replaces) Prometheus + Alertmanager
+ Loki + Watchdog DMS by adding LLM reasoning over alerts, logs, and state to
produce **actionable, deduplicated, well-correlated work items** — instead of
the raw alert stream the operator currently parses manually.

### Why this exists

Today the operator:
- Receives raw Alertmanager emails (often correlated to one underlying cause, but emitted separately)
- Manually `kubectl`s into clusters to diagnose
- Hand-writes GH issues for follow-up
- Reads every Renovate PR by hand (~70/month across 7 repos)
- Trusts that backups work because the CronJob is green (not because anything is verified end-to-end)
- Relies on CLAUDE.md doctrines staying true through manual discipline

The agent collapses this into:
- One coherent GH issue per incident with root-cause hypothesis + recommended action + correlated commits/logs/metrics
- One verdict per Renovate PR (auto-merge if policy passes, otherwise a triage comment)
- Weekly proactive cluster scan → wiki digest + email
- Monthly doctrine compliance audit → drift report
- Weekly real backup verification (B2/Velero/Litestream) → pass/fail report
- Self-policing cost ceiling, kill switches, full audit trail in Loki

### What this is NOT

- Not an SRE replacement — the operator still owns prd changes, hardware, network, BIOS
- Not auto-remediation — the agent reports + sometimes opens draft PRs, never executes mutations against live clusters (except narrow backup-verification reads in an isolated namespace)
- Not a Slackbot — Slack isn't in the stack; reporting goes to GH/wiki/email/Grafana
- Not a chat interface — the agent is scheduled + event-triggered, not conversational

---

## 2. Scope

### In scope (9 capabilities, "modes")

| Mode | Name | Cadence |
|---|---|---|
| A | Alert triage → GH issue | Every 5 min, no-op if no alerts firing |
| B | Proactive cluster scan → wiki digest + email | Mon 09:00 EEST (weekly) |
| D | Change correlation (embedded in A) | Every A run |
| E | Runbook executor (embedded in A) | Every A run when alert maps to a known runbook |
| F | Auto-PR for trivial fixes (embedded in A) | Whenever finding's fix is classified "trivial" |
| G | Backup verification (B2 / Velero / Litestream) | Sun 03:00 EEST (weekly) |
| H | Doctrine compliance scan (CLAUDE.md → live state) | 1st of month 09:00 EEST |
| I | Renovate PR triage | Every 2h during business hours |
| J | Auto-merge low-risk Renovate PRs (embedded in I) | Same run as I |

### Out of scope

- Auto-applied K8s mutations (no `kubectl apply`/`delete`/`patch` against live clusters)
- Direct prd commits (prd changes flow through `tools/promote-to-prd.sh` semver tag, same as today)
- Network / hardware / BIOS changes (`mikrotik-infra`, `bios-config` — agent reads but never PRs)
- Conversational interface (no chat, no Slack, no human-driven prompts at runtime)
- Multi-tenant — single operator, single org

---

## 3. Architecture

### 3.1 Where it lives

- Docker container on NAS, deployed via `truenas-infra/apps/cluster-agent/docker-compose.yaml`
- Python (matches `amtctl` / `stress-dashboard` stack)
- One container, internal scheduler (APScheduler) — not multi-container, not external cron
- Restart-on-failure; logs ship to Loki via existing NAS log shipper

### 3.2 What it talks to

| Target | Why | Access pattern |
|---|---|---|
| Both K8s clusters (`dev`, `prd`) | State, events, logs | Kubeconfig in Doppler — restricted `cluster-agent-readonly` ServiceAccount per cluster |
| Loki (`logs-{env}.w1.lv`) | Pull correlating logs | HTTP, basic auth from Doppler |
| Alertmanager (`alerts-{env}.w1.lv`) | List firing alerts | HTTP, basic auth from Doppler |
| Prometheus (`metrics-{env}.w1.lv`) | Query trends (PromQL) | HTTP, read-only |
| MinIO / AIStor (`s3-{env}.w1.lv`) | Backup verification (Mode G) | `mc` CLI, service-user creds in Doppler |
| Backblaze B2 EU (`b2-eu`) | Backup verification (Mode G) | `mc` CLI, scoped creds in Doppler |
| GitHub | Read PRs/commits, create issues, create draft PRs, auto-merge | GitHub App (`cluster-agent[bot]`) |
| Claude (via Agent SDK) | LLM | Authenticated via Max 5x OAuth credentials in Doppler (see § 3.6). $100/mo Max Agent SDK credit; agent self-alerts at $75. |
| SMTP (reused from Alertmanager) | Email notifications | Doppler creds shared with alertmanager |
| Grafana (both clusters) | Post annotations on dashboards | API token in Doppler |

### 3.3 Reporting surfaces

| Surface | Content | Why this surface |
|---|---|---|
| **GH issues** | Per-incident work items (A), Renovate triage comments (I), trivial-fix draft PRs (F) | These are work — need labels, lifecycle, "closed = fixed" |
| **Wiki** (`wiki/docs/reports/`) | Weekly digest (B), monthly doctrine scan (H), backup verification report (G) | Persistent, searchable, deployable; reports are reference material |
| **Email** | Notifications about the above + critical findings | Push-based; operator's lock screen |
| **Grafana** | Findings counters, cost/health metrics, dashboard annotations | Correlates findings with metric timelines |

#### Email budget (5 channels, deliberately low-volume)

| Email | Cadence | Content |
|---|---|---|
| Weekly digest | Mon 09:00 EEST | 1-line summary + link to wiki page |
| Backup verification | Sun ~04:00 EEST | PASS/FAIL + link to wiki report |
| Monthly doctrine scan | 1st of month ~10:00 | PASS/FAIL + drift count + link |
| Critical finding | Immediate | `severity:high` findings NOT covered by Alertmanager |
| Auto-merge digest | End-of-day on auto-merge days | List of merges + revert command |

Per-channel mute via `EMAIL_DIGEST_DISABLED=weekly,backup` (comma-separated in Doppler).

Recipient: `guntars@rakitko.lv`.

### 3.4 Grafana integration

Three integration points, all using existing infrastructure:

1. **Prometheus metrics** — agent exposes `/metrics` on port `9595`; both clusters' Prometheus scrape it via additional `scrape_config` (10 lines added to `kube-prometheus-stack` values per cluster).

2. **Grafana dashboard** "Cluster Agent" — ConfigMap in `flux-cd/infrastructure/configs/base/dashboards/cluster-agent.json`. Panels: health row (last-success per mode, error rate, cost burn), findings row (counts by severity/category), PR activity row, backup verification trend, doctrine drift trend.

3. **Dashboard annotations** — when agent creates a finding, it posts a Grafana annotation on the relevant dashboard via API. Findings show as vertical lines on existing dashboards (e.g. Longhorn, MSSQL).

4. **Loki for drill-down** — agent emits structured JSON logs (mode, finding-id, llm-rationale, evidence-refs); Grafana logs panel on the agent dashboard lets you click a finding → see the LLM rationale that produced it. **Trust foundation: every agent decision is auditable.**

#### Metrics emitted

| Metric | Type | Purpose |
|---|---|---|
| `cluster_agent_run_total{mode,status}` | counter | every mode run, success/fail |
| `cluster_agent_run_duration_seconds{mode}` | histogram | how long each mode takes |
| `cluster_agent_finding_total{mode,severity,category}` | counter | every finding |
| `cluster_agent_open_findings{severity}` | gauge | currently-open GH issues opened by agent |
| `cluster_agent_pr_action_total{action}` | counter | `comment` / `auto_merge` / `skip_for_review` |
| `cluster_agent_anthropic_tokens_total{kind}` | counter | input/output/cache-read tokens |
| `cluster_agent_anthropic_cost_usd_total` | counter | cumulative spend (live cost meter) |
| `cluster_agent_last_success_timestamp{mode}` | gauge | for "agent stuck" alert if last_success > 2× cadence ago |
| `cluster_agent_backup_verification_status{target}` | gauge | 1=pass, 0=fail per target |
| `cluster_agent_doctrine_drift_count{repo}` | gauge | doctrine violations found in last scan |

#### Self-cost-policing alert

Prometheus rule fires at `cluster_agent_anthropic_cost_usd_total > 75` per rolling 30d → agent disables itself + emails operator.

### 3.5 State

SQLite DB on bind-mounted volume (`./data/state.db`). Stores:
- Open findings (keyed on `dedup_key`)
- Last-run timestamps per mode
- PR triage history (for Mode I dedup)
- Cost-per-mode rolling counters
- Phase-history audit log

Backup: nightly `sqlite3 .backup` + `mc cp` to a new `cluster-agent`
bucket on `nas-prd` (created in P0 via `truenas-infra` setup scripts —
adds one entry to `setup-minio-buckets.sh` canonical list). SSE-S3
encrypted by default per the existing `setup-minio-encryption.sh`
posture. ILM rule: 30-day expiration (state DB is small + replayable
from Loki + GH).

### 3.6 Authentication — Max subscription via Agent SDK OAuth

Anthropic's policy changed (May 2026, effective **June 15, 2026** — Support
article 15036540). Pro/Max/Team/Enterprise subscriptions can now power the
Claude Agent SDK with a separate monthly Agent SDK credit ($100/mo on Max 5x)
that does NOT count against the subscription's interactive usage limits.
"Third-party apps that authenticate with your Claude subscription through
the Agent SDK" are explicitly in scope — this design fits that description.
This supersedes the earlier April 2026 third-party restriction.

**Conclusion:** authenticate via Max 5x subscription using Agent SDK OAuth.
API key remains a fallback option (Anthropic recommends it for shared
production-scale automation; not required for solo homelab use).

Configuration:
- Credentials live in dedicated Doppler project: `cluster-agent/prd.CLAUDE_OAUTH_CREDENTIALS`
- Surfaced into the container via the same Docker Compose `configs:`
  pattern used for the AIStor license — `_render_compose` substitutes
  the Doppler value at deploy time, container mounts at
  `/claude/.credentials.json`, agent points the SDK there at boot
- SDK handles token refresh automatically (in theory; see § 3.6.1 below)
- Anthropic Console "Usage credits" setting:
  - **Default (disabled)** = on exhausting $100/mo Agent SDK credit, requests
    return rate-limit errors until next billing cycle (safe, agent self-throttles)
  - **Optional (enabled)** = overflow bills at standard API rates against
    payment method, with operator-configurable monthly cap. Recommend keeping
    disabled — our $10-50/mo predicted spend is well within the $100 credit
- Aggressive prompt caching (system prompt + policy reused on every run) →
  realistic spend $10-15/mo, far below the $100 credit ceiling

#### 3.6.1 Open: how to actually obtain a containerizable OAuth credential

**Empirical finding (2026-05-25 during Task 22.5):** Claude Code v2.1.150
on macOS does **not** create `~/.claude/.credentials.json`. OAuth tokens
are stored as a ~1776-char encoded/encrypted blob in
`~/Library/Application Support/Claude/config.json` under the
`oauth:tokenCache` key. The `claude-agent-sdk` Python package's
`ClaudeAgentOptions` exposes **no auth-related parameters** — the SDK
delegates auth either to the Claude Code CLI's host-side session
(macOS Keychain / encrypted blob, not portable to Linux) OR to the
standard `ANTHROPIC_API_KEY` env var (pay-per-token API).

**There is currently no documented path** to export Max-OAuth tokens
into a Linux container in a refreshable form. Per Anthropic Support
Article 15036540, this is expected to change on **June 15, 2026** when
Agent SDK Max-subscription support goes "explicitly supported";
Anthropic should publish the in-container mechanism around that date.

**Decision (2026-05-25):** defer the Claude OAuth credential population
until ~June 15 when the supported mechanism ships. In the meantime:
- The Doppler key `cluster-agent/prd.CLAUDE_OAUTH_CREDENTIALS` is set
  to a placeholder (`__PLACEHOLDER_OPERATOR_FILL__`) so it's visible
  in Doppler.
- P0 has zero LLM calls (Tasks 1-24 are pure foundation: container,
  scheduler skeleton with no modes registered, observability, deploy).
  The placeholder never gets read by the SDK in P0.
- P1 (Mode A enable) is post-June-15 anyway per the schedule below.
- Fallback if June 15 doesn't ship a usable in-container OAuth flow:
  switch to `ANTHROPIC_API_KEY` (~$10-15/mo). One-line env var swap.

Operational note: OAuth credentials (when usable) are less robust than
a static API key. If a token is revoked the container cannot self-recover.
The agent self-monitors and emits a `severity:high` finding + emails
operator on SDK auth failure; recovery is a one-time re-auth + Doppler
key update.

**Pre-June-15 behavior** (if OAuth-in-container were available today):
Agent SDK calls would draw against the main Max interactive pool. Per
Article 15036540 — *"Starting June 15, 2026, Agent SDK usage no longer
counts toward your Claude plan's usage limits"* — implies the current
behavior is to count against those limits.

Practical rollout schedule:

| Period | LLM cadence |
|---|---|
| **May 23 – May 31 (P0)** | No LLM calls — pure foundation work |
| **June 1 – June 14 (P1 low-volume)** | Manual + hourly Mode A on dev, sandbox repo only. Validate prompts, fixtures, dedup. ~10-20 LLM calls/day total. |
| **June 15 onwards** | Full cadence — Mode A every 5 min, Mode I every 2h, etc. Credit pool isolates from interactive use. |

---

## 4. Agent loop + prompts + tools

### 4.1 Core loop pattern

Each mode is a Python coroutine using the Claude Agent SDK. Same skeleton,
different prompt + tool set:

```python
async def run_mode(mode: Mode, trigger_ctx: dict) -> ModeResult:
    cost_budget = MODE_BUDGETS[mode]              # e.g. $0.50 per A-run
    options = ClaudeAgentOptions(
        system_prompt=load_prompt(mode),           # versioned in /prompts
        allowed_tools=TOOL_SET[mode],              # explicit allowlist per mode
        max_turns=MODE_BUDGETS[mode].max_turns,
        model="claude-sonnet-4-5",                 # Opus for H only
        mcp_servers={"cluster": cluster_mcp},      # in-process MCP server
    )
    findings = []
    async for msg in query(prompt=build_context(mode, trigger_ctx), options=options):
        capture_metrics(msg)                       # tokens, cost, /metrics push
        if msg.is_finding():
            findings.append(parse_finding(msg))
        if cost_so_far() > cost_budget:
            abort("budget exceeded", emit_finding=True)
            break
    return persist_and_dispatch(findings)          # state.db → GH/wiki/email/Grafana
```

### 4.2 Prompts — versioned in git

```
truenas-infra/apps/cluster-agent/prompts/
├── _shared/
│   ├── policy.md            # rendered from policy.yaml (the Section 5 rules)
│   ├── output_schema.md     # the finding JSON schema (§ 4.4)
│   └── house_style.md       # tone: "homelab-pragmatic, no enterprise jargon"
├── alert_triage.md          # Mode A
├── proactive_scan.md        # Mode B
├── renovate_triage.md       # Mode I + J
├── backup_verification.md   # Mode G
└── doctrine_compliance.md   # Mode H
```

Prompts use Jinja `{% include %}` to embed `_shared/`. Bad prompt → revert commit → restart container. No live edits, no magic.

### 4.3 Tool surface (per-mode scoped)

Defense in depth: explicit allowlist + sandboxed Bash where needed + mandatory audit-log emit.

| Tool | A | B | D | E | F | G | H | I | J |
|---|---|---|---|---|---|---|---|---|---|
| `kubectl_get` (read-only, regex-scoped) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | |
| `kubectl_logs` | ✓ | | ✓ | ✓ | | | | | |
| `kubectl_describe` | ✓ | ✓ | | ✓ | | | | | |
| `loki_query` (LogQL, read-only) | ✓ | ✓ | ✓ | ✓ | | | ✓ | | |
| `prometheus_query` (PromQL, read-only) | ✓ | ✓ | | | | | ✓ | | |
| `flux_get` | ✓ | ✓ | ✓ | | | | | | |
| `git_log` / `git_diff` (read-only) | | | ✓ | | | | ✓ | ✓ | ✓ |
| `gh_pr_read` | | | | | ✓ | | | ✓ | ✓ |
| `gh_issue_create` | ✓ | ✓ | | | | ✓ | ✓ | | |
| `gh_issue_comment` | ✓ | ✓ | | | | | | ✓ | |
| `gh_pr_comment` | | | | | | | | ✓ | |
| `gh_pr_create_draft` | | | | | ✓ | | | | |
| `gh_pr_merge` | | | | | | | | | ✓ |
| `mc_*` (MinIO/B2) | | | | | | ✓ | | | |
| `mssql_query` (read-only on test pod) | | | | | | ✓ | | | |
| `web_fetch` (release notes, CHANGELOGs) | | ✓ | | | | | ✓ | ✓ | |
| `bash_sandboxed` (regex allowlist) | | | | ✓ | | ✓ | | | |

Every tool is an MCP tool function with type-safe params, schema validation,
**mandatory audit-log emit** (one JSON line to Loki per call: `{tool, params,
result_summary, agent_run_id, llm_rationale}`). Every action is queryable in
Loki forever.

`bash_sandboxed` is the escape hatch — used only by E (runbook executor) + G
(backup verification). Accepts only commands matching a per-mode regex
allowlist. Anything not matching → tool returns error, agent must choose a
different path.

### 4.4 Finding schema (the persistence boundary)

Everything Claude produces is normalized to this shape before leaving the agent:

```yaml
id: "01JK3R8Q9M..."        # ULID, sortable
created_at: "2026-05-23T17:42:00Z"
mode: "A"                  # A/B/D/E/F/G/H/I/J
cluster: "dev"             # dev/prd/nas/global
severity: "high"           # high/medium/low/info
title: "Pocket-ID pod CrashLoopBackOff after Longhorn restart"
summary: |
  Single-paragraph human-readable explanation.
evidence:                  # everything the agent looked at
  - type: alert
    ref: "Alertmanager/PodRestartingFrequently@2026-05-23T17:30:00Z"
  - type: log
    ref: "loki:{namespace='pocket-id'}|2026-05-23T17:25..17:42"
    excerpt: "..."
  - type: commit
    ref: "kube-infra@a546a84"
  - type: helmrelease
    ref: "longhorn@1.11.2"
root_cause_hypothesis: |
  Best-current-guess at root cause.
confidence: 0.75           # the agent's self-rated confidence
recommended_action: |
  What you should do, step by step.
runbook_ref: "wiki/docs/runbooks/longhorn-troubleshooting.md#stuck-attach"
auto_action:               # nullable — what the agent already did
  type: "draft_pr"
  ref: "kube-infra#525"
dedup_key: "alert:PodRestartingFrequently:pocket-id-0:dev"
```

**Dedup rule:** before creating a GH issue, agent looks up `dedup_key` in
`state.db`:
- Open issue with same key exists → comment update, don't open new
- Closed issue < 7d ago with same key → reopen with comment
- Otherwise → create new

Prevents issue spam.

### 4.5 Cost + token budgets

Costs metered at standard API list rates against the Max 5x Agent SDK
monthly credit ($100/mo).

| Mode | Cadence | Budget/run | Model | Monthly est. |
|---|---|---|---|---|
| A (alert triage) | 5min, only if alerts | $0.50 | Sonnet | $5 (~10 firings/mo) |
| B (proactive scan) | weekly | $2 | Sonnet | $8 |
| I (Renovate triage) | 2h business hours | $0.30/PR | Sonnet | $20 (~70 PRs/mo) |
| G (backup verification) | weekly | $1 | Sonnet | $4 |
| H (doctrine compliance) | monthly | $5 | Opus | $5 |
| **Total** | | | | **~$40-50/mo nominal, ~$10-15/mo with caching** |

Either projection sits comfortably under the $100/mo Max 5x Agent SDK
credit. Self-policing Prometheus alert fires at $75/mo (15% safety margin
before hitting the credit ceiling).

---

## 5. Auto-merge policy (Mode J — the high-blast mode)

This is the agent's most consequential capability. The policy below is the
**default starting point**; the operator can tighten/loosen any layer later.

### 5.1 Layer 1 — Repo-level

| Repo | Default policy | Reasoning |
|---|---|---|
| `wiki` | ✅ Auto-merge OK | Worst case = bad doc, easy revert |
| `hw-validation` | ✅ Auto-merge OK | Test harness, no prod impact |
| `truenas-infra` | 🟡 Patch-only + release-notes scan | App images run on NAS — recoverable but disruptive |
| `kube-infra` | 🟡 Patch-only + path allowlist (Layer 2) | Prod GitOps source of truth |
| GIKS | 🟡 Patch-only, green CI required | App code; `prd` env gated separately by `tools/promote-to-prd.sh` |
| `mikrotik-infra` | 🔴 Operator-only | Network = single chokepoint |
| `bios-config` | 🔴 Operator-only | Hardware-level; bad bump can brick a node |
| `talos-os/` paths anywhere | 🔴 Operator-only | Node OS = highest blast |

### 5.2 Layer 2 — kube-infra path-level

✅ **Auto-merge OK for patch bumps** to these chart components:
- `doppler-operator` (low blast, restartable)
- `reflector` (stateless)
- `trust-manager` (re-renders certs, restartable)
- `metrics-server` (stateless)
- `kyverno` patch (engine only, not policies)
- Renovate Docker GH Actions versions

🔴 **NEVER auto-merge** (regardless of semver):
- `cilium` (CNI — bad merge = cluster network down)
- `longhorn` (storage)
- `traefik` / `traefik-admin` / `traefik-tunnel` (ingress)
- `kube-prometheus-stack` (observability layer; complex migrations)
- `cert-manager` (cert plane)
- `pocket-id` (auth plane — locks operator out)
- `velero` (DR plane)
- MSSQL image in `flux-cd/apps/sql-servers/base/mssql-statefulset.yaml`
- Anything under `flux-cd/infrastructure/crds/`
- Anything under `talos-os/`

### 5.3 Layer 3 — Universal bans (regardless of repo/semver)

🔴 Agent NEVER touches these even on otherwise-green repos:
- RBAC (`Role` / `ClusterRole` / `*Binding`)
- PSA labels / Kyverno `ClusterPolicy`
- NetworkPolicy / CNP / CCNP
- Encryption-related (LUKS keys, KMS keys, Talos `systemDiskEncryption`)
- Doppler secret references (adding/removing keys)
- Ingress routing rules (`IngressRoute`, `Middleware`)
- Anything in a file with `# operator-only` magic comment

### 5.4 Layer 4 — Per-PR freshness/quality gates (ALL must pass)

For any PR otherwise eligible:
- CI green (all required checks)
- PR age ≥ **48h** (let other adopters be the canary)
- Renovate `merge confidence` ≥ `high` (when available)
- Release notes scan clean — no `BREAKING` / `MIGRATION` / `DEPRECATED` / `SECURITY` / `CVE` / `ATTENTION` keywords
- No `do-not-automerge` label on PR
- Not within 24h of a prior auto-merge in same repo (rate limit — 1/repo/day max)

### 5.5 Layer 5 — Time-of-day gate

Auto-merge only fires:
- **Mon–Thu, 09:00–17:00 EEST**
- Never Fri/Sat/Sun
- Never within 7 days before/after a calendar event tagged `vacation` (if calendar hook added later)

### 5.6 Bedrock — irreducible operator-only

Even if everything else passes:
- Major version bumps (`X.0.0`)
- Anything where agent's self-rated confidence < `high`
- Talos / kernel / node images
- Prd kubernetes objects (no direct `kubectl apply` to prd — only via the
  operator's `dev → main` PR promotion path, gated by the in-cluster
  `promotion-gates.yml` workflow — see
  https://wiki.w1.lv/runbooks/branching-and-promotion/)
- Backup/restore code paths

**GitOps model note (2026-05-24 migration):** kube-infra adopted a
two-branch `dev → main` model with `gh pr create --base main --head dev`
promotion gated by the `promotion-gates.yml` workflow. Auto-merge by
the cluster-agent acts on Renovate PRs targeting `dev` (the default
branch) only — promotion to prd remains operator-only. This gives us a
free soak window: every cluster-agent-merged Renovate PR reaches prd
only via the operator's subsequent `dev → main` PR.

### 5.7 Policy storage

The full policy lives as `truenas-infra/apps/cluster-agent/policy.yaml`,
rendered into the system prompt at run time. Policy changes are git PRs that
the operator reviews like any other change.

A rendered, human-readable copy lives at `wiki/docs/cluster-agent/policy.md`,
auto-synced from `policy.yaml` on every change.

---

## 6. Security model

### 6.1 GitHub identity — GitHub App, not PAT

GitHub App `cluster-agent` installed across the repos.

| Repo | Issues | PRs | Contents |
|---|---|---|---|
| `wiki` | RW | — | RW (digest commits to `docs/reports/`) |
| `hw-validation` | RW | RW | R |
| `kube-infra` | RW | RW (read for triage, write for draft PRs) | R |
| `truenas-infra` | RW | RW | R |
| `mikrotik-infra` | RW | R | R |
| `bios-config` | RW | R | R |
| GIKS | RW | RW (triage only, no merge per policy) | R |

App **never** has: admin, settings, org members, secrets, packages, workflows.

Auto-merge requires `PRs:Write` on target repo (policy gate is in our code, not GH).

### 6.2 K8s RBAC — read-only per cluster, plus narrow writer

**Per cluster, two ServiceAccounts:**

```yaml
# cluster-agent-readonly: bound to ClusterRole/cluster-agent-readonly
#   verbs: get/list/watch on most resources
#   resources: pods, pods/log, nodes, namespaces, events, services,
#              deployments, statefulsets, daemonsets, jobs, cronjobs,
#              configmaps, persistentvolumes, persistentvolumeclaims,
#              helmreleases.helm.toolkit.fluxcd.io,
#              kustomizations.kustomize.toolkit.fluxcd.io,
#              gitrepositories.source.toolkit.fluxcd.io,
#              volumes.longhorn.io, backups.velero.io,
#              certificates.cert-manager.io, ingressroutes.traefik.io,
#              servicemonitors.monitoring.coreos.com
#   NEVER: secrets, pods/exec, *.scale, anything ending in /eviction
```

```yaml
# cluster-agent-test-restore: bound to Role/cluster-agent-test-restore
#   namespace: cluster-agent-tests (dedicated, isolated)
#   verbs: create/delete on jobs, pods, configmaps in THIS namespace only
#   purpose: Mode G spins up throwaway MSSQL test-restore pod, then cleans up
```

Token rotation every 90d (calendar reminder + script).

### 6.3 Secret handling — Doppler-only

All in `infrastructure/ops.CLUSTER_AGENT_*`:

| Key | Used by |
|---|---|
| `CLAUDE_OAUTH_CREDENTIALS` | LLM calls via Max 5x Agent SDK (one-time `claude login` on NAS, credentials copied to Doppler) |
| `GH_APP_ID` / `GH_APP_PRIVATE_KEY` / `GH_APP_INSTALLATION_ID` | gh ops via App auth |
| `KUBECONFIG_DEV` / `KUBECONFIG_PRD` | restricted SA tokens (read-only role) |
| `KUBECONFIG_TEST_RESTORE_DEV` / `_PRD` | narrow-writer SA for Mode G |
| `LOKI_BASIC_AUTH_*`, `PROMETHEUS_BASIC_AUTH_*`, `ALERTMANAGER_BASIC_AUTH_*` | observability stack |
| `MINIO_NAS_KEY_*`, `B2_KEY_ID` / `B2_APP_KEY` | backup verification |
| `SMTP_USERNAME` / `SMTP_PASSWORD` | email — **reused** from `kube-prometheus-stack.alertmanager.*` |
| `GRAFANA_API_TOKEN_DEV` / `_PRD` | annotation API |

Doppler service token at container start; agent never writes secrets to disk,
never logs them (audit-log wrapper has a redaction pass — same shape as
existing `amtctl` redactor).

### 6.4 Kill switches (5 layers)

| Lever | Granularity | How |
|---|---|---|
| Master kill | All modes | `docker-compose stop cluster-agent` on NAS — instant |
| Doppler enable flag | All modes | `CLUSTER_AGENT_ENABLED=false` — agent reads on each schedule tick |
| Per-mode disable | One or more modes | `CLUSTER_AGENT_DISABLED_MODES=J,F` (comma-separated) |
| Per-repo auto-merge disable | Per repo | `CLUSTER_AGENT_AUTOMERGE_DISABLED_REPOS=kube-infra` |
| Circuit breaker (auto) | Per mode | 3 consecutive failures → that mode auto-disables, emits finding |
| Cost circuit breaker | All modes | Cost > $75/mo (= 75% of $100 Max Agent SDK credit) → agent disables itself + emails operator |

### 6.5 Auto-merge phased rollout (mandatory in spec)

| Phase | Duration | What J does |
|---|---|---|
| **A — Dry-run only** | 30 days minimum | Agent comments "WOULD auto-merge" but does NOT merge. Audit-only. |
| **B — `wiki` only** | 14 days minimum, clean dry-run history | Worst case: bad doc, easy revert |
| **C — `truenas-infra` + `hw-validation`** | 14 days minimum | Expand to other low-blast repos |
| **D — `kube-infra` Layer 2 allowlist** | Manual operator promotion | Per-chart sign-off |
| **Never** | — | `mikrotik-infra`, `bios-config`, `talos-os/`, Layer 3 universal bans |

Every auto-merged commit gets a trailer:
```
Auto-merged-by: cluster-agent[bot]
Policy-version: 2026.05.23
Confidence: 0.92
Release-notes-scan: clean
CI-status: green
PR-age: 67h
```

Helper script `kube-infra/tools/revert-auto-merge.sh DURATION` lists +
optionally reverts every commit with that trailer in a time window.
**One-command "undo the last hour of agent work."**

### 6.6 Container hardening

- Non-root (UID 10001), drop ALL capabilities, no `--privileged`, no host network
- `read_only: true` rootfs; writable tmpfs for `/tmp` + bind-mount for `./data` (state.db)
- Healthcheck on `/health` (returns last-success-per-mode + cost burn)
- Resource limits: `mem_limit: 1g`, `cpus: 1.0`
- Egress whitelist via docker network (only hosts from § 3.2)

### 6.7 Audit trail

| Surface | What lands there |
|---|---|
| Loki | Every tool call + params + result-summary + agent-run-id + LLM rationale |
| state.db | Every finding (full schema) — local query for ad-hoc analysis |
| GitHub audit log | Every issue/PR/comment under `cluster-agent[bot]` identity |
| Anthropic Console | Agent SDK usage summary (tokens, spend) at the Max subscription level — per-call detail recorded by the agent itself in Loki + Prometheus, not by Console |
| Prometheus | All counters from § 3.4 |
| Git | Every auto-merged commit stamped with trailer |

**Operating principle: every agent decision must be answerable via a single Loki query.**

---

## 7. Testing + rollout

### 7.1 Repository layout

```
truenas-infra/apps/cluster-agent/
├── docker-compose.yaml
├── Dockerfile
├── pyproject.toml
├── src/cluster_agent/
│   ├── modes/                 # one file per mode (alert_triage.py, etc.)
│   ├── tools/                 # MCP tool implementations
│   ├── policy/                # policy.yaml + classifier
│   ├── state/                 # SQLite ORM, dedup
│   ├── emit/                  # GH, wiki, email, Grafana, Prometheus
│   └── main.py                # entrypoint + APScheduler
├── prompts/                   # system prompts
├── tests/
│   ├── fixtures/              # anonymized real alerts, PRs, logs
│   ├── unit/                  # policy classifier, dedup, schema
│   ├── replay/                # whole-mode replay tests
│   └── integration/           # real Anthropic call against fake alert
└── tools/
    └── revert-auto-merge.sh
```

### 7.2 Tests

| Test type | What it covers | Run when |
|---|---|---|
| Unit (policy) | Policy classifier (highest-blast logic; 95%+ branch coverage required) | Every commit (CI) |
| Unit (dedup) | `dedup_key` generation, "already-open" lookup, reopen-within-7d | Every commit |
| Unit (schema) | Finding JSON conforms to schema; LLM output parses cleanly | Every commit |
| Replay (per-mode) | Anonymized real alerts/PRs in `tests/fixtures/` re-run after prompt changes; output diffed against pinned golden | Every prompt change |
| Integration (live API) | One real Anthropic call against synthetic alert; assert finding created, metrics moved, audit log emitted | Nightly + on release |
| Cost-burn simulation | Replay 7d of historical workload, measure spend vs estimate | Pre-release |
| Smoke (post-deploy) | Container starts, `/health` 200, `/metrics` reachable, no LLM call | Every container start |

### 7.3 Phased rollout

| Phase | Duration | What's enabled | Gating criteria to advance |
|---|---|---|---|
| **P0 Foundation** | ~1 week | Container live, /metrics + /health green, can read all sources, no LLM | Smoke green for 3d, dashboards rendering |
| **P1 Mode A on dev, sandbox repo** | 2-3 weeks | LLM enabled for A; GH writes go to `cluster-agent-sandbox` repo, dev only | Operator reviews ≥ 20 real findings, ≥ 80% rated useful, 0 secrets leaked |
| **P2 Mode A on dev+prd, real issues** | 2 weeks | A → real GH issues; B + D + E enabled | 0 false-positive criticals, dedup working |
| **P3 Mode I dry-run** | 2 weeks | Renovate triage comments; J in dry-run | Operator agrees with J verdict ≥ 95% over 30+ PRs |
| **P4 Mode J live, `wiki` only** | 2 weeks | Auto-merge enabled for `wiki` per § 6.5 Phase B | Zero bad auto-merges; revert script tested |
| **P5 Mode J live, `truenas-infra` + `hw-validation`** | 2 weeks | Per § 6.5 Phase C | Same as P4 |
| **P6 Mode J live, `kube-infra` Layer-2 allowlist** | Manual operator promotion | Chart-by-chart enablement | Per-chart sign-off |
| **P7 Modes G + H** | 1 week each | Backup verification + doctrine compliance | First successful weekly G + first monthly H |

**Skip-ahead rule:** any phase can be paused for any duration. Operator
decides when to advance. Minimums discourage rushed promotion; no maximum.

### 7.4 Operational readiness gates (objective, not vibes)

Before promoting any phase, **all** of:
- `cluster_agent_run_total{status="error"} / cluster_agent_run_total{status="*"}` over the phase < 5%
- No `severity:high` finding rolled back within 24h of creation (false-positive rate < 1%)
- Cost spend in the phase tracking within ± 30% of estimate
- Audit log has zero gaps: every tool call has a matching emit
- Operator approval recorded in `wiki/docs/cluster-agent/phase-history.md`

### 7.5 Documentation

Created during P0:
- `wiki/docs/runbooks/cluster-agent-runbook.md` — start/stop, disable a mode, disable per-repo automerge, revert auto-merges, interpret /metrics, where to look in Loki
- `wiki/docs/cluster-agent/policy.md` — the YAML policy from §§ 5 + 6, human-readable
- `wiki/docs/cluster-agent/prompts/` — snapshots of every system prompt, committed alongside any prompt change
- `wiki/docs/cluster-agent/phase-history.md` — log of which phase the agent is in, when it advanced, who approved

Created by agent at runtime:
- `wiki/docs/reports/YYYY-MM-DD-weekly.md` — Mode B output
- `wiki/docs/reports/YYYY-MM-DD-monthly-doctrine.md` — Mode H output
- `wiki/docs/reports/YYYY-MM-DD-backup-verification.md` — Mode G output

### 7.6 Estimated effort

| Block | Effort (solo, focused) |
|---|---|
| P0 foundation (container, /metrics, /health, MCP tools, Doppler) | ~3 days |
| Mode A + B + D + E (read-only modes) | ~5 days |
| Mode I + J classifier (policy is the bulk) | ~4 days |
| Mode G (backup verification — MinIO/B2/MSSQL) | ~3 days |
| Mode H (doctrine scan — CLAUDE.md parser + diff) | ~2 days |
| Mode F (auto-PR generation) | ~2 days |
| Grafana dashboard + scrape config | ~1 day |
| Tests (replay fixtures, policy unit tests) | ~3 days |
| Wiki runbook + docs | ~1 day |
| **Total** | **~24 days focused work** = ~3 months calendar at homelab pace |

---

## 8. Open questions / known unknowns

| Question | Resolution path |
|---|---|
| Will Agent SDK spend track our $10-15/mo estimate, or be closer to nominal $40-50/mo? | Cost-burn simulation in P0 with 7d of replay; iterate prompt caching aggressiveness. Either projection sits well within $100/mo Max 5x credit |
| Will OAuth credential refresh in a long-running container be stable? | Test through full P0 → P1 transition; if SDK auto-refresh hiccups, agent emits high-severity finding so we catch it fast |
| Is `metrics-server` truly safe to auto-merge, or should it be on the never-list? | Confirm during P3 dry-run by manually reviewing each `metrics-server` Renovate PR for 1 cycle |
| Should backup verification (Mode G) restore into a separate MSSQL instance on the NAS, or use the existing test-restore namespace? | Resolved here: dedicated `cluster-agent-tests` namespace in dev cluster only; never in prd |
| What's the right Grafana dashboard layout — single dashboard or per-mode dashboards? | Single dashboard; per-mode rows. Can split later if rows get crowded. |
| Calendar/vacation gate worth building? | Defer — manual `EMAIL_DIGEST_DISABLED` + `CLUSTER_AGENT_DISABLED_MODES=J` covers it. Revisit if vacation cadence justifies. |
| Cross-cluster correlation in Mode B (e.g., "dev had this issue 3 weeks ago — prd may follow") | YAGNI for now; revisit in P2 |

---

## 9. References

- `truenas-infra/CLAUDE.md` — NAS app conventions, Doppler key patterns
- `truenas-infra/apps/amtctl/` + `truenas-infra/apps/stress-dashboard/` — reference shape for new NAS apps
- `kube-infra/CLAUDE.md` — cluster doctrines (the input to Mode H)
- `wiki/docs/runbooks/` — existing operator runbooks (the input to Mode E)
- Anthropic Support Article 15036540 — "Use the Claude Agent SDK with your Claude plan" (the May 2026 policy that authorizes Max-via-OAuth for the Agent SDK; § 3.6 above)
- Claude Agent SDK docs — agent loop, MCP tools, options
- Existing kube-prometheus-stack ServiceMonitor + dashboard patterns
- Existing `tools/promote-to-prd.sh` — the prd promotion path the agent must NOT bypass

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **Mode** | One of the 9 capabilities (A/B/D/E/F/G/H/I/J); each has its own prompt, tool allowlist, schedule, and budget |
| **Finding** | Structured JSON output of an agent run; persists in state.db and dispatches to GH/wiki/email/Grafana |
| **Dedup key** | Per-finding identifier for "is this the same problem we already opened an issue for?" |
| **Policy** | The auto-merge rules in § 5; lives in `policy.yaml`, rendered into prompts |
| **Bedrock** | The irreducible operator-only set — agent NEVER touches these regardless of other gates |
| **Phase** | Rollout milestone (P0-P7); each has objective gates before advancing |
| **MCP tool** | Type-safe Python function exposed to the LLM via Model Context Protocol; emits mandatory audit log |
| **GitHub App** | `cluster-agent[bot]` identity for all GH operations — distinguishes agent activity from operator |
