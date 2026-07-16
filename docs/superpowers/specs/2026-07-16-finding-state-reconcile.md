# cluster-agent — reconcile finding state against live GitHub before dedup

**Status:** Implemented — 2026-07-16
**Amends:** [`2026-07-06-cluster-agent-digest-graduation.md`](2026-07-06-cluster-agent-digest-graduation.md)
(findings→kube-infra) and [`2026-05-23-cluster-agent-design.md`](2026-05-23-cluster-agent-design.md)
§ 4.4 (dedup).
**Implementation home:** `apps/cluster-agent/` — `state/dedup.py`
(`mark_closed`), `modes/daily_digest.py` (`_reconcile_finding_states`),
`tests/test_reconcile.py`. Ships via the normal `manage.sh phase apps
--only cluster-agent --apply` + container restart flow. No Doppler/App
change.

## 1. Problem observed (2026-07-16)

Every day the digest reports **0 actionable findings** and its prose says
chronic conditions (Trivy cluster-wide CVE storm on both clusters, Longhorn
`renovate-cache` connection-refused on dev, etc.) are *"already tracked by
an existing open GH issue."* But **there are zero open `cluster-agent`
finding issues** — every finding issue in `kube-infra` is closed (newest
closed 2026-07-09), and the `cluster-agent-digest` repo only holds the two
current digest-summaries. The chronic conditions therefore have **no live
tracker**, yet the agent keeps suppressing them.

## 2. Root cause

The dedup state DB (`findings` table) only ever learns `state='closed'`
from the agent *itself* — but Mode-A finding issues are **human-close-only**
(the `needs-review` triage queue; the operator closes them after handling).
`dispatch.record()` always writes `state='open'`; nothing reconciles an
operator close back to `state.db`.

Consequences, compounded by the 2026-07-06 repo graduation:

- All 97 rows in the live `state.db` were `state='open'`, every one pointing
  at `guntars-rakitko/cluster-agent-sandbox#NN` — the **retired** pre-
  graduation repo — with `last_seen_at` in May/June.
- `daily_digest._load_open_dedup_keys()` returns those stale keys.
- The digest prompt (`prompts/digest.md`, "Already-open GH issues" block +
  selection rule "skip if an existing OPEN issue has this dedup_key") is
  fed the stale list, so the LLM correctly-per-its-instructions skips the
  chronic alert and writes *"open issue already exists"* — pointing at an
  issue that is closed **and** in a renamed repo.

The prompt is not wrong. **The dedup input is stale.** The dedup module's
own REOPEN/CREATE-after-close logic (§ 4.4) is fine but never reached,
because the LLM never emits the finding that would invoke it.

## 3. Fix

Reconcile `state.db` against real GitHub issue state **before** building the
dedup list, each digest run:

- `state/dedup.py :: mark_closed(db, dedup_key, *, closed_at)` — targeted
  `UPDATE state='closed', closed_at=?` that leaves severity/payload/
  created_at/last_seen intact.
- `modes/daily_digest.py :: _reconcile_finding_states(sdb, findings_repo)`
  — for each `state='open'` finding, group by the issue's repo, fetch that
  repo's open+closed issue state (≤2 API calls/repo), and `mark_closed` any
  finding whose issue is closed, gone, or whose repo is unreachable. `closed_at`
  = the GH close date when known, else the finding's `last_seen_at`
  (old → outside `REOPEN_WINDOW` → next emit CREATEs a fresh issue in the
  active findings repo rather than commenting on a dead one). Called from
  `run_async` right before `_load_open_dedup_keys`.

**Fail-safe:** a GH error on the *active* `FINDINGS_REPO` is swallowed (a
transient GitHub outage must not false-close live findings); a retired /
unreachable *other* repo (e.g. the redirected sandbox) is treated as
"all closed" — correct, nothing tracks those anymore. Reconcile is wrapped
so it can never break the digest.

No prompt change. No new Doppler key. Self-limiting: once the one-time batch
of stale sandbox records is closed, only the (few) live `kube-infra` finding
issues are checked each run.

## 4. Effect on next run

The stale sandbox keys drop out of `open_issue_keys`. The LLM re-evaluates
the 24h landscape with an accurate "already tracked" list and files findings
for whatever is **currently** chronic/flapping and untracked — in practice
~1–2 per cluster (the Trivy CVE-storm being the headline), not the 97
historical one-offs (those alerts aren't firing now). Going forward, every
operator close is learned on the next run.

## 5. Rollout + verification

1. `cd ~/github/truenas-infra && ./manage.sh phase apps --only cluster-agent --apply`
2. `ssh truenas_admin@nas.w1.lv 'sudo docker restart cluster-agent'`
3. Manual **dev** fire first (per CLAUDE.md § cluster-agent ops):
   `docker exec cluster-agent /venv/bin/python -c "import asyncio; from
   cluster_agent.modes.daily_digest import run_async; print(asyncio.run(run_async(cluster='dev')))"`
   — confirm the log shows `reconcile: closed N stale-open finding(s)` and a
   fresh Trivy-storm finding is filed in `kube-infra` (label `cluster-agent`).
4. Tests: `.venv/bin/python -m pytest` (adds `tests/test_reconcile.py`).

## 6. Follow-up (not in this change)

- The Trivy CVE-storm alert itself is alert-fatigue (fires 100+×/day, always
  non-actionable). Tuning/reclassifying the alert rule lives in `kube-infra`
  (`prometheus-rules-trivy.yaml`), tracked separately.
- `dispatch.py` REOPEN still only *comments* on the closed issue (doesn't
  flip it back to open) — unchanged here; noted in the graduation spec.
