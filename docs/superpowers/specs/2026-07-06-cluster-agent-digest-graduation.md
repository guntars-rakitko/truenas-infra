# cluster-agent graduation — route findings to kube-infra, digests to their own repo

**Status:** Draft — 2026-07-06
**Operator decision:** graduate Mode A out of soak by **routing by type** —
the daily *digest-summary* stays in a renamed permanent digest repo; **every
individual Finding (all severities) is filed in `kube-infra`**, the ops repo.
(Chosen 2026-07-06; supersedes both the "wholesale move everything into
kube-infra" and the "selective high-severity bridge" options — see § 3.)
**Amends:** [`2026-05-23-cluster-agent-design.md`](2026-05-23-cluster-agent-design.md)
§ 7.3 (phased rollout) — redefines the P2 "Mode A → real issues" step.
**Implementation home:** `apps/cluster-agent/` (code) + Doppler
`cluster-agent/{dev,prd}` (config) + the `cluster-agent[bot]` GitHub App
(installation + repo access). The operator applies the config/App changes;
the code changes land via the normal `apps/cluster-agent/` deploy flow.

---

## 1. Goal

Mode A (the daily alert+log digest) has run trusted on **both** clusters
since 2026-05-26 — well past the P1 soak gate in § 7.3 (≥ 20 findings
reviewed, ≥ 80 % useful, 0 secret leaks). It still writes exclusively to
**`cluster-agent-sandbox`**, a private, code-less GitHub repo whose name (and
its § 7.3 framing as a throwaway "sandbox destination during P1 soak") is now
a misnomer, with two consequences:

1. **The name lies.** It reads as disposable; it is in fact the permanent,
   sole system of record for everything the agent emits.
2. **Trusted, actionable findings are buried where nobody works.** The
   operator lives in `kube-infra`; a genuinely actionable finding sits in a
   repo that's never opened except deliberately.

The fix is a **clean routing split by issue type**:

- **Daily `digest-summary` issues → a renamed permanent repo,
  `cluster-agent-digest`.** The summary is the *full landscape* — every alert
  group the LLM saw, including chronic-but-known background noise. It's a
  snapshot stream, not a backlog; it auto-supersedes daily. It belongs in its
  own repo, out of the ops inbox.
- **Every individual Finding (all severities) → `kube-infra`.** A Finding is
  the LLM's *curated actionable output* — it only files one when something is
  worth an individual issue (a typical day: ~0–3 findings per cluster; prd is
  often 0). That short, already-filtered list is exactly what belongs in the
  ops repo, human-closed like any other work item.

This is the § 7.3 "P2 → real issues" graduation, made concrete: findings *do*
graduate to the real ops repo — it's just that the **summary firehose does
not**, and gets its own permanent home instead.

## 2. Non-goals

- **No severity/confidence threshold on findings.** All curated findings go
  to `kube-infra`; the LLM's decision-to-file *is* the filter (§ 3). (A
  disabled-by-default `FINDINGS_MIN_SEVERITY` floor exists only as future
  insurance — § 6.)
- **No duplication.** A finding gets **one** issue (in `kube-infra`); a
  summary gets **one** issue (in `cluster-agent-digest`). Nothing is written
  to both.
- **No new agent Modes.** Mode A only. Mode F (auto-PR), Mode J (auto-merge),
  Mode E (runbook executor) stay deferred per the
  [roadmap-reshape](2026-05-26-cluster-agent-roadmap-reshape.md) § 6.
- **No change to the digest LLM pipeline** (alert aggregation, log mining,
  dedup, cost gates, summary rendering) — this is about *destinations*, not
  detection.
- **No auto-close authority for the bot on `kube-infra`.** The bot creates +
  comments on findings; a human closes them (§ 5).

## 3. Decision: route by type; don't threshold-filter findings

Two alternatives were on the table and rejected:

**(a) Wholesale move *everything* into `kube-infra`** (findings *and*
summaries). Rejected: the daily summary is a high-volume, low-signal landscape
snapshot (mostly background noise) that auto-supersedes — pouring it into the
ops repo drowns the inbox and puts a churning snapshot where durable work
items live.

**(b) Selective bridge — only `severity==high` + high-confidence findings to
`kube-infra`, the rest stay in the digest repo.** Rejected because it
misjudges what a "finding" is:

- Findings are **already curated** — the LLM leaves background noise in the
  *summary* and only promotes something to a Finding when it's worth an issue.
  Re-filtering that curated list by severity would **bury** genuinely useful
  low/medium findings in a repo nobody opens (e.g. a `severity-low`
  etcd-fragmentation finding that's real and worth tracking).
- It adds a **threshold to tune** (noise if too loose, missed issues if too
  tight) — ongoing operational surface for no gain.
- It **duplicates** each bridged finding across two repos, forcing fragile
  "remember both issue numbers" cross-repo dedup state.

Routing by *type* avoids all three: the severity label rides along on every
finding, so the operator can filter in-repo (`label:cluster-agent
-label:severity-low`) if they ever want to — without anything being
pre-buried, without a threshold, without duplication.

| Issue type | Repo | Volume | Lifecycle | Why here |
|---|---|---|---|---|
| `digest-summary` | `cluster-agent-digest` (renamed) | 1/cluster/day | auto-supersedes yesterday's | high-volume landscape snapshot; a stream, not a backlog |
| Finding (all severities) | `kube-infra` | ~0–3/cluster/day | human-close (bot never closes) | LLM-curated actionable item; belongs where the operator works |

## 4. Finding routing

### 4.1 Where findings go

Every Finding the digest emits is filed in `kube-infra` (the value of the new
`FINDINGS_REPO` key, § 6) — no severity/confidence gate. The dispatch path is
unchanged except for the target repo: `dispatch.py` reads `FINDINGS_REPO`
(instead of `SANDBOX_REPO`) for the create/comment/reopen it already does.

- **Labels:** `cluster-agent` (provenance) + `needs-review` (§ 5.2) +
  `severity-{high|medium|low|info}` + `kub-{dev,prd}`. Create `cluster-agent`
  + `needs-review` in `kube-infra` first (they don't exist there yet); the
  `severity-*` / `kub-*` labels the bot already uses just need to exist in the
  new repo too.
- **Dedup is unchanged.** One issue per `dedup_key` in `kube-infra`;
  `lookup()` → CREATE / COMMENT / REOPEN exactly as today
  (`state/dedup.py`, `REOPEN_WINDOW = 7d`). No dual-issue bookkeeping — this
  is the same single-issue-per-finding flow, just pointed at `kube-infra`.

### 4.2 Summaries reference the findings cross-repo

The daily summary (in `cluster-agent-digest`) already renders a "rolled into"
column linking each escalated alert group to its per-Finding issue. Those
links now point at `kube-infra` issues (cross-repo links work fine). Verify
`summary_issue.py` builds the link with the finding's actual repo, not a
hard-coded same-repo assumption.

### 4.3 Prerequisites (safety rails — do these first)

- **Repo safelist:** add `guntars-rakitko/kube-infra` to the write-safelist
  in `tools/github.py` (currently `…/cluster-agent-sandbox` only, ~line 148).
  The bot refuses to write to any repo not on the safelist — the guardrail
  that stops a mis-config from spraying issues cluster-wide. Keep the digest
  repo on the safelist too (under its new name).
- **GitHub App install scope:** grant the `cluster-agent[bot]` App **Issues:
  read & write** on `kube-infra` — and nothing else (no contents, no PRs, no
  workflows). The App physically cannot do more than file/comment issues.

## 5. Lifecycle + close policy (the operator's original worry, resolved)

The original hesitation about graduating was: *"if the daily digest
auto-closes, won't I miss unhandled items?"* — resolved by keeping summaries
and findings as **separate objects with opposite lifecycles**:

### 5.1 Daily summary auto-supersedes (in the digest repo)

`_close_previous_summaries()` (`modes/summary_issue.py`) closes yesterday's
`digest-summary` when today's is filed ("superseded by today's summary"). Safe
to keep: the summary is a *snapshot of the day's landscape*, not a work item —
and every actionable thing it references is now its **own** `kube-infra` issue
that is **human-close-only**. Nothing actionable is lost when a summary is
superseded. The worry was a false coupling that doesn't exist.

### 5.2 Findings are human-close-only (in `kube-infra`)

- The bot creates / comments / reopens findings by `dedup_key` but **never
  auto-closes** one. A human closes a finding when the underlying problem is
  fixed. (The App scope in § 4.3 technically permits close; policy + code
  forbid it — belt and suspenders.)
- **`needs-review` is the triage queue.** Every finding gets it on creation.
  `label:needs-review` in `kube-infra` is the operator's "what has the agent
  surfaced that I haven't looked at" query. Remove it by hand once triaged;
  the issue stays open until the problem is actually resolved.
- **Recurrence visibility (§ 6 code change):** when a still-open finding is
  deduped, the bot posts a **"still firing as of `<date>`"** comment (today
  the COMMENT path is silent, so a live-but-known finding can't be told apart
  from one that went quiet).

## 6. Concrete changes

### Config (Doppler `cluster-agent/{dev,prd}`)

| Key | Change |
|---|---|
| `SANDBOX_REPO` | **Retire.** Split into the two below. Keep reading it as a fallback for `DIGEST_REPO` for one release so a half-applied config never 0-outs the digest, then drop it. |
| `DIGEST_REPO` | **New** — `guntars-rakitko/cluster-agent-digest`. Destination for `digest-summary` issues. |
| `FINDINGS_REPO` | **New** — `guntars-rakitko/kube-infra`. Destination for individual findings. Empty ⇒ fall back to `DIGEST_REPO` (lets you land code before flipping findings over). |
| `FINDINGS_MIN_SEVERITY` | **New, default empty (= all severities).** Future insurance only — set to `medium`/`high` if low/info findings ever prove noisy in the ops repo. Ships inert. |

### Code (`apps/cluster-agent/`)

- `dispatch.py` — route by object type: summaries → `DIGEST_REPO`, findings →
  `FINDINGS_REPO` (with the empty-`FINDINGS_REPO`⇒`DIGEST_REPO` fallback +
  the optional `FINDINGS_MIN_SEVERITY` floor). Same create/comment/reopen
  logic, parameterized on repo.
- `state/dedup.py` COMMENT path — post the **"still firing as of `<date>`"**
  recurrence comment on dedup.
- `tools/github.py` — add `kube-infra` to the write-safelist; keep the digest
  repo (renamed) on it.
- `modes/daily_digest.py:187` — repoint the default repo constant off the
  literal `cluster-agent-sandbox`.
- `modes/summary_issue.py` — build "rolled into" links with the finding's
  actual (`kube-infra`) repo (§ 4.2); summary lifecycle unchanged.

### Infra / GitHub

- **Rename the repo** `cluster-agent-sandbox` → `cluster-agent-digest`
  (Settings → Rename; GitHub auto-redirects the old path + existing issue
  links). Don't recreate a repo named `cluster-agent-sandbox` afterward (it
  would break the redirect).
- Create labels `cluster-agent` + `needs-review` (and ensure `severity-*` /
  `kub-*` exist) in `kube-infra`.
- Extend the `cluster-agent[bot]` App install to `kube-infra` (Issues R/W
  only).

### Docs (same commit set as the code)

- `truenas-infra/CLAUDE.md` § cluster-agent — new repo name, the routing
  split, the `DIGEST_REPO`/`FINDINGS_REPO`/`FINDINGS_MIN_SEVERITY` keys.
- `2026-05-23-cluster-agent-design.md` § 7.3 — note P2 graduated as
  "findings → kube-infra, summaries → cluster-agent-digest" (not a wholesale
  move, not a severity bridge).
- `wiki/docs/runbooks/cluster-agent-runbook.md` — a "reviewing findings"
  section (findings live in kube-infra under `label:needs-review`; summaries
  live in the digest repo; what the recurrence comment means).

## 7. Coordinated rollout (order matters — the daily digest must not break)

The rename, the Doppler keys, and the code defaults all name the same repos;
move them together or a 06:00 run writes nowhere.

1. **Land code first, findings still in the old repo.** Ship the
   `DIGEST_REPO`(+`SANDBOX_REPO`-fallback) / `FINDINGS_REPO`(empty⇒`DIGEST_REPO`)
   routing + the recurrence comment + the safelist entry. Deploy
   (`manage.sh phase apps --apply`; restart the container — bind-mounted
   source is cached by uvicorn). With `FINDINGS_REPO` empty, everything still
   lands in `cluster-agent-sandbox`. No behavior change.
2. **Rename the repo** on GitHub; set `DIGEST_REPO` =
   `…/cluster-agent-digest`; `manage.sh phase apps --apply`. Fire a manual
   dev digest and confirm the *summary* lands in the renamed repo. Drop the
   `SANDBOX_REPO` fallback next release.
3. **Prep the findings target:** create the labels in `kube-infra`; grant the
   App Issues R/W on `kube-infra`; add it to the safelist (shipped in step 1,
   confirm live).
4. **Flip findings to kube-infra (dev first):** set `FINDINGS_REPO` =
   `…/kube-infra`; `manage.sh phase apps --apply`. Fire a manual dev digest
   that contains a finding (synthetic or real); confirm exactly one
   `kube-infra` issue with `cluster-agent`+`needs-review`+`severity-*`, the
   summary's "rolled into" link points at it, and the next run **comments**
   (doesn't re-create). Then enable prd.
5. **Docs** land with the code (§ 6).

## 8. Risks + tuning

- **Low/info finding triage load.** All curated findings — including low/info
  — land in `kube-infra`. Volume is ~0–3/cluster/day, `needs-review` batches
  triage, and it beats burying them. If it ever gets noisy, set
  `FINDINGS_MIN_SEVERITY` (ships inert) — no redeploy of logic.
- **Bot presence in the ops repo.** The `cluster-agent` label makes every
  bot-filed issue filterable/dismissable; the App scope + human-close policy
  (§ 5.2) keep it from churning the backlog.
- **Summary cross-links.** The "rolled into" links now cross repos; verify
  step 4 renders them against the finding's real repo.
- **Rename redirects aren't forever.** GitHub redirects the old repo path
  until someone creates a new repo with the old name — don't recreate
  `cluster-agent-sandbox`.

## 9. Tasks

- [ ] Code: route summaries→`DIGEST_REPO`, findings→`FINDINGS_REPO` (fallbacks + inert `FINDINGS_MIN_SEVERITY`); safelist `kube-infra`; repoint default off `cluster-agent-sandbox`
- [ ] Code: "still firing as of `<date>`" recurrence comment on the dedup COMMENT path
- [ ] Code: summary "rolled into" links use the finding's real repo
- [ ] Doppler: retire `SANDBOX_REPO`; add `DIGEST_REPO`, `FINDINGS_REPO` (empty), `FINDINGS_MIN_SEVERITY` (empty) (dev+prd)
- [ ] GitHub: rename `cluster-agent-sandbox` → `cluster-agent-digest`
- [ ] GitHub: create `cluster-agent`+`needs-review` labels in `kube-infra`; grant App Issues R/W on `kube-infra`
- [ ] Rollout per § 7 (code → rename → findings-prep → findings-on, dev before prd), a manual digest fire verifying each step
- [ ] Docs: truenas-infra CLAUDE.md, design-spec § 7.3, wiki cluster-agent-runbook (same commit set)
