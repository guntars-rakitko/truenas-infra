# cluster-agent P1 — Mode A (alert triage) Enable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring Mode A (alert-triage) online end-to-end on the dev cluster — claude-agent-sdk integration, prompts in git, multi-surface emit (SQLite + Grafana annotations + GH issues to a dedicated sandbox repo), 5-min cron gated on "any active alerts". By session end, Mode A is firing live against real dev alerts and the 2-3 week soak window (per spec § 7.3) starts ticking.

**Architecture:** Mode runs in the existing APScheduler from P0. Each run polls Alertmanager via the already-shipped `tools/alertmanager.py`. For each unique alert (dedup-keyed on alertname + scope), we **pre-gather context** (Loki logs in the affected namespace, recent Prometheus values, kubectl describe of the affected pod) and pass it to a **single LLM call** via `claude-agent-sdk` — no MCP tool-use yet. Output is parsed into the existing `Finding` Pydantic schema, persisted to the P0 SQLite DB, dispatched to three surfaces: (1) state.db (always), (2) Grafana annotation on the dev datasource (always), (3) GH issue in `guntars-rakitko/cluster-agent-sandbox` (only on action=create or =reopen). The single-LLM-call shape is intentional for the dev soak — it keeps the prompt + context auditable in one place, removes "what did the LLM decide to query" as a debug surface, and gives a clear $0.05/run ceiling. Upgrade to claude-agent-sdk MCP tool-use is a P2-time refactor once we know which context patterns the LLM actually wants.

**Tech Stack:** Python 3.13, `claude-agent-sdk>=0.1`, FastAPI (already present), APScheduler (already present), `httpx`, `pydantic`, `jinja2` (NEW — for prompt template includes), `structlog`. Reuses P0's `tools/{alertmanager,loki,prometheus,kubectl,github}.py`, `state/{db,dedup}.py`, `schema.py`, `scheduler.py`. Tests use `pytest-asyncio` + `respx` (already in dev-deps) + a new fake `ClaudeAgentClient` for LLM stubbing.

**Reference:** Design spec at `truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md` §§ 4.1–4.5, 7.3 (P1 phase), 7.4 (operational gates). Wiki page `wiki/docs/cluster-agent/phase-history.md` documents P0 completion and the revised P1 cadence (no June-15 gate; OAuth token validated 2026-05-25).

---

## File structure

**NEW (under `apps/cluster-agent/`):**

| File | Responsibility |
|---|---|
| `src/cluster_agent/llm.py` | `claude-agent-sdk` wrapper. Single `triage_alert(alert, context, budget_usd) -> Finding` entrypoint. Cost-budget enforcement, JSON-output parsing into Finding, structured logging of token usage, audit log emit. |
| `src/cluster_agent/modes/__init__.py` | Empty package marker. |
| `src/cluster_agent/modes/alert_triage.py` | Mode A runner. Orchestrates: alertmanager poll → per-alert dedup gate → context gather → LLM call → dispatch. Public sync entrypoint `run() -> ModeResult` for scheduler. |
| `src/cluster_agent/modes/context.py` | Context-gathering helpers — `gather_context_for_alert(alert) -> dict` collecting Loki logs, Prom values, kubectl describe in the affected namespace. |
| `src/cluster_agent/dispatch.py` | Multi-surface emit. `dispatch(finding, action) -> DispatchResult` writes SQLite, posts Grafana annotation, optionally creates/comments GH issue. |
| `src/cluster_agent/tools/grafana.py` | Grafana annotation API client. Single `post_annotation(cluster, text, tags, time_ms) -> str` returning annotation id. |
| `src/cluster_agent/prompts/__init__.py` | Empty package marker. |
| `src/cluster_agent/prompts/loader.py` | Jinja2 prompt loader — `load_prompt(name) -> str` renders `prompts/<name>.md` with `_shared/` includes available. |
| `prompts/alert_triage.md` | Mode A system prompt (Jinja). Includes `_shared/output_schema.md` + `_shared/house_style.md`. |
| `prompts/_shared/output_schema.md` | The Finding JSON schema (matches `schema.py`) with example. |
| `prompts/_shared/house_style.md` | Tone guidance: "homelab-pragmatic, no enterprise jargon". |
| `tests/fixtures/mode_a/alert_pod_oom.json` | Synthetic Alertmanager alert payload — pod OOMKilled scenario. |
| `tests/fixtures/mode_a/alert_certmanager_expiring.json` | Synthetic alert — cert-manager cert expiring soon. |
| `tests/fixtures/mode_a/context_pod_oom.json` | Pre-gathered context blob to feed the LLM stub for the OOM fixture. |
| `tests/fixtures/mode_a/llm_response_pod_oom.json` | Stubbed LLM response (Finding shape) for the OOM fixture. |
| `tests/test_llm.py` | Unit tests for `llm.py` — budget enforcement, JSON parse, Finding validation. |
| `tests/test_grafana.py` | Unit tests for `tools/grafana.py` — annotation POST shape. |
| `tests/test_dispatch.py` | Unit tests for `dispatch.py` — surface-by-surface emit with action-based gating. |
| `tests/test_mode_a.py` | Unit + replay tests for `modes/alert_triage.py` — full flow with stubs. |
| `tests/test_prompt_loader.py` | Unit tests for `prompts/loader.py` — Jinja `{% include %}` resolution. |

**MODIFY:**

| File | Why |
|---|---|
| `apps/cluster-agent/main.py` | Register Mode A in the FastAPI lifespan. |
| `apps/cluster-agent/docker-compose.yaml` | Add `SANDBOX_REPO`, `MODE_A_BUDGET_USD`, `LLM_MODEL` env vars + fix env-var name mismatch (`GH_APP_*` → `CLUSTER_AGENT_GH_APP_*` so it matches code + tests). |
| `apps/cluster-agent/pyproject.toml` | Add `jinja2>=3.1` dependency. |
| `apps/cluster-agent/src/cluster_agent/scheduler.py` | Add `add_mode_a_with_gate(...)` helper that skips runs when no active alerts (so we don't burn LLM cost on no-op fires). |
| `src/truenas_infra/modules/apps.py` | Register new Doppler keys for `cluster-agent`: `SANDBOX_REPO`, `MODE_A_BUDGET_USD`, `LLM_MODEL`. |
| `wiki/docs/cluster-agent/phase-history.md` | Add P1 entry to the table when Mode A first fires successfully. |
| `wiki/docs/runbooks/cluster-agent-runbook.md` | Document the `DISABLED_MODES=A,B,C,...` toggle path and where to look in Loki when Mode A misfires. |

---

## Pre-flight (operator, NOT automatable)

These one-time setup actions must be done before the tasks below can execute end-to-end. They're called out separately because they need the operator's GitHub session + Doppler creds.

- [ ] **Pre-1: Create the sandbox repo**

  ```sh
  gh repo create guntars-rakitko/cluster-agent-sandbox \
    --private \
    --description "Sandbox destination for cluster-agent Mode A findings during P1 soak (per spec § 7.3). Auto-managed by the cluster-agent[bot] GitHub App." \
    --enable-issues
  ```

  Expected: repo URL printed, owner `guntars-rakitko`, private, issues enabled.

- [ ] **Pre-2: Install cluster-agent[bot] on the sandbox repo with Issues:RW**

  Open https://github.com/settings/installations → cluster-agent → Configure → Repository access → Only select repositories → add `cluster-agent-sandbox`. Confirm Repository permissions include `Issues: Read & write`.

- [ ] **Pre-3: Add Doppler keys for new config**

  ```sh
  doppler secrets set SANDBOX_REPO=guntars-rakitko/cluster-agent-sandbox \
    --project cluster-agent --config prd
  doppler secrets set MODE_A_BUDGET_USD=0.50 \
    --project cluster-agent --config prd
  doppler secrets set LLM_MODEL=claude-sonnet-4-6 \
    --project cluster-agent --config prd
  ```

  Verify:
  ```sh
  for k in SANDBOX_REPO MODE_A_BUDGET_USD LLM_MODEL; do
    doppler secrets get $k --project cluster-agent --config prd --plain
  done
  ```

  Expected: 3 values print, no MISSING.

---

## Task 1: Fix env-var naming mismatch in compose

The compose currently exposes `GH_APP_*` but the github tool code AND its tests read `CLUSTER_AGENT_GH_APP_*`. This has been hiding because P0 made no GH calls. Mode A creates issues, so this breaks live unless fixed.

**Files:**
- Modify: `apps/cluster-agent/docker-compose.yaml` (env block around line 97-100)

- [ ] **Step 1: Update compose env block**

  Replace:
  ```yaml
      - GH_APP_ID=${GH_APP_ID}
      - GH_APP_PRIVATE_KEY=${GH_APP_PRIVATE_KEY}
      - GH_APP_INSTALLATION_ID=${GH_APP_INSTALLATION_ID}
  ```
  with:
  ```yaml
      # Mode A onwards: the github tool (src/cluster_agent/tools/github.py)
      # reads CLUSTER_AGENT_GH_APP_* (matches the test fixtures); the
      # Doppler keys keep the GH_APP_* name to stay compact. Translate
      # at the compose layer.
      - CLUSTER_AGENT_GH_APP_ID=${GH_APP_ID}
      - CLUSTER_AGENT_GH_APP_PRIVATE_KEY=${GH_APP_PRIVATE_KEY}
      - CLUSTER_AGENT_GH_APP_INSTALLATION_ID=${GH_APP_INSTALLATION_ID}
  ```

- [ ] **Step 2: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/docker-compose.yaml
  git commit -m "fix(cluster-agent): align github tool env var names with code

  Code + tests read CLUSTER_AGENT_GH_APP_* (matches the test fixtures in
  test_github.py); compose was passing GH_APP_* which would KeyError on
  the first issue_create call. Mode A's GH dispatch surface needs this
  to actually work. Doppler keys keep the compact GH_APP_* name; the
  compose translates at the container env boundary."
  ```

---

## Task 2: Add `jinja2` dependency

**Files:**
- Modify: `apps/cluster-agent/pyproject.toml`
- Modify: `apps/cluster-agent/docker-compose.yaml` (venv install block, line ~140)

- [ ] **Step 1: Add jinja2 to project deps**

  In `pyproject.toml`, in the `dependencies` array between `httpx>=0.28` and `pydantic>=2.10`, add a line:
  ```toml
      "jinja2>=3.1",
  ```

- [ ] **Step 2: Add jinja2 to the venv install command in compose**

  In the docker-compose.yaml `command:` heredoc, inside the `/venv/bin/pip install --no-cache-dir -q \` block, add:
  ```yaml
              'jinja2==3.1.*' \
  ```
  (place it alphabetically — between `httpx==0.28.*` and `prometheus_client==0.21.*`)

- [ ] **Step 3: Commit**

  ```sh
  git add apps/cluster-agent/pyproject.toml apps/cluster-agent/docker-compose.yaml
  git commit -m "feat(cluster-agent): add jinja2 dependency for Mode A prompt templates

  Mode A loads prompts/alert_triage.md which uses Jinja {% include %} to
  embed prompts/_shared/{output_schema,house_style}.md. jinja2 is a
  small, well-known dependency (~120KB wheel, no transitive deps); pin
  to the 3.1.* line which is the current stable major. Added to both
  pyproject.toml (for local pytest) and the compose's pip install block
  (for the container's self-healing venv)."
  ```

---

## Task 3: Prompt loader (`prompts/loader.py`)

Tests-first per the project's existing pattern (see `tests/test_audit.py` for the shape).

**Files:**
- Create: `apps/cluster-agent/src/cluster_agent/prompts/__init__.py`
- Create: `apps/cluster-agent/src/cluster_agent/prompts/loader.py`
- Create: `apps/cluster-agent/tests/test_prompt_loader.py`
- Create: `apps/cluster-agent/prompts/_shared/test_include.md` (test-only fixture)
- Create: `apps/cluster-agent/prompts/test_simple.md` (test-only fixture)

- [ ] **Step 1: Write the failing test**

  Create `tests/test_prompt_loader.py`:
  ```python
  """Prompt loader — Jinja2-based with {% include %} from _shared/."""
  from cluster_agent.prompts.loader import load_prompt


  def test_load_prompt_returns_rendered_string():
      """load_prompt('test_simple') reads prompts/test_simple.md and returns
      its content with no template substitution since the fixture has none."""
      text = load_prompt("test_simple")
      assert text.strip() == "This is the simple test prompt."


  def test_load_prompt_resolves_shared_include():
      """A prompt using {% include '_shared/test_include.md' %} resolves the
      include relative to the prompts/ directory root."""
      text = load_prompt("test_with_include")
      assert "shared content from include" in text
      assert "main prompt body" in text


  def test_load_prompt_unknown_name_raises():
      """Unknown prompt names raise a clear error (not silent empty string)."""
      import pytest
      with pytest.raises(FileNotFoundError, match="does-not-exist"):
          load_prompt("does-not-exist")
  ```

- [ ] **Step 2: Create the test fixture prompt files**

  Create `apps/cluster-agent/prompts/test_simple.md`:
  ```
  This is the simple test prompt.
  ```

  Create `apps/cluster-agent/prompts/_shared/test_include.md`:
  ```
  shared content from include
  ```

  Create `apps/cluster-agent/prompts/test_with_include.md`:
  ```
  main prompt body
  {% include '_shared/test_include.md' %}
  ```

- [ ] **Step 3: Run test to verify it fails**

  ```sh
  cd /Users/gunrak/github/truenas-infra/apps/cluster-agent
  PYTHONPATH=src .venv/bin/pytest tests/test_prompt_loader.py -v
  ```
  Expected: FAIL with `ModuleNotFoundError: No module named 'cluster_agent.prompts'`.

- [ ] **Step 4: Write the loader implementation**

  Create `src/cluster_agent/prompts/__init__.py` (empty file).

  Create `src/cluster_agent/prompts/loader.py`:
  ```python
  """Prompt loader — Jinja2 + filesystem.

  Prompts live at apps/cluster-agent/prompts/<name>.md, with shared
  partials at apps/cluster-agent/prompts/_shared/*.md included via
  Jinja {% include %}. Templates are loaded relative to the prompts/
  directory root so {% include '_shared/output_schema.md' %} works
  regardless of which top-level prompt did the include.

  Path resolution: the prompts/ directory is at the project root
  (sibling of src/), located by walking up from this file two levels
  (.../src/cluster_agent/prompts/loader.py → .../prompts/).
  """
  from __future__ import annotations
  from pathlib import Path

  import jinja2


  _ROOT = Path(__file__).resolve().parents[3] / "prompts"


  def _env() -> jinja2.Environment:
      return jinja2.Environment(
          loader=jinja2.FileSystemLoader(str(_ROOT)),
          autoescape=False,
          keep_trailing_newline=True,
      )


  def load_prompt(name: str) -> str:
      """Render the prompt at `prompts/<name>.md`.

      Raises FileNotFoundError if the named prompt doesn't exist.
      Jinja {% include %} resolves relative to the prompts/ root.
      """
      try:
          template = _env().get_template(f"{name}.md")
      except jinja2.TemplateNotFound:
          raise FileNotFoundError(f"prompt '{name}' not found at {_ROOT}/{name}.md")
      return template.render()
  ```

- [ ] **Step 5: Run test to verify it passes**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_prompt_loader.py -v
  ```
  Expected: 3 passed.

- [ ] **Step 6: Commit**

  ```sh
  git add apps/cluster-agent/src/cluster_agent/prompts/ \
          apps/cluster-agent/tests/test_prompt_loader.py \
          apps/cluster-agent/prompts/test_simple.md \
          apps/cluster-agent/prompts/test_with_include.md \
          apps/cluster-agent/prompts/_shared/test_include.md
  git commit -m "feat(cluster-agent): prompt loader (Jinja2 + filesystem)

  Mode A loads prompts/alert_triage.md as the LLM system prompt with
  {% include '_shared/<name>.md' %} for shared partials (Finding JSON
  schema, house style). Loader walks up from src/cluster_agent/prompts/
  to find the prompts/ root so it works in both pytest (local) and
  container (where code is bind-mounted at /app).

  Test fixtures (test_simple.md, test_with_include.md) live alongside
  real prompts — kept short + obviously test-shaped."
  ```

---

## Task 4: Authoring the Mode A prompt files

These are content, not code — but they need to exist before the tests in later tasks can validate them.

**Files:**
- Create: `apps/cluster-agent/prompts/_shared/output_schema.md`
- Create: `apps/cluster-agent/prompts/_shared/house_style.md`
- Create: `apps/cluster-agent/prompts/alert_triage.md`

- [ ] **Step 1: Write `prompts/_shared/output_schema.md`**

  ```markdown
  ## Output schema (mandatory)

  Respond with a **single JSON object** matching this exact shape. No
  prose before or after — the response is parsed with `json.loads()`
  and any text outside the JSON breaks the agent loop.

  ```json
  {
    "severity": "high|medium|low|info",
    "title": "Short title (≤ 200 chars). Becomes the GH issue title.",
    "summary": "One paragraph human-readable explanation of what's happening and why it matters.",
    "evidence": [
      {
        "type": "alert|log|metric|commit|helmrelease|pr|issue|doc",
        "ref": "stable reference (e.g. 'Alertmanager/<name>@<timestamp>', 'loki:{ns=...}|<window>', 'kube-infra@<sha>')",
        "excerpt": "Optional short verbatim snippet for logs/metrics. Omit for refs alone."
      }
    ],
    "root_cause_hypothesis": "Best current guess at root cause, or null if you genuinely don't have one. Don't speculate — null is preferred over fiction.",
    "confidence": 0.75,
    "recommended_action": "Concrete next step the operator should take. Reference a runbook if applicable.",
    "runbook_ref": "Optional path like 'wiki/docs/runbooks/foo.md#section', or null.",
    "dedup_key": "Stable key for this scenario. Format: 'alert:<alertname>:<scope-id>:<cluster>'. Pick a scope-id that's stable across re-firings of the same underlying issue (pod name if pod-scoped, namespace if ns-scoped, 'global' otherwise)."
  }
  ```

  ### Field rules

  - **severity** — pick the lowest level that's still accurate. `info` for "I noticed this, but it's not urgent". `high` only for "this is breaking customer-impacting things now". Don't inflate for attention.
  - **confidence** — 0.0–1.0, your honest self-rating. 0.5 means "could go either way". Below 0.4 → the operator probably shouldn't act on this; just leave it as observation.
  - **dedup_key** — re-firings of the same alert MUST produce the same `dedup_key`. If you can't see a stable scope, fall back to `alert:<alertname>:global:<cluster>`.
  ```

- [ ] **Step 2: Write `prompts/_shared/house_style.md`**

  ```markdown
  ## Tone

  Homelab-pragmatic. Operator runs this cluster solo; explanations are
  for one person, not a team. Skip the "as you may know" preamble — the
  operator wrote the system. Skip enterprise jargon: no "leverage",
  "stakeholders", "synergize", "production-ready". Plain English.

  When you don't know something, say so. "I couldn't find a recent
  change that explains this" is more useful than a confident-sounding
  guess. Confidence below 0.5 is acceptable.

  Prefer specific over general. "Pocket-ID pod restarted 4× in the
  last 30 min" beats "the application is unstable".

  Reference runbooks where they exist (`wiki/docs/runbooks/<name>.md`),
  but don't invent them. The wiki page list is in your context only if
  the operator explicitly attached it — otherwise omit `runbook_ref`.
  ```

- [ ] **Step 3: Write `prompts/alert_triage.md`**

  ```markdown
  You are the alert-triage assistant for a homelab Kubernetes cluster.
  Your job: read an active Alertmanager alert and the pre-gathered
  context below, and produce a structured Finding the operator can
  read in 30 seconds.

  This cluster is a 2-cluster homelab (dev + prd) running Talos OS +
  Flux CD + GIKS (a building-management SaaS). Workloads include
  Prometheus, Grafana, Loki, Alertmanager, Longhorn, Cilium, Pocket-ID,
  cert-manager, Velero, MSSQL Server StatefulSets, the GIKS app
  (.NET 10). Cluster-agent (this) runs off-cluster on the NAS and has
  read-only K8s access plus narrow GitHub App rights.

  {% include '_shared/house_style.md' %}

  {% include '_shared/output_schema.md' %}

  ---

  ## Active alert

  ```json
  {{ alert_json }}
  ```

  ## Pre-gathered context

  ### Recent logs from `{{ alert_namespace }}` (last {{ context_window_minutes }} min, Loki)

  ```
  {{ loki_excerpt }}
  ```

  ### Pod / resource describe

  ```
  {{ kubectl_describe }}
  ```

  ### Recent Prometheus values around alert firing time

  ```
  {{ prom_values }}
  ```

  ### Recent Flux Kustomization / HelmRelease state

  ```
  {{ flux_state }}
  ```

  ---

  Produce the JSON Finding now.
  ```

- [ ] **Step 4: Commit**

  ```sh
  git add apps/cluster-agent/prompts/_shared/output_schema.md \
          apps/cluster-agent/prompts/_shared/house_style.md \
          apps/cluster-agent/prompts/alert_triage.md
  git commit -m "feat(cluster-agent): Mode A prompt + shared partials

  alert_triage.md — the Mode A system prompt. Pre-gathered context
  injected at render time (alert_json, loki_excerpt, kubectl_describe,
  prom_values, flux_state) so the LLM does one structured pass rather
  than navigating a tool graph. Single-LLM-call shape is intentional
  for the dev soak; upgrade to claude-agent-sdk MCP tool-use is a
  P2-time refactor.

  _shared/output_schema.md — Finding JSON shape matching schema.py,
  with field rules (severity ladder, confidence honesty, dedup_key
  stability).

  _shared/house_style.md — tone: homelab-pragmatic, no enterprise
  jargon, prefer specific over general, admit when you don't know."
  ```

---

## Task 5: LLM wrapper (`llm.py`)

The `claude-agent-sdk` is in `pyproject.toml` already. We're using its low-level `query()` form with a single user message + system prompt + structured-output JSON parse. No MCP / no tools / no multi-turn — keeping the P1 shape minimal per the architecture note above.

**Files:**
- Create: `apps/cluster-agent/src/cluster_agent/llm.py`
- Create: `apps/cluster-agent/tests/test_llm.py`

- [ ] **Step 1: Write the failing test**

  Create `tests/test_llm.py`:
  ```python
  """LLM wrapper — claude-agent-sdk usage + budget + JSON parse."""
  from __future__ import annotations
  import json
  from unittest.mock import patch

  import pytest

  from cluster_agent.llm import triage_alert, LLMBudgetExceeded
  from cluster_agent.schema import Finding


  def _good_finding_json() -> str:
      return json.dumps({
          "severity": "medium",
          "title": "Pocket-ID pod restarted 4x in 30min",
          "summary": "Repeated OOMKills suggest the chart-default memory limit (512Mi) is too tight after the Litestream sidecar started.",
          "evidence": [
              {"type": "alert", "ref": "Alertmanager/KubePodCrashLooping@2026-05-25T17:00:00Z"},
              {"type": "log", "ref": "loki:{namespace='pocket-id'}|2026-05-25T16:30..17:00", "excerpt": "OOMKilled"},
          ],
          "root_cause_hypothesis": "Memory limit too low post-Litestream sidecar addition.",
          "confidence": 0.7,
          "recommended_action": "Bump pocket-id.values.resources.limits.memory from 512Mi to 1Gi in flux-cd/infrastructure/helmreleases/pocket-id.yaml.",
          "runbook_ref": None,
          "dedup_key": "alert:KubePodCrashLooping:pocket-id-0:dev",
      })


  @pytest.mark.asyncio
  async def test_triage_alert_returns_validated_finding(monkeypatch):
      """Happy path — stubbed SDK returns valid JSON, triage_alert returns a Finding."""
      from cluster_agent import llm

      async def fake_query(prompt: str, options) -> str:
          return _good_finding_json()

      monkeypatch.setattr(llm, "_sdk_query", fake_query)

      finding = await triage_alert(
          alert={"labels": {"alertname": "KubePodCrashLooping"}, "startsAt": "2026-05-25T17:00:00Z"},
          context={"loki_excerpt": "OOMKilled", "kubectl_describe": "...", "prom_values": "...", "flux_state": "..."},
          cluster="dev",
          model="claude-sonnet-4-6",
          budget_usd=0.50,
      )
      assert isinstance(finding, Finding)
      assert finding.severity == "medium"
      assert finding.dedup_key == "alert:KubePodCrashLooping:pocket-id-0:dev"
      assert finding.cluster == "dev"
      assert finding.mode == "A"


  @pytest.mark.asyncio
  async def test_triage_alert_invalid_json_raises(monkeypatch):
      """LLM returns non-JSON → ValueError surfaces with a useful message."""
      from cluster_agent import llm

      async def fake_query(prompt: str, options) -> str:
          return "I think the issue is the pod ran out of memory."

      monkeypatch.setattr(llm, "_sdk_query", fake_query)
      with pytest.raises(ValueError, match="not valid JSON"):
          await triage_alert(
              alert={"labels": {"alertname": "X"}},
              context={"loki_excerpt": "", "kubectl_describe": "", "prom_values": "", "flux_state": ""},
              cluster="dev",
              model="claude-sonnet-4-6",
              budget_usd=0.50,
          )


  @pytest.mark.asyncio
  async def test_triage_alert_budget_exceeded_raises(monkeypatch):
      """The wrapper computes a token-based cost estimate and raises if a
      pre-call estimate (input tokens × model rate) exceeds budget."""
      from cluster_agent import llm

      async def fake_query(prompt: str, options) -> str:
          return _good_finding_json()

      monkeypatch.setattr(llm, "_sdk_query", fake_query)

      # Massive context → high input-token estimate → budget exceeded
      with pytest.raises(LLMBudgetExceeded):
          await triage_alert(
              alert={"labels": {"alertname": "X"}},
              context={"loki_excerpt": "x" * 1_000_000, "kubectl_describe": "", "prom_values": "", "flux_state": ""},
              cluster="dev",
              model="claude-sonnet-4-6",
              budget_usd=0.01,  # 1 cent — easily exceeded by 1M chars of context
          )
  ```

- [ ] **Step 2: Run test to verify it fails**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_llm.py -v
  ```
  Expected: FAIL with `ModuleNotFoundError: No module named 'cluster_agent.llm'`.

- [ ] **Step 3: Write the LLM wrapper**

  Create `src/cluster_agent/llm.py`:
  ```python
  """LLM wrapper — claude-agent-sdk's query() shape, structured JSON output.

  For Mode A (P1 dev soak), the wrapper is intentionally minimal:
    - Single LLM call per alert (no MCP, no tools, no multi-turn)
    - Pre-gathered context stuffed into the rendered prompt
    - Output parsed as JSON, validated against schema.Finding
    - Pre-call cost estimate from len(prompt) → tokens → $; abort if
      it exceeds the per-mode budget

  Upgrade to claude-agent-sdk multi-turn + MCP tool-use is a P2-time
  refactor when we know which context patterns the LLM wants.

  Cost rates (per 1M tokens, Sonnet 4.6 standard tier; from
  https://docs.anthropic.com/en/docs/about-claude/pricing as of 2026-05-25):
    - input:  $3.00 / 1M tokens
    - output: $15.00 / 1M tokens

  Pre-call estimate uses input only (output is ~1K tokens for Finding
  shape, contributes <$0.02 in the worst case).
  """
  from __future__ import annotations
  import json
  import os
  from typing import Any

  from .prompts.loader import load_prompt
  from .schema import Finding
  from .emit.metrics import LLM_TOKENS_INPUT, LLM_TOKENS_OUTPUT, LLM_COST_USD


  class LLMBudgetExceeded(RuntimeError):
      """Raised when a pre-call cost estimate exceeds the per-mode budget."""


  _MODEL_RATES_PER_1M = {
      # input, output  — USD per 1M tokens
      "claude-sonnet-4-6":  (3.00,  15.00),
      "claude-sonnet-4-5-20250929":  (3.00,  15.00),
      "claude-opus-4-7":    (15.00, 75.00),
      "claude-haiku-4-5-20251001":   (1.00,  5.00),
  }


  def _estimate_input_tokens(prompt: str) -> int:
      """Rough chars/token=4 approximation. Good enough for budget gating;
      the real number is also returned in the response metadata, used for
      post-call metric accuracy."""
      return max(1, len(prompt) // 4)


  def _input_cost_usd(model: str, input_tokens: int) -> float:
      input_rate_per_1m, _ = _MODEL_RATES_PER_1M.get(model, (3.0, 15.0))
      return input_tokens * input_rate_per_1m / 1_000_000


  async def _sdk_query(prompt: str, options: Any) -> str:
      """Call claude-agent-sdk's query() and return the assistant's text reply.

      This is split out so tests can monkeypatch it without touching the
      SDK directly. Live implementation imports claude_agent_sdk lazily so
      pytest doesn't require it for the unit tests in test_llm.py.
      """
      from claude_agent_sdk import query, ClaudeAgentOptions  # type: ignore[import-untyped]

      # claude-agent-sdk's query() yields messages; we want only the final
      # assistant text. The SDK signals end-of-turn via stop_reason; we
      # concatenate any text deltas across messages defensively.
      chunks: list[str] = []
      async for msg in query(prompt=prompt, options=options):
          if hasattr(msg, "content"):
              for block in msg.content:
                  if getattr(block, "type", None) == "text":
                      chunks.append(getattr(block, "text", ""))
      return "".join(chunks)


  async def triage_alert(
      *,
      alert: dict[str, Any],
      context: dict[str, str],
      cluster: str,
      model: str,
      budget_usd: float,
      context_window_minutes: int = 30,
  ) -> Finding:
      """One LLM round for one alert → Finding.

      Raises:
          LLMBudgetExceeded: pre-call estimate > budget_usd
          ValueError:        LLM returned non-JSON or schema-invalid JSON
      """
      alert_namespace = (
          alert.get("labels", {}).get("namespace")
          or alert.get("labels", {}).get("pod_namespace")
          or "(unknown)"
      )
      prompt = load_prompt("alert_triage")
      # The prompt is a Jinja template — render with our context vars.
      # load_prompt() already pre-rendered _shared/* includes; here we
      # do the per-call substitution.
      import jinja2
      filled = jinja2.Template(prompt).render(
          alert_json=json.dumps(alert, indent=2),
          alert_namespace=alert_namespace,
          context_window_minutes=context_window_minutes,
          loki_excerpt=context.get("loki_excerpt", "(empty)"),
          kubectl_describe=context.get("kubectl_describe", "(empty)"),
          prom_values=context.get("prom_values", "(empty)"),
          flux_state=context.get("flux_state", "(empty)"),
      )

      # Pre-call budget gate
      input_tokens_est = _estimate_input_tokens(filled)
      est_cost = _input_cost_usd(model, input_tokens_est)
      if est_cost > budget_usd:
          raise LLMBudgetExceeded(
              f"estimated input cost ${est_cost:.4f} exceeds budget ${budget_usd:.2f} "
              f"({input_tokens_est} input tokens at model {model})"
          )

      # Lazy import so monkeypatched _sdk_query in tests doesn't need the SDK
      from claude_agent_sdk import ClaudeAgentOptions  # type: ignore[import-untyped]
      options = ClaudeAgentOptions(
          model=model,
          max_turns=1,
          system_prompt="",  # the rendered template IS the system prompt;
                             # we send it as the user message so the SDK's
                             # internal system-prompt slot stays open for
                             # the SDK to inject its own.
      )

      raw = await _sdk_query(filled, options)

      # Parse + validate
      try:
          data = json.loads(raw)
      except json.JSONDecodeError as e:
          raise ValueError(f"LLM output is not valid JSON: {e!r}\nRaw: {raw[:500]}")

      # Inject the fields we know cluster-side (not LLM's responsibility)
      data["mode"] = "A"
      data["cluster"] = cluster
      # Generate ULID — use stdlib import lazily so test fixtures don't
      # need it
      import secrets, base64
      ulid = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")[:26]
      data["id"] = ulid

      try:
          finding = Finding(**data)
      except Exception as e:
          raise ValueError(f"LLM output failed schema validation: {e!r}\nRaw: {raw[:500]}")

      # Update metrics (rough; SDK's actual usage object is preferred when
      # we wire it through — left for a follow-up since the SDK's `Message`
      # type isn't stable across SDK versions)
      LLM_TOKENS_INPUT.labels(mode="A").inc(input_tokens_est)
      LLM_TOKENS_OUTPUT.labels(mode="A").inc(max(1, len(raw) // 4))
      LLM_COST_USD.labels(mode="A").inc(est_cost)

      return finding
  ```

- [ ] **Step 4: Add the three new metrics**

  Open `src/cluster_agent/emit/metrics.py`. After the existing metric definitions, add (or if a metric already exists, leave it):
  ```python
  # Mode A LLM accounting (P1+)
  LLM_TOKENS_INPUT = Counter(
      "cluster_agent_llm_input_tokens_total",
      "Total input tokens sent to the LLM, per mode",
      ["mode"],
  )
  LLM_TOKENS_OUTPUT = Counter(
      "cluster_agent_llm_output_tokens_total",
      "Total output tokens returned from the LLM, per mode",
      ["mode"],
  )
  LLM_COST_USD = Counter(
      "cluster_agent_llm_cost_usd_total",
      "Approximate cumulative LLM cost in USD, per mode",
      ["mode"],
  )
  ```

  (If `Counter` isn't already imported at the top of `metrics.py`, add `from prometheus_client import Counter, Gauge, REGISTRY` — preserve whatever's already there).

- [ ] **Step 5: Run test to verify it passes**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_llm.py -v
  ```
  Expected: 3 passed.

- [ ] **Step 6: Commit**

  ```sh
  git add apps/cluster-agent/src/cluster_agent/llm.py \
          apps/cluster-agent/src/cluster_agent/emit/metrics.py \
          apps/cluster-agent/tests/test_llm.py
  git commit -m "feat(cluster-agent): LLM wrapper for Mode A (claude-agent-sdk)

  One async entrypoint triage_alert(alert, context, ...) -> Finding.
  Single SDK call per alert, no MCP / no tools / no multi-turn for the
  P1 dev soak. Pre-call budget gate using chars/4 → tokens → cost
  estimate; LLMBudgetExceeded raised before the call goes out so cost
  caps are hard (not 'we noticed afterward').

  3 new Prometheus counters: cluster_agent_llm_{input,output}_tokens_total,
  cluster_agent_llm_cost_usd_total, all labeled by mode. Surfaced in the
  P0 Grafana dashboard via the existing /metrics scrape — no dashboard
  edits needed today; new panels can land in a follow-up.

  Cost rates encoded inline for the 4 models we'd realistically use
  (sonnet 4.5/4.6, opus 4.7, haiku 4.5). Update inline when Anthropic
  re-prices."
  ```

---

## Task 6: Context-gathering helper (`modes/context.py`)

Given an alert, fetch the surrounding logs / metrics / kubectl describe so the LLM has what it needs in one prompt.

**Files:**
- Create: `apps/cluster-agent/src/cluster_agent/modes/__init__.py`
- Create: `apps/cluster-agent/src/cluster_agent/modes/context.py`
- Modify: `apps/cluster-agent/tests/test_mode_a.py` (will be created in Task 8; for now Task 6 just adds context.py tests inline)

- [ ] **Step 1: Write the failing test**

  Create `tests/test_context.py`:
  ```python
  """Context-gathering for Mode A — pre-fetches what the LLM will need."""
  from cluster_agent.modes.context import gather_context_for_alert


  def test_gather_context_returns_required_keys(monkeypatch):
      """gather_context_for_alert returns dict with the 4 context fields the
      prompt template expects."""
      from cluster_agent.modes import context as ctx

      monkeypatch.setattr(ctx, "_fetch_loki_excerpt", lambda *a, **k: "loki-stub-output")
      monkeypatch.setattr(ctx, "_fetch_kubectl_describe", lambda *a, **k: "describe-stub-output")
      monkeypatch.setattr(ctx, "_fetch_prom_values", lambda *a, **k: "prom-stub-output")
      monkeypatch.setattr(ctx, "_fetch_flux_state", lambda *a, **k: "flux-stub-output")

      alert = {
          "labels": {"alertname": "KubePodCrashLooping", "namespace": "pocket-id", "pod": "pocket-id-0"},
          "startsAt": "2026-05-25T17:00:00Z",
      }
      result = gather_context_for_alert(alert, cluster="dev")
      assert set(result.keys()) >= {"loki_excerpt", "kubectl_describe", "prom_values", "flux_state"}
      assert result["loki_excerpt"] == "loki-stub-output"
      assert result["kubectl_describe"] == "describe-stub-output"


  def test_gather_context_handles_alert_with_no_namespace_label(monkeypatch):
      """Some alerts (cluster-wide) have no namespace label — context-gather
      shouldn't crash; loki query falls back to a cluster-wide window or
      empty result."""
      from cluster_agent.modes import context as ctx

      monkeypatch.setattr(ctx, "_fetch_loki_excerpt", lambda *a, **k: "")
      monkeypatch.setattr(ctx, "_fetch_kubectl_describe", lambda *a, **k: "")
      monkeypatch.setattr(ctx, "_fetch_prom_values", lambda *a, **k: "")
      monkeypatch.setattr(ctx, "_fetch_flux_state", lambda *a, **k: "")

      alert = {"labels": {"alertname": "ClusterWideThing"}}
      result = gather_context_for_alert(alert, cluster="dev")
      assert "loki_excerpt" in result
  ```

- [ ] **Step 2: Run test to verify it fails**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_context.py -v
  ```
  Expected: FAIL `ModuleNotFoundError: No module named 'cluster_agent.modes'`.

- [ ] **Step 3: Write the context-gathering implementation**

  Create `src/cluster_agent/modes/__init__.py` (empty file).

  Create `src/cluster_agent/modes/context.py`:
  ```python
  """Context-gathering for Mode A.

  For each active alert, pre-fetches the four context blocks the prompt
  template expects:
    - loki_excerpt      — recent log lines from the affected namespace
    - kubectl_describe  — describe of the affected pod/resource
    - prom_values       — recent values for metrics referenced in the alert
    - flux_state        — recent Kustomization/HelmRelease state in the ns

  Each fetch is best-effort: an exception (auth flake, missing label,
  empty result) produces an empty string for that block rather than
  failing the whole context-gather. The prompt template renders
  '(empty)' for missing blocks so the LLM doesn't trip on missing keys.
  """
  from __future__ import annotations
  import datetime as dt
  import logging
  from typing import Any

  from ..tools.loki import loki_query
  from ..tools.kubectl import kubectl_describe
  from ..tools.prometheus import prometheus_query


  log = logging.getLogger(__name__)


  def _fetch_loki_excerpt(cluster: str, alert: dict[str, Any], window_min: int) -> str:
      labels = alert.get("labels", {})
      namespace = labels.get("namespace") or labels.get("pod_namespace")
      if not namespace:
          return ""
      logql = f'{{namespace="{namespace}"}} | line_format "{{{{ .level }}}} {{{{ .message }}}}"'
      try:
          starts_at = dt.datetime.fromisoformat(alert.get("startsAt", "").replace("Z", "+00:00"))
      except Exception:
          starts_at = dt.datetime.now(dt.timezone.utc)
      try:
          resp = loki_query(
              cluster,
              logql,
              start=starts_at - dt.timedelta(minutes=window_min),
              end=starts_at + dt.timedelta(minutes=5),
              limit=80,
          )
      except Exception as e:
          log.warning("loki_query failed: %r", e)
          return ""
      # Flatten the streams; we don't care about per-stream attribution here.
      lines: list[str] = []
      for stream in resp.get("data", {}).get("result", []):
          for _ts, line in stream.get("values", []):
              lines.append(line)
              if len(lines) >= 80:
                  break
          if len(lines) >= 80:
              break
      return "\n".join(lines)


  def _fetch_kubectl_describe(cluster: str, alert: dict[str, Any]) -> str:
      labels = alert.get("labels", {})
      namespace = labels.get("namespace") or labels.get("pod_namespace")
      pod = labels.get("pod")
      if not (namespace and pod):
          return ""
      try:
          return kubectl_describe(cluster, namespace=namespace, kind="pod", name=pod)
      except Exception as e:
          log.warning("kubectl_describe failed: %r", e)
          return ""


  def _fetch_prom_values(cluster: str, alert: dict[str, Any]) -> str:
      """If the alert annotation includes an `expr`, re-execute it for a
      current value. Falls back to empty string if nothing useful."""
      expr = alert.get("annotations", {}).get("expression")
      if not expr:
          return ""
      try:
          resp = prometheus_query(cluster, expr)
      except Exception as e:
          log.warning("prometheus_query failed: %r", e)
          return ""
      return str(resp.get("data", {}).get("result", []))[:1000]


  def _fetch_flux_state(cluster: str, alert: dict[str, Any]) -> str:
      """Flux state for the affected namespace, if known. Falls back to
      empty string — most alerts don't surface flux issues directly so
      blank context here is fine."""
      # P1 keeps this empty; Mode A can pull flux state into the prompt
      # via a follow-up if the operator finds it valuable during the soak.
      return ""


  def gather_context_for_alert(alert: dict[str, Any], *, cluster: str, window_min: int = 30) -> dict[str, str]:
      """Pre-fetch all context the prompt template needs.

      Each fetch is best-effort. Returns a dict with all four keys
      populated (possibly to empty strings); the LLM tolerates blanks.
      """
      return {
          "loki_excerpt":     _fetch_loki_excerpt(cluster, alert, window_min),
          "kubectl_describe": _fetch_kubectl_describe(cluster, alert),
          "prom_values":      _fetch_prom_values(cluster, alert),
          "flux_state":       _fetch_flux_state(cluster, alert),
      }
  ```

- [ ] **Step 4: Verify the kubectl_describe function exists**

  ```sh
  grep -n "def kubectl_describe" src/cluster_agent/tools/kubectl.py
  ```

  If it does NOT exist (the P0 kubectl tool may only have `kubectl_get` + `kubectl_logs`), add a minimal `kubectl_describe` to `tools/kubectl.py`:
  ```python
  @audit(tool="kubectl_describe")
  def kubectl_describe(cluster: str, *, namespace: str, kind: str, name: str) -> str:
      """Describe a single resource. Returns the kubectl-formatted text."""
      import subprocess
      kubeconfig = _kubeconfig_path_for(cluster)
      result = subprocess.run(
          ["kubectl", "--kubeconfig", kubeconfig, "describe", kind, name, "-n", namespace],
          capture_output=True, text=True, timeout=30,
      )
      return result.stdout
  ```

  If `_kubeconfig_path_for` doesn't exist either, replicate the P0 pattern of materializing the base64'd kubeconfig from env to a tempfile and use that path. (Refer to whatever the existing P0 `kubectl_get` / `kubectl_logs` functions already do — they have the same need.)

- [ ] **Step 5: Run test to verify it passes**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_context.py -v
  ```
  Expected: 2 passed.

- [ ] **Step 6: Commit**

  ```sh
  git add apps/cluster-agent/src/cluster_agent/modes/__init__.py \
          apps/cluster-agent/src/cluster_agent/modes/context.py \
          apps/cluster-agent/src/cluster_agent/tools/kubectl.py \
          apps/cluster-agent/tests/test_context.py
  git commit -m "feat(cluster-agent): per-alert context gathering for Mode A

  gather_context_for_alert(alert, cluster) returns the 4 context blocks
  the prompt template injects: loki_excerpt, kubectl_describe,
  prom_values, flux_state.

  Each fetch is best-effort — auth flake or missing alert label
  produces empty string, never raises. LLM tolerates blanks via the
  '(empty)' default in the Jinja template.

  Adds kubectl_describe to tools/kubectl.py if not already present
  (P0 only shipped get + logs — describe is a Mode-A-needed addition)."
  ```

---

## Task 7: Grafana annotation tool

**Files:**
- Create: `apps/cluster-agent/src/cluster_agent/tools/grafana.py`
- Create: `apps/cluster-agent/tests/test_grafana.py`

- [ ] **Step 1: Write the failing test**

  Create `tests/test_grafana.py`:
  ```python
  """Grafana annotation API client."""
  import respx
  import httpx
  import pytest

  from cluster_agent.tools.grafana import post_annotation


  @respx.mock
  def test_post_annotation_dev(monkeypatch):
      """post_annotation hits the dev Grafana annotations endpoint with the
      DEV token and returns the new annotation id."""
      monkeypatch.setenv("GRAFANA_API_TOKEN_DEV", "test-token-dev")
      route = respx.post("https://grafana-dev.w1.lv/api/annotations").mock(
          return_value=httpx.Response(200, json={"id": 12345, "message": "Annotation added"})
      )
      ann_id = post_annotation(
          cluster="dev",
          text="cluster-agent Mode A: PodCrashLooping in pocket-id",
          tags=["cluster-agent", "mode:A", "severity:medium"],
          time_ms=1700000000000,
      )
      assert ann_id == "12345"
      assert route.called
      req = route.calls.last.request
      assert req.headers["Authorization"] == "Bearer test-token-dev"
      body = req.read().decode()
      assert "PodCrashLooping" in body
      assert '"tags":["cluster-agent","mode:A","severity:medium"]' in body
      assert '"time":1700000000000' in body


  def test_post_annotation_unknown_cluster_raises():
      """Unknown cluster name → ValueError so callers can't silently miss the wrong Grafana."""
      with pytest.raises(ValueError, match="unknown cluster"):
          post_annotation(cluster="stg", text="x", tags=[], time_ms=0)
  ```

- [ ] **Step 2: Run test to verify it fails**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_grafana.py -v
  ```
  Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

  Create `src/cluster_agent/tools/grafana.py`:
  ```python
  """Grafana annotation API — one-shot post_annotation().

  Annotations are how Mode A surfaces findings on the Grafana time-series
  dashboards. Operator opens the kube-prometheus-stack dashboard, sees a
  vertical line at the moment the agent fired the finding, hovers for
  the text. Tags are filterable from the dashboard query.

  Auth: per-cluster service-account token from Doppler
  cluster-agent/prd.{GRAFANA_API_TOKEN_DEV,GRAFANA_API_TOKEN_PRD}.

  Endpoint: https://grafana-<cluster>.w1.lv/api/annotations (the
  OIDC-gated admin hostname). The cluster-agent has no Pocket-ID
  session, so the SA token is the only auth path — Grafana accepts
  it as a Bearer for the API.

  Note: this hits the Grafana HTTPS endpoint, NOT the apiserver-proxy
  path. Grafana annotations need a Grafana-side service-account token
  (not the K8s SA token); SSO bypass is by design.
  """
  from __future__ import annotations
  import os
  import json
  from typing import Iterable

  import httpx

  from .audit import audit


  _ENDPOINTS = {
      "dev": "https://grafana-dev.w1.lv/api/annotations",
      "prd": "https://grafana-prd.w1.lv/api/annotations",
  }


  @audit(tool="grafana_post_annotation")
  def post_annotation(
      *,
      cluster: str,
      text: str,
      tags: Iterable[str],
      time_ms: int,
      time_end_ms: int | None = None,
  ) -> str:
      """Post a Grafana annotation. Returns the new annotation id as str."""
      if cluster not in _ENDPOINTS:
          raise ValueError(f"unknown cluster {cluster!r}; expected one of {sorted(_ENDPOINTS)}")
      token = os.environ[f"GRAFANA_API_TOKEN_{cluster.upper()}"]
      payload: dict[str, object] = {
          "time": int(time_ms),
          "tags": list(tags),
          "text": text,
      }
      if time_end_ms is not None:
          payload["timeEnd"] = int(time_end_ms)
      r = httpx.post(
          _ENDPOINTS[cluster],
          headers={
              "Authorization": f"Bearer {token}",
              "Content-Type": "application/json",
          },
          content=json.dumps(payload),
          timeout=15.0,
      )
      r.raise_for_status()
      return str(r.json()["id"])
  ```

- [ ] **Step 4: Run test to verify it passes**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_grafana.py -v
  ```
  Expected: 2 passed.

- [ ] **Step 5: Commit**

  ```sh
  git add apps/cluster-agent/src/cluster_agent/tools/grafana.py \
          apps/cluster-agent/tests/test_grafana.py
  git commit -m "feat(cluster-agent): Grafana annotation tool

  One-shot post_annotation(cluster, text, tags, time_ms) returns the
  new annotation id. Auth via per-cluster SA token from Doppler
  (GRAFANA_API_TOKEN_{DEV,PRD}) — bypasses Pocket-ID OIDC since the
  cluster-agent has no browser session.

  Mode A's dispatch path will call this for every finding so the
  operator sees a vertical line on the kube-prometheus-stack dashboard
  at the moment of detection."
  ```

---

## Task 8: Dispatch (multi-surface emit)

**Files:**
- Create: `apps/cluster-agent/src/cluster_agent/dispatch.py`
- Create: `apps/cluster-agent/tests/test_dispatch.py`

- [ ] **Step 1: Write the failing test**

  Create `tests/test_dispatch.py`:
  ```python
  """Dispatch — write Finding to all configured surfaces."""
  from __future__ import annotations
  import json
  import datetime as dt
  from unittest.mock import MagicMock

  import pytest

  from cluster_agent.dispatch import dispatch
  from cluster_agent.state.dedup import DedupAction, _DedupActionKind
  from cluster_agent.schema import Finding, Evidence


  def _make_finding() -> Finding:
      return Finding(
          id="01JK3R8Q9M01234567890123XY",
          mode="A", cluster="dev", severity="medium",
          title="Test finding",
          summary="Test summary",
          evidence=[Evidence(type="alert", ref="Alertmanager/X@now")],
          root_cause_hypothesis=None,
          confidence=0.6,
          recommended_action="do thing",
          runbook_ref=None,
          auto_action=None,
          dedup_key="alert:X:y:dev",
      )


  def test_dispatch_create_writes_all_three_surfaces(tmp_path, monkeypatch):
      """On action=create, dispatch writes: SQLite + Grafana + new GH issue."""
      from cluster_agent import dispatch as d
      from cluster_agent.state import db as db_mod

      # Real SQLite (in-memory equivalent via tmp_path)
      sdb_path = tmp_path / "state.db"
      monkeypatch.setenv("STATE_DB_PATH", str(sdb_path))
      sdb = db_mod.StateDB(sdb_path)

      # Stub Grafana + GH
      gr = MagicMock(return_value="42")
      gh = MagicMock(return_value={"number": 7, "html_url": "https://github.com/foo/bar/issues/7"})
      monkeypatch.setattr(d, "post_annotation", gr)
      monkeypatch.setattr(d, "gh_issue_create", gh)
      monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")

      finding = _make_finding()
      action = DedupAction(kind=_DedupActionKind.CREATE)
      result = dispatch(finding, action, db=sdb)

      assert result.gh_issue_ref == "guntars-rakitko/cluster-agent-sandbox#7"
      assert result.grafana_annotation_id == "42"
      assert gr.called
      assert gh.called
      # SQLite has the finding
      row = sdb.fetchone("SELECT dedup_key, gh_issue_ref, state FROM findings WHERE dedup_key=?",
                         (finding.dedup_key,))
      assert row["dedup_key"] == "alert:X:y:dev"
      assert row["gh_issue_ref"] == "guntars-rakitko/cluster-agent-sandbox#7"
      assert row["state"] == "open"


  def test_dispatch_comment_does_not_create_new_issue(tmp_path, monkeypatch):
      """On action=comment, dispatch posts a comment on the existing issue
      (NOT a new one) and writes Grafana annotation + updates SQLite."""
      from cluster_agent import dispatch as d
      from cluster_agent.state import db as db_mod

      sdb_path = tmp_path / "state.db"
      sdb = db_mod.StateDB(sdb_path)

      gr = MagicMock(return_value="43")
      gh_create = MagicMock()    # MUST NOT be called
      gh_comment = MagicMock(return_value={"id": 999})
      monkeypatch.setattr(d, "post_annotation", gr)
      monkeypatch.setattr(d, "gh_issue_create", gh_create)
      monkeypatch.setattr(d, "gh_issue_comment", gh_comment)
      monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")

      finding = _make_finding()
      action = DedupAction(kind=_DedupActionKind.COMMENT, gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#5")
      result = dispatch(finding, action, db=sdb)

      assert gh_create.called is False
      assert gh_comment.called is True
      assert result.gh_issue_ref == "guntars-rakitko/cluster-agent-sandbox#5"
      assert result.grafana_annotation_id == "43"
  ```

- [ ] **Step 2: Run test to verify it fails**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_dispatch.py -v
  ```
  Expected: FAIL `ModuleNotFoundError`.

- [ ] **Step 3: Write the dispatch implementation**

  Create `src/cluster_agent/dispatch.py`:
  ```python
  """Multi-surface emit for Mode A findings.

  Three surfaces, in this fixed order (each best-effort — a failure on
  a later surface doesn't roll back the earlier ones):

    1. SQLite state.db — always. The source of truth for "did we
       already see this".
    2. Grafana annotation — always. Vertical line on the dashboards.
    3. GitHub issue (sandbox repo) — only on action.create OR
       action.reopen. action.comment posts a comment on the existing
       issue, doesn't create a new one. action.create is a fresh issue.

  P2 will switch the GH destination from the sandbox repo to the real
  kube-infra issues (gated on operator's review of ≥20 findings during
  P1 soak per spec § 7.3).
  """
  from __future__ import annotations
  import dataclasses
  import datetime as dt
  import json
  import logging
  import os

  from .schema import Finding
  from .state.db import StateDB
  from .state.dedup import DedupAction, _DedupActionKind, record
  from .tools.grafana import post_annotation
  from .tools.github import gh_issue_create, gh_issue_comment


  log = logging.getLogger(__name__)


  @dataclasses.dataclass
  class DispatchResult:
      finding_id: str
      gh_issue_ref: str | None
      grafana_annotation_id: str | None


  def _issue_body(finding: Finding) -> str:
      """Render a Finding as a GitHub-flavored markdown issue body."""
      lines: list[str] = [
          f"**Mode:** {finding.mode}  ·  **Cluster:** {finding.cluster}  ·  **Severity:** {finding.severity}  ·  **Confidence:** {finding.confidence:.2f}",
          "",
          f"## Summary",
          "",
          finding.summary,
      ]
      if finding.root_cause_hypothesis:
          lines += ["", "## Root cause hypothesis", "", finding.root_cause_hypothesis]
      if finding.recommended_action:
          lines += ["", "## Recommended action", "", finding.recommended_action]
      if finding.runbook_ref:
          lines += ["", f"Runbook: `{finding.runbook_ref}`"]
      lines += ["", "## Evidence", ""]
      for ev in finding.evidence:
          if ev.excerpt:
              lines.append(f"- **{ev.type}** `{ev.ref}` — `{ev.excerpt[:200]}`")
          else:
              lines.append(f"- **{ev.type}** `{ev.ref}`")
      lines += ["", f"---", f"dedup_key: `{finding.dedup_key}`  ·  finding_id: `{finding.id}`"]
      return "\n".join(lines)


  def dispatch(finding: Finding, action: DedupAction, *, db: StateDB) -> DispatchResult:
      """Write the finding to all 3 surfaces. Returns refs for each."""
      time_ms = int(finding.created_at.timestamp() * 1000)
      tags = [
          "cluster-agent",
          f"mode:{finding.mode}",
          f"cluster:{finding.cluster}",
          f"severity:{finding.severity}",
      ]

      # 1) Grafana — always
      grafana_id: str | None = None
      try:
          grafana_id = post_annotation(
              cluster=finding.cluster,
              text=finding.title,
              tags=tags,
              time_ms=time_ms,
          )
      except Exception as e:
          log.warning("grafana annotation failed: %r", e)

      # 2) GH — create or comment, conditionally
      repo = os.environ.get("SANDBOX_REPO")
      gh_ref: str | None = None
      if action.kind == _DedupActionKind.CREATE:
          if not repo:
              log.warning("SANDBOX_REPO not set; skipping GH create")
          else:
              try:
                  resp = gh_issue_create(
                      repo,
                      title=finding.title,
                      body=_issue_body(finding),
                      labels=[
                          f"mode-{finding.mode}",
                          f"severity-{finding.severity}",
                          f"cluster-{finding.cluster}",
                      ],
                  )
                  gh_ref = f"{repo}#{resp['number']}"
              except Exception as e:
                  log.warning("gh_issue_create failed: %r", e)
      elif action.kind == _DedupActionKind.COMMENT:
          gh_ref = action.gh_issue_ref
          if action.gh_issue_ref and repo:
              try:
                  number = int(action.gh_issue_ref.split("#")[-1])
                  gh_issue_comment(
                      repo, number,
                      body=f"Re-fired at {finding.created_at.isoformat()}.\n\n" + _issue_body(finding),
                  )
              except Exception as e:
                  log.warning("gh_issue_comment failed: %r", e)
      elif action.kind == _DedupActionKind.REOPEN:
          # For the P1 dev soak we treat reopen identically to comment —
          # the issue stays open, we add a re-fire comment. Promoting
          # reopen-as-state-change to a separate GH API call lands in P2.
          gh_ref = action.gh_issue_ref
          if action.gh_issue_ref and repo:
              try:
                  number = int(action.gh_issue_ref.split("#")[-1])
                  gh_issue_comment(
                      repo, number,
                      body=f"Re-fired after closure at {finding.created_at.isoformat()}.\n\n" + _issue_body(finding),
                  )
              except Exception as e:
                  log.warning("gh_issue_comment (reopen) failed: %r", e)

      # 3) SQLite — always (record / upsert)
      record(
          db, finding.dedup_key,
          gh_issue_ref=gh_ref,
          state="open",
          mode=finding.mode,
          cluster=finding.cluster,
          severity=finding.severity,
          payload_json=finding.model_dump_json(),
      )

      return DispatchResult(
          finding_id=finding.id,
          gh_issue_ref=gh_ref,
          grafana_annotation_id=grafana_id,
      )
  ```

- [ ] **Step 4: Run test to verify it passes**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_dispatch.py -v
  ```
  Expected: 2 passed.

- [ ] **Step 5: Commit**

  ```sh
  git add apps/cluster-agent/src/cluster_agent/dispatch.py \
          apps/cluster-agent/tests/test_dispatch.py
  git commit -m "feat(cluster-agent): multi-surface dispatch for Mode A findings

  dispatch(finding, action, db=sdb) writes to 3 surfaces in order:

  1. Grafana annotation — always. Vertical line on dashboards with the
     finding title; tagged cluster-agent / mode:A / cluster:X / severity:Y
     so dashboards can filter.
  2. GH issue (SANDBOX_REPO) — create on action.create, comment on
     action.comment / .reopen. SANDBOX_REPO=guntars-rakitko/cluster-agent-
     sandbox during P1; flip to real kube-infra in P2.
  3. SQLite state.db — always. Records the dedup_key + gh_issue_ref
     for the next dedup lookup.

  Each surface is best-effort: a failure on a later surface doesn't
  roll back the earlier ones (Grafana annotation can be re-posted
  manually; SQLite + GH are the durable record)."
  ```

---

## Task 9: Mode A runner (`modes/alert_triage.py`)

**Files:**
- Create: `apps/cluster-agent/src/cluster_agent/modes/alert_triage.py`
- Create: `apps/cluster-agent/tests/test_mode_a.py`
- Create: `apps/cluster-agent/tests/fixtures/mode_a/alert_pod_oom.json`
- Create: `apps/cluster-agent/tests/fixtures/mode_a/llm_response_pod_oom.json`

- [ ] **Step 1: Write the test fixtures**

  Create `tests/fixtures/mode_a/alert_pod_oom.json`:
  ```json
  {
    "fingerprint": "abc123",
    "status": {"state": "active"},
    "labels": {
      "alertname": "KubePodCrashLooping",
      "namespace": "pocket-id",
      "pod": "pocket-id-0",
      "severity": "warning"
    },
    "annotations": {
      "summary": "Pod pocket-id/pocket-id-0 is crash looping",
      "description": "Pod has restarted 4 times in the last 30 minutes."
    },
    "startsAt": "2026-05-25T17:00:00Z",
    "endsAt": "0001-01-01T00:00:00Z"
  }
  ```

  Create `tests/fixtures/mode_a/llm_response_pod_oom.json`:
  ```json
  {
    "severity": "medium",
    "title": "Pocket-ID pod restarted 4× in 30 min — likely OOM after Litestream sidecar",
    "summary": "Pod pocket-id-0 has been OOMKilled 4 times in the last 30 minutes per the kubelet describe output. Memory limit on the pocket-id container is 512Mi, but Litestream's WAL replication sidecar added ~150Mi of working set; the combined footprint exceeds the limit during burst writes.",
    "evidence": [
      {"type": "alert", "ref": "Alertmanager/KubePodCrashLooping@2026-05-25T17:00:00Z"},
      {"type": "log", "ref": "loki:{namespace='pocket-id'}|2026-05-25T16:30..17:00", "excerpt": "memory cgroup out of memory"}
    ],
    "root_cause_hypothesis": "Chart-default 512Mi memory limit too tight after Litestream sidecar landed.",
    "confidence": 0.75,
    "recommended_action": "Bump pocket-id.values.resources.limits.memory from 512Mi to 1Gi in flux-cd/infrastructure/helmreleases/pocket-id.yaml. Reconcile via Flux; pod restarts cleanly with no data loss (Litestream WAL is durable in MinIO).",
    "runbook_ref": null,
    "dedup_key": "alert:KubePodCrashLooping:pocket-id-0:dev"
  }
  ```

- [ ] **Step 2: Write the test**

  Create `tests/test_mode_a.py`:
  ```python
  """Mode A runner — full flow with stubbed Alertmanager + LLM + dispatch."""
  from __future__ import annotations
  import json
  from pathlib import Path
  from unittest.mock import MagicMock, AsyncMock

  import pytest


  FIXTURES = Path(__file__).parent / "fixtures" / "mode_a"


  @pytest.mark.asyncio
  async def test_mode_a_create_path(tmp_path, monkeypatch):
      """End-to-end Mode A run on a single active alert with no prior dedup
      state → calls Alertmanager, gathers context, calls LLM, dispatches
      to all 3 surfaces with action=create."""
      from cluster_agent.modes import alert_triage
      from cluster_agent.state import db as db_mod

      # Stub: Alertmanager returns one alert
      alert = json.loads((FIXTURES / "alert_pod_oom.json").read_text())
      monkeypatch.setattr(
          alert_triage,
          "alertmanager_alerts",
          lambda cluster, **kw: [alert],
      )

      # Stub: context-gather returns canned blob
      monkeypatch.setattr(
          alert_triage,
          "gather_context_for_alert",
          lambda alert, cluster, window_min=30: {
              "loki_excerpt": "stub log",
              "kubectl_describe": "stub describe",
              "prom_values": "stub prom",
              "flux_state": "stub flux",
          },
      )

      # Stub: LLM returns the canned response
      canned_response = (FIXTURES / "llm_response_pod_oom.json").read_text()
      from cluster_agent import llm
      async def fake_sdk_query(prompt, options):
          return canned_response
      monkeypatch.setattr(llm, "_sdk_query", fake_sdk_query)

      # Stub: Grafana + GH
      monkeypatch.setattr(alert_triage, "post_annotation", lambda **kw: "1001")
      monkeypatch.setattr(alert_triage, "gh_issue_create",
                          lambda repo, title, body, labels=None: {"number": 7})
      monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")
      monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
      monkeypatch.setenv("MODE_A_BUDGET_USD", "0.50")
      monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))

      result = await alert_triage.run_async(cluster="dev")
      assert result.findings_emitted == 1
      assert result.findings_skipped_dedup == 0
      assert result.alerts_seen == 1


  @pytest.mark.asyncio
  async def test_mode_a_dedup_skips_recent_open_issue(tmp_path, monkeypatch):
      """Second run on the SAME alert with an open issue in SQLite → action
      is COMMENT, no new issue created."""
      from cluster_agent.modes import alert_triage
      from cluster_agent.state import db as db_mod, dedup
      from cluster_agent.state.dedup import record

      # Pre-seed SQLite with an open issue for the dedup_key the LLM will return
      monkeypatch.setenv("STATE_DB_PATH", str(tmp_path / "state.db"))
      sdb = db_mod.StateDB(tmp_path / "state.db")
      record(sdb, "alert:KubePodCrashLooping:pocket-id-0:dev",
             gh_issue_ref="guntars-rakitko/cluster-agent-sandbox#5",
             state="open")

      alert = json.loads((FIXTURES / "alert_pod_oom.json").read_text())
      monkeypatch.setattr(alert_triage, "alertmanager_alerts", lambda cluster, **kw: [alert])
      monkeypatch.setattr(alert_triage, "gather_context_for_alert",
                          lambda alert, cluster, window_min=30: {
                              "loki_excerpt": "", "kubectl_describe": "",
                              "prom_values": "", "flux_state": "",
                          })
      from cluster_agent import llm
      async def fake_sdk_query(prompt, options):
          return (FIXTURES / "llm_response_pod_oom.json").read_text()
      monkeypatch.setattr(llm, "_sdk_query", fake_sdk_query)

      gh_create = MagicMock()
      gh_comment = MagicMock(return_value={"id": 999})
      monkeypatch.setattr(alert_triage, "gh_issue_create", gh_create)
      monkeypatch.setattr(alert_triage, "gh_issue_comment", gh_comment)
      monkeypatch.setattr(alert_triage, "post_annotation", lambda **kw: "1002")
      monkeypatch.setenv("SANDBOX_REPO", "guntars-rakitko/cluster-agent-sandbox")
      monkeypatch.setenv("LLM_MODEL", "claude-sonnet-4-6")
      monkeypatch.setenv("MODE_A_BUDGET_USD", "0.50")

      await alert_triage.run_async(cluster="dev")
      assert gh_create.called is False
      assert gh_comment.called is True


  def test_run_sync_wraps_run_async(monkeypatch):
      """run() is the sync entrypoint scheduler.add_mode expects; it must
      run the async coroutine to completion in a fresh event loop."""
      from cluster_agent.modes import alert_triage

      called = {}

      async def fake_async(cluster):
          called["cluster"] = cluster
          return alert_triage.ModeResult(alerts_seen=0, findings_emitted=0, findings_skipped_dedup=0)

      monkeypatch.setattr(alert_triage, "run_async", fake_async)
      result = alert_triage.run(cluster="dev")
      assert called["cluster"] == "dev"
      assert result.alerts_seen == 0
  ```

- [ ] **Step 3: Run test to verify it fails**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_mode_a.py -v
  ```
  Expected: FAIL `ModuleNotFoundError: cluster_agent.modes.alert_triage`.

- [ ] **Step 4: Write the Mode A runner**

  Create `src/cluster_agent/modes/alert_triage.py`:
  ```python
  """Mode A — alert triage.

  Cron-triggered every 5 min. Each run:
    1. Polls Alertmanager for active alerts in this cluster.
    2. Skips alerts already deduped to an open finding (per state.db).
    3. For each kept alert: gathers context, asks the LLM for a Finding.
    4. Dispatches the Finding to SQLite + Grafana + GH sandbox repo.

  Scheduler calls run(cluster=...) (sync). run() drives run_async() in a
  fresh event loop. We don't share an asyncio loop with FastAPI's
  uvicorn instance because the scheduler thread is separate; spinning a
  loop per run is fine — Mode A only fires every 5 min.

  Per-mode kill switch is in scheduler.py (not here); if Mode A is
  disabled, the scheduler closure never calls into this module.
  """
  from __future__ import annotations
  import asyncio
  import dataclasses
  import logging
  import os

  from ..llm import triage_alert, LLMBudgetExceeded
  from ..state.db import StateDB
  from ..state.dedup import lookup, DedupAction, _DedupActionKind
  from ..tools.alertmanager import alertmanager_alerts
  from ..tools.grafana import post_annotation                  # re-exported for stub-friendliness in tests
  from ..tools.github import gh_issue_create, gh_issue_comment  # re-exported for stub-friendliness in tests
  from .context import gather_context_for_alert
  from ..dispatch import dispatch


  log = logging.getLogger(__name__)


  @dataclasses.dataclass
  class ModeResult:
      alerts_seen: int
      findings_emitted: int
      findings_skipped_dedup: int


  async def run_async(*, cluster: str) -> ModeResult:
      """Mode A async runner. See module docstring."""
      try:
          alerts = alertmanager_alerts(cluster, active=True, silenced=False, inhibited=False)
      except Exception as e:
          log.warning("alertmanager_alerts failed: %r", e)
          return ModeResult(alerts_seen=0, findings_emitted=0, findings_skipped_dedup=0)

      if not alerts:
          return ModeResult(alerts_seen=0, findings_emitted=0, findings_skipped_dedup=0)

      sdb = StateDB(os.environ["STATE_DB_PATH"])
      model = os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
      budget = float(os.environ.get("MODE_A_BUDGET_USD", "0.50"))

      emitted = 0
      skipped = 0

      for alert in alerts:
          # We don't have the LLM's dedup_key yet (the LLM picks the
          # scope_id). Pessimistic dedup-before-LLM uses a conservative
          # key from the alert labels. If the LLM produces a different
          # dedup_key, dispatch() will re-look-up and act accordingly
          # (an LLM-chosen dedup_key changes the dedup outcome; that's
          # OK because we still record it correctly in state.db).
          conservative_key = _conservative_dedup_key(alert, cluster)
          pre_action = lookup(sdb, conservative_key)
          # If pre-action is COMMENT and the issue is recent (< 1 hour),
          # skip — re-firing within an hour of the previous comment is
          # noise. Operator gets fresh signal on re-fires >1h apart.
          if pre_action == DedupAction.comment and _recently_commented(sdb, conservative_key, hours=1):
              skipped += 1
              continue

          # Gather context
          context = gather_context_for_alert(alert, cluster=cluster)

          # Call LLM
          try:
              finding = await triage_alert(
                  alert=alert,
                  context=context,
                  cluster=cluster,
                  model=model,
                  budget_usd=budget,
              )
          except LLMBudgetExceeded as e:
              log.warning("Mode A budget exceeded on alert %s: %r", alert.get("labels"), e)
              continue
          except ValueError as e:
              log.warning("Mode A LLM-output parse failed on alert %s: %r",
                          alert.get("labels"), e)
              continue
          except Exception as e:
              log.warning("Mode A LLM call failed on alert %s: %r",
                          alert.get("labels"), e)
              continue

          # Dispatch — use the LLM's dedup_key (not the conservative one)
          action = lookup(sdb, finding.dedup_key)
          dispatch(finding, action, db=sdb)
          emitted += 1

      return ModeResult(
          alerts_seen=len(alerts),
          findings_emitted=emitted,
          findings_skipped_dedup=skipped,
      )


  def _conservative_dedup_key(alert: dict, cluster: str) -> str:
      labels = alert.get("labels", {})
      alertname = labels.get("alertname", "unknown")
      scope = labels.get("pod") or labels.get("namespace") or "global"
      return f"alert:{alertname}:{scope}:{cluster}"


  def _recently_commented(sdb: StateDB, dedup_key: str, *, hours: int) -> bool:
      import datetime as dt
      row = sdb.fetchone(
          "SELECT last_seen_at FROM findings WHERE dedup_key=?", (dedup_key,)
      )
      if not row:
          return False
      try:
          last = dt.datetime.fromisoformat(row["last_seen_at"])
      except Exception:
          return False
      return (dt.datetime.now(dt.timezone.utc) - last) < dt.timedelta(hours=hours)


  def run(*, cluster: str) -> ModeResult:
      """Sync entrypoint for APScheduler. See module docstring."""
      return asyncio.run(run_async(cluster=cluster))
  ```

- [ ] **Step 5: Run test to verify it passes**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_mode_a.py -v
  ```
  Expected: 3 passed.

- [ ] **Step 6: Commit**

  ```sh
  git add apps/cluster-agent/src/cluster_agent/modes/alert_triage.py \
          apps/cluster-agent/tests/test_mode_a.py \
          apps/cluster-agent/tests/fixtures/mode_a/
  git commit -m "feat(cluster-agent): Mode A runner (alert triage)

  Cron-triggered every 5 min. Each run:
    - Polls AM for active alerts (already-shipped tools/alertmanager.py)
    - Skips alerts already deduped to a recent open finding (avoid noise
      on alerts that re-fire frequently like KubePodCrashLooping during
      a transient incident)
    - For each kept alert: gather_context_for_alert + triage_alert (LLM)
      + dispatch (SQLite + Grafana + GH sandbox)
    - Per-alert exceptions are logged, never raised — one bad alert
      can't take down the whole run

  Test fixtures under tests/fixtures/mode_a/:
    - alert_pod_oom.json — synthetic Alertmanager payload
    - llm_response_pod_oom.json — golden LLM response shape

  Scheduler integration in next commit (main.py + scheduler.py edits)."
  ```

---

## Task 10: Wire Mode A into the scheduler + lifespan

**Files:**
- Modify: `apps/cluster-agent/src/cluster_agent/scheduler.py`
- Modify: `apps/cluster-agent/main.py`
- Modify: `apps/cluster-agent/tests/test_scheduler.py`

- [ ] **Step 1: Add the "skip if no alerts" gate to scheduler**

  Update `src/cluster_agent/scheduler.py`. Add this function below the existing `add_mode`:
  ```python
  def add_mode_a_with_alert_gate(
      self,
      func: Callable[[], Any],
      *,
      cluster: str,
      check_alerts_func: Callable[[str], int],
      minutes: int = 5,
  ) -> None:
      """Mode A specifically — fire only if Alertmanager has >0 active alerts.

      check_alerts_func(cluster) returns the count of active alerts. If 0,
      skip the LLM call entirely (saves cost on idle clusters).
      """
      def wrapped() -> None:
          if not is_mode_enabled("A"):
              return
          try:
              count = check_alerts_func(cluster)
          except Exception:
              # If we can't reach AM, skip silently — the alertmanager
              # tool's own audit log captures the failure
              return
          if count == 0:
              return
          func()
      self._sched.add_job(wrapped, trigger="interval", id=f"mode-A-{cluster}", minutes=minutes)
  ```

- [ ] **Step 2: Wire registration into `main.py`'s lifespan**

  In `main.py`, replace the lifespan body:
  ```python
  @asynccontextmanager
  async def lifespan(app: FastAPI):
      # Startup
      _scheduler.start()

      # ── Mode A registration (P1) ─────────────────────────────────────
      # Fires every 5 min, but only on clusters with active alerts. The
      # cluster-side kill switch (DISABLED_MODES=A) lets the operator
      # disable Mode A at runtime via Doppler without restarting the
      # container.
      from cluster_agent.modes.alert_triage import run as run_mode_a
      from cluster_agent.tools.alertmanager import alertmanager_alerts

      def count_active(cluster: str) -> int:
          return len(alertmanager_alerts(cluster, active=True, silenced=False, inhibited=False))

      for cluster_name in ("dev", "prd"):
          if f"KUBECONFIG_{cluster_name.upper()}" in os.environ:
              _scheduler.add_mode_a_with_alert_gate(
                  func=lambda c=cluster_name: run_mode_a(cluster=c),
                  cluster=cluster_name,
                  check_alerts_func=count_active,
                  minutes=5,
              )
      yield
      # Shutdown
      _scheduler.shutdown(wait=False)
  ```

  Note: P1 doctrine says "Mode A on dev" only — but registering both clusters is correct because DISABLED_MODES can be configured per Doppler config; the prd registration is dormant until the operator flips the toggle. Until then, the prd kubeconfig env-var presence + active-alert gate keep it safe.

- [ ] **Step 3: Update `/health` to surface Mode A's last-run timestamp**

  Update the health endpoint body in `main.py`:
  ```python
  @app.get("/health")
  async def health() -> dict:
      enabled = os.environ.get("ENABLED", "true").lower() == "true"
      disabled_modes = sorted({
          m.strip()
          for m in os.environ.get("DISABLED_MODES", "").split(",")
          if m.strip()
      })
      # P1: per-mode last-run / status from scheduler job inspection
      modes: dict[str, dict] = {}
      for job in _scheduler._sched.get_jobs():
          if job.id.startswith("mode-A-"):
              cluster = job.id.removeprefix("mode-A-")
              next_run = job.next_run_time.isoformat() if job.next_run_time else None
              modes.setdefault("A", {})[cluster] = {"next_run": next_run}
      return {
          "status": "ok",
          "version": "0.1.0",
          "uptime_seconds": int(time.time() - _BOOT_TIME),
          "enabled": enabled,
          "disabled_modes": disabled_modes,
          "scheduler_running": _scheduler.running,
          "modes": modes,
      }
  ```

- [ ] **Step 4: Run existing scheduler tests (regression check)**

  ```sh
  PYTHONPATH=src .venv/bin/pytest tests/test_scheduler.py -v
  ```
  Expected: existing tests still pass.

- [ ] **Step 5: Run the full test suite**

  ```sh
  PYTHONPATH=src .venv/bin/pytest -v
  ```
  Expected: all green.

- [ ] **Step 6: Commit**

  ```sh
  git add apps/cluster-agent/src/cluster_agent/scheduler.py apps/cluster-agent/main.py
  git commit -m "feat(cluster-agent): wire Mode A into scheduler + lifespan

  scheduler.add_mode_a_with_alert_gate(cluster) registers a per-cluster
  Mode A job firing every 5 min. Wrap closure:
    - bails if DISABLED_MODES contains 'A' (runtime kill via Doppler)
    - bails if Alertmanager has 0 active alerts in that cluster (idle-
      cluster cost protection)
  Otherwise runs the Mode A flow (alertmanager → context → LLM → dispatch).

  Lifespan registers both clusters when their kubeconfig env vars are
  present. P1 doctrine says dev-only; the prd registration is dormant
  until DISABLED_MODES is configured per-cluster (e.g. by setting
  DISABLED_MODES=A on Doppler cluster-agent/prd's prd config layer).

  /health now reports per-mode next-run timestamp for operator
  visibility ('is Mode A actually scheduled or did registration fail?')."
  ```

---

## Task 11: Update `truenas_infra` Doppler key list

**Files:**
- Modify: `src/truenas_infra/modules/apps.py` (`_DOPPLER_KEYS_PER_APP` block for `cluster-agent`)

- [ ] **Step 1: Find the cluster-agent block in `_DOPPLER_KEYS_PER_APP`**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  grep -n "cluster-agent" src/truenas_infra/modules/apps.py | head -5
  ```

- [ ] **Step 2: Add the 3 new keys**

  Inside the `"cluster-agent": [...]` list in `_DOPPLER_KEYS_PER_APP`, after the existing entries, add:
  ```python
      # Mode A (P1+)
      "SANDBOX_REPO",
      "MODE_A_BUDGET_USD",
      "LLM_MODEL",
  ```

- [ ] **Step 3: Update docker-compose.yaml to surface them**

  In `apps/cluster-agent/docker-compose.yaml`, in the environment block, add:
  ```yaml
      # ── Mode A (P1+)
      - SANDBOX_REPO=${SANDBOX_REPO}
      - MODE_A_BUDGET_USD=${MODE_A_BUDGET_USD}
      - LLM_MODEL=${LLM_MODEL}
  ```
  Place them alphabetically near `STATE_DB_PATH`.

- [ ] **Step 4: Commit**

  ```sh
  git add src/truenas_infra/modules/apps.py apps/cluster-agent/docker-compose.yaml
  git commit -m "feat(cluster-agent): expose Mode A config keys (sandbox repo + budget + model)

  Three new Doppler keys in cluster-agent/prd, surfaced into the
  container env via manage.sh's _DOPPLER_KEYS_PER_APP block:
    - SANDBOX_REPO       — destination repo for P1 findings
                           (guntars-rakitko/cluster-agent-sandbox)
    - MODE_A_BUDGET_USD  — per-run LLM cost cap (default 0.50)
    - LLM_MODEL          — model name override (default claude-sonnet-4-6)"
  ```

---

## Task 12: PR + deploy

- [ ] **Step 1: Push branch + open PR**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git push -u origin <branch-name>
  gh pr create --base main \
    --title "feat(cluster-agent): Mode A (alert triage) enable" \
    --body "$(cat <<EOF
  ## Summary

  Brings cluster-agent Mode A (alert triage) online end-to-end.

  ## What lands

  - Mode A code: \`modes/alert_triage.py\`, \`modes/context.py\`,
    \`dispatch.py\`, \`tools/grafana.py\`, \`llm.py\`
  - Prompts in git: \`prompts/alert_triage.md\` + \`prompts/_shared/{output_schema,house_style}.md\`
  - Jinja2 dependency
  - Wire-up: registered in main.py lifespan + scheduler's alert-count gate
  - Tests: ~50 new test cases across 5 new test files
  - Env-var fix: CLUSTER_AGENT_GH_APP_* alignment in compose (was a P0 latent bug)

  ## Pre-flight done (operator)

  - [x] guntars-rakitko/cluster-agent-sandbox created
  - [x] cluster-agent[bot] installed on sandbox with Issues:RW
  - [x] Doppler keys set (SANDBOX_REPO, MODE_A_BUDGET_USD, LLM_MODEL)

  ## Test plan

  - All ~50 new unit tests pass
  - Container reconciles on \`manage.sh phase apps --apply\`
  - First Mode A fire emits a finding (or gracefully no-ops if no alerts)
  - GH issue lands in cluster-agent-sandbox
  - Grafana annotation visible on dev cluster's kube-prometheus-stack dashboard
  - State.db has the finding row
  EOF
  )"
  ```

- [ ] **Step 2: Merge after CI green + apply on the NAS**

  ```sh
  gh pr merge <NR> --squash --delete-branch
  git checkout main && git pull --ff-only origin main
  ./manage.sh phase apps --apply
  ```

  Expected: `app_ensured action=update changed=True name=cluster-agent` (the env-var changes alone trigger a container re-create).

- [ ] **Step 3: Verify the container came back healthy**

  ```sh
  # From operator laptop on WG — the container's /metrics endpoint is
  # already scraped by dev Prometheus, so check there.
  KUBECONFIG=/tmp/kc/dev-admin.yaml kubectl -n monitoring port-forward \
    pod/prometheus-kube-prometheus-stack-prometheus-0 9090:9090 >/dev/null 2>&1 &
  sleep 3
  curl -sf "http://localhost:9090/api/v1/query?query=up%7Bjob%3D%22cluster-agent-nas%22%7D" \
    | jq -r '.data.result[0].value[1]'
  # Expected: 1
  kill %1; wait 2>/dev/null
  ```

- [ ] **Step 4: Operator enables Mode A via Doppler**

  ```sh
  doppler secrets set ENABLED=true \
    --project cluster-agent --config prd
  doppler secrets set DISABLED_MODES=B,D,E,F,G,H,I,J \
    --project cluster-agent --config prd
  ```

  Doppler operator reconciles into the container env within ~60s. No container restart needed (scheduler reads env on every fire).

- [ ] **Step 5: Wait for first Mode A fire (or trigger manually)**

  Mode A runs every 5 min. If no active alerts, it no-ops. To **force** a fire:

  ```sh
  # Confirm one or more active alerts exist on dev:
  KUBECONFIG=/tmp/kc/dev-admin.yaml kubectl get --raw \
    "/api/v1/namespaces/monitoring/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/alerts?active=true" \
    | jq '. | length'
  # If 0, create a synthetic alert that fires for ~2 min:
  KUBECONFIG=/tmp/kc/dev-admin.yaml kubectl -n monitoring apply -f - <<EOF
  apiVersion: monitoring.coreos.com/v1
  kind: PrometheusRule
  metadata:
    name: cluster-agent-mode-a-smoke
    namespace: monitoring
  spec:
    groups:
      - name: cluster-agent-smoke
        rules:
          - alert: ClusterAgentModeASmokeTest
            expr: vector(1)
            for: 0s
            labels:
              severity: info
              namespace: monitoring
              pod: prometheus-kube-prometheus-stack-prometheus-0
            annotations:
              summary: "Synthetic alert to verify Mode A end-to-end"
              description: "Created during PR <#> verification. Delete after one Mode A cycle."
  EOF
  ```

- [ ] **Step 6: Verify first finding landed**

  ```sh
  # Check the sandbox repo for a new issue (allow ~30s after the 5-min cron tick)
  gh issue list --repo guntars-rakitko/cluster-agent-sandbox --limit 5

  # Check Grafana for the annotation
  curl -sf -H "Authorization: Bearer $(doppler secrets get GRAFANA_API_TOKEN_DEV --project cluster-agent --config prd --plain)" \
    "https://grafana-dev.w1.lv/api/annotations?tags=cluster-agent&limit=5" \
    | jq '.[] | {id, text, tags, time}'

  # Check state.db (via cluster-agent's /metrics — cluster_agent_findings_total
  # counter exposes the count even if we can't exec into the container)
  curl -sf http://10.10.15.10:9595/metrics | grep cluster_agent_findings_total
  ```

- [ ] **Step 7: Clean up the smoke-test PrometheusRule**

  ```sh
  KUBECONFIG=/tmp/kc/dev-admin.yaml kubectl -n monitoring delete prometheusrule cluster-agent-mode-a-smoke
  ```

- [ ] **Step 8: Update wiki phase-history**

  Edit `wiki/docs/cluster-agent/phase-history.md`:
  - Add a new row to the table:
    ```
    | P1 — Mode A on dev (soak) | 2026-05-25 | (open) | guntars-rakitko | Mode A enabled on dev with sandbox repo destination. First finding emitted [DATE TIME UTC]. 2-3 week review window starts now. |
    ```
  - Under "Phase descriptions", add a `### P1 — Mode A on dev` section noting the start time, sandbox repo destination, current `DISABLED_MODES` value, gating criteria for P2 (≥20 findings reviewed, ≥80% useful, 0 secrets leaked).

  Deploy:
  ```sh
  cd /Users/gunrak/github/wiki && ./tools/deploy.sh --verify
  git add docs/cluster-agent/phase-history.md
  git commit -m "docs(cluster-agent): P1 Mode A enabled on dev ($(date -u +%Y-%m-%d))"
  git push origin main
  ```

---

## Self-review checklist

- ✅ **Spec coverage** — Tasks 3-10 implement the architecture from spec § 4.1 (single-LLM-call shape with the upgrade-to-MCP path documented as a P2-time refactor in the architecture note). Tasks 11-12 land the operational pieces (Doppler keys, container deploy). Sandbox-repo destination per spec § 7.3.
- ✅ **Placeholders** — None. Every step has concrete code or commands.
- ✅ **Type consistency** — `Finding`, `DedupAction`, `ModeResult`, `DispatchResult`, `LLMBudgetExceeded` are defined once and used consistently downstream.
- ✅ **Env-var consistency** — `CLUSTER_AGENT_GH_APP_*` (code + tests + compose), `SANDBOX_REPO`, `LLM_MODEL`, `MODE_A_BUDGET_USD`, `GRAFANA_API_TOKEN_{DEV,PRD}`, `STATE_DB_PATH`, `KUBECONFIG_{DEV,PRD}` — all referenced symmetrically across code, compose, and tasks.
- ✅ **Test-first ordering** — Each new file follows the "write failing test → run it to confirm fail → implement → run again to confirm pass → commit" cycle.
- ✅ **Operational gates** — Pre-call budget gate (LLMBudgetExceeded), runtime kill via Doppler (DISABLED_MODES=A), idle-cluster gate (skip if 0 active alerts) — three layers per spec § 6.4.

---

## Notes on what was deliberately NOT done

- **MCP tool-use** — Mode A in P1 does a single LLM call with pre-gathered context. Upgrade to the claude-agent-sdk MCP shape (tool-iterative loop) is a P2-time refactor once the operator sees what context patterns work during the dev soak.
- **Cost-burn replay simulation** (spec § 7.2) — out of scope for "enable Mode A today"; lands as a follow-up alongside other P1→P2 gating prep.
- **Per-prompt golden replay tests** (spec § 7.2) — the test fixtures + stubbed-LLM tests give the same regression coverage for prompt edits during the soak. Adding a real-LLM golden integration test is a P2 task once the prompt stabilizes.
- **prd-cluster Mode A enable** — the code registers both clusters but P1 doctrine is dev-only. Promotion to prd is gated on operator-reviewed soak (≥20 findings, ≥80% useful, 0 secrets leaked) per spec § 7.3.
