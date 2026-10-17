# cluster-agent in the clusters — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the cluster-agent as one Flux-deployed pod per cluster, built in CI from its own
repo, and delete everything it needed on the NAS.

**Architecture:**
- The code moves, with its history, from `truenas-infra/apps/cluster-agent` to a new private repo,
  `guntars-rakitko/cluster-agent`, which builds a cosign-signed `python:3.14-slim` image.
- kube-infra deploys it to each cluster in the sms-gateway pattern: the namespace and its Doppler
  plumbing in `infra-configs`, the app in `flux-cd/apps/cluster-agent`. msa2-dev gets it first,
  then msa2-prd gets the same digest.
- The NAS app, its token account and the NAS-only plumbing are deleted the day prd passes its
  checks.

**Tech Stack:**
- **Agent:** Python 3.14, FastAPI/uvicorn, APScheduler, httpx; tests with pytest and respx; uv.
- **Build:** Docker, GitHub Actions, cosign v2.
- **Cluster:** Flux, kustomize, Cilium, Kyverno 1.19, kube-prometheus-stack, the Doppler operator.
- **NAS:** TrueNAS `midclt`.

**Spec:** `docs/superpowers/specs/2026-10-17-cluster-agent-in-cluster-design.md`, approved
2026-10-17. Two corrections found while planning are already in it:
- § 4.4: the scrape needs no cross-namespace pair, because monitoring's rule 4 already reaches
  every pod;
- § 4.7: the rules switch per cluster rather than matching both job labels, and `ClusterAgentDown`
  gains `absent()`.

One refinement to § 4.3: the image is also built, without pushing, on pull requests (Task 8).

## Global Constraints

- **Behaviour is unchanged:**
  - model `claude-sonnet-4-6`;
  - `FINDINGS_REPO=guntars-rakitko/kube-infra`, `DIGEST_REPO=guntars-rakitko/cluster-agent-digest`;
  - schedule dev 06:00, prd 06:01 Europe/Riga;
  - the prompts, dedup and issue formats as they are.
- **Image:** `ghcr.io/guntars-rakitko/cluster-agent:<commit sha>` with its digest.
  - Base `python:3.14-slim`, pinned by digest.
  - kubectl `v1.36.4`, sha256 `8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a`.
  - User `10001:10001`, port `9595`.
- **Signing:** `sigstore/cosign-installer` stays at v3.10.1 (cosign v2, classic `.sig`), and
  Renovate caps it at `<4.0.0`.
- **Names:**
  - namespace and ServiceAccount: `cluster-agent`;
  - Prometheus job: `cluster-agent`;
  - Secrets: `cluster-agent-shared` (from shr) and `cluster-agent-env` (per cluster);
  - pull secret: `ghcr-readonly`.
- **Doppler keys:**
  - `infrastructure/shr`: `SHARED_CLUSTER_AGENT_ANTHROPIC_API_KEY`, `SHARED_CLUSTER_AGENT_GH_APP_ID`,
    `SHARED_CLUSTER_AGENT_GH_APP_INSTALLATION_ID`, `SHARED_CLUSTER_AGENT_GH_APP_PRIVATE_KEY`;
  - `infrastructure/{dev,prd}`: `CLUSTER_AGENT_GRAFANA_SA_TOKEN`;
  - SES comes from the existing `SHARED_SES_W1_*`.
- **The env names the code reads stay the same:** `ANTHROPIC_API_KEY`, `CLUSTER_AGENT_GH_APP_ID`,
  `CLUSTER_AGENT_GH_APP_INSTALLATION_ID`, `CLUSTER_AGENT_GH_APP_PRIVATE_KEY`,
  `GRAFANA_SA_TOKEN_<KEY>`, `SES_SMTP_HOST`, `SES_SMTP_PORT`, `SES_SMTP_USERNAME`,
  `SES_SMTP_PASSWORD`, `SES_FROM_DEFAULT`. New: `CLUSTER_KEY`.
- **Who does what on live systems:**
  - Subagents are READ-ONLY on clusters and appliances (workspace rule).
  - The main session does every live write (Doppler writes, `kubectl exec`, merges), each with
    the operator's OK.
  - The operator runs the irreversible deletions (the TrueNAS app, NAS directories, MinIO
    buckets, the Doppler project). This plan gives the commands.
- **Changes go through PRs.** Each repo's change is a PR, and its docs change in the same PR.
  Cross-repo PRs link each other. The only direct push is the new repo's first history push.
- **kube-infra:** PRs target `dev`; prd changes only through the house promotion to `main`. Never
  merge on a red check.
- **Shell:** the Bash tool runs zsh, where `=word` and `$var[...]` expand and broke commands this
  session. Put multi-line scripts in the scratchpad and run them with `bash`.

## Review Focus

1. **The agent pod disappears entirely** (Flux prunes the Deployment, or it is scaled to 0). An
   alert must still fire. Covered by Task 14's `absent()` clause, checked in Task 15.
2. **The ServiceAccount token rotates under a running process.** The next call must use the new
   token. Covered by Task 2's `test_proxy_rereads_the_token_on_every_call`; live check in Task 15.
3. **The agent's own audit lines quote tripwire words.** No digest may count them. Covered by
   Task 6's tests; live check in Task 15.
4. **The GitHub App PEM loses its trailing newline in the Doppler copy.** JWT signing then fails
   and findings are dropped. Covered by Task 11's checksum comparison; Task 15 mints a token from
   the pod.
5. **An overlay names the wrong cluster** (prd's overlay says `CLUSTER_KEY=dev`). prd's findings
   would be labelled `msa2-dev`. Covered by the render assertions in Task 13 and Task 16.

---

## Phase 0: the documents

### Task 0: Land the spec and this plan in truenas-infra

**Files:**
- `docs/superpowers/specs/2026-10-17-cluster-agent-in-cluster-design.md`
- `docs/superpowers/plans/2026-10-17-cluster-agent-in-cluster.md`

Both are on branch `docs/cluster-agent-in-cluster-spec`, worktree
`~/github/truenas-infra-wt-cluster-agent-in-cluster`.

- [ ] **Step 1:** Commit the plan, push the branch, and open a docs-only PR.
- [ ] **Step 2:** Merge it with the operator's OK.
- [ ] **Step 3:** Verify with `git -C ~/github/truenas-infra fetch -q && git -C ~/github/truenas-infra log -1 --oneline origin/main -- docs/superpowers/plans/2026-10-17-cluster-agent-in-cluster.md`.
  Expected: the merge commit. Task 1 takes both documents from `main`.

## Phase 1: the new repo

### Task 1: Create `guntars-rakitko/cluster-agent` with the code's history

**Files:**
- New repo root, made from the truenas-infra paths `apps/cluster-agent/` (renamed to the repo
  root) and these documents, at the same paths:
  - `docs/superpowers/specs/{2026-05-23-cluster-agent-design,2026-05-26-cluster-agent-p3-log-mining,2026-05-26-cluster-agent-roadmap-reshape,2026-07-06-cluster-agent-digest-graduation,2026-07-16-finding-state-reconcile,2026-10-17-cluster-agent-in-cluster-design}.md`;
  - `docs/superpowers/plans/{2026-05-23-cluster-agent-p0-foundation,2026-05-25-cluster-agent-p1-mode-a-enable,2026-10-17-cluster-agent-in-cluster}.md`.

- [ ] **Step 1:** In the scratchpad, clone truenas-infra fresh (`git clone https://github.com/guntars-rakitko/truenas-infra.git ca-history`).
  Run `git filter-repo` with one `--path` per path above and
  `--path-rename apps/cluster-agent/:`. Install it with `brew install git-filter-repo` if
  missing.
- [ ] **Step 2:** Verify the result:
  - `git log --oneline -- src/cluster_agent/llm.py | wc -l` is greater than 1;
  - `ls` shows `main.py src prompts tests docs pyproject.toml uv.lock` at the root;
  - `git log --reverse --format=%as | head -1` is in May 2026.
- [ ] **Step 3:** Baseline before any change: `uv run --extra dev pytest -q`. Expected: everything
  passes.
- [ ] **Step 4:** Create the repo and set its merge options:
  - `gh repo create guntars-rakitko/cluster-agent --private --description "Daily LLM digest of each homelab cluster's alerts, logs and changes"`;
  - `gh repo edit guntars-rakitko/cluster-agent --enable-squash-merge --enable-merge-commit=false --enable-rebase-merge=false --delete-branch-on-merge`.
- [ ] **Step 5:** `git remote add origin git@github.com:guntars-rakitko/cluster-agent.git && git push -u origin main`.
  This is the plan's one direct push. Clone it to `~/github/cluster-agent`, and work on branch
  `feat/in-cluster` from here on.

### Task 2: In-cluster API access, refusing the other cluster

**Files:**
- Modify: `src/cluster_agent/clusters.py`, `src/cluster_agent/tools/k8s_proxy.py`,
  `src/cluster_agent/tools/kubectl.py`
- Create: `tests/conftest.py`
- Test: `tests/test_clusters.py`, `tests/test_observability_tools.py`, `tests/test_kubectl.py`

**Interfaces:**
- Produces in `clusters.py`:
  - `class ClusterKeyError(RuntimeError)`;
  - `local_key() -> str`: returns `CLUSTER_KEY`, or raises `ClusterKeyError` when it is unset,
    empty, or not a key of `CLUSTER_NAMES`;
  - `assert_local(key: str) -> None`: raises `ClusterKeyError` unless `key == local_key()`.
- Produces in `k8s_proxy.py`:
  - `SA_DIR: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount")`;
  - `_apiserver_url() -> str`;
  - `_tls_verify() -> ssl.SSLContext`;
  - `_bearer_token() -> str`;
  - `_build_proxy_url(namespace, service, port, path) -> str`.

  `proxy_get(...)` and every `kubectl_*` keep their signatures.
- Produces in `tests/conftest.py`:
  - an autouse fixture `_agent_env` setting `CLUSTER_KEY=dev`,
    `ANTHROPIC_API_KEY=sk-ant-api03-test`, `FINDINGS_REPO=guntars-rakitko/kube-infra` and
    `DIGEST_REPO=guntars-rakitko/cluster-agent-digest`;
  - a fixture `in_cluster(tmp_path, monkeypatch) -> Path`. It writes `token` (`t1`) and `ca.crt`
    into a temp ServiceAccount directory, points `k8s_proxy.SA_DIR` at it, and sets
    `KUBERNETES_SERVICE_HOST=10.96.0.1` and `KUBERNETES_SERVICE_PORT=443`;
  - a helper `make_ca_pem(cn: str) -> bytes`, moved from the old CA test.

- [ ] **Step 1: Write the failing tests**
  ```python
  # tests/test_clusters.py
  def test_local_key_reads_cluster_key(monkeypatch):
      monkeypatch.setenv("CLUSTER_KEY", "prd")
      assert clusters.local_key() == "prd"

  @pytest.mark.parametrize("value", [None, "", "staging"])
  def test_local_key_refuses_a_missing_or_unknown_key(monkeypatch, value):
      ...  # unset or set CLUSTER_KEY to value
      with pytest.raises(clusters.ClusterKeyError, match="CLUSTER_KEY"):
          clusters.local_key()

  # tests/test_observability_tools.py
  def test_proxy_rereads_the_token_on_every_call(in_cluster): ...
      # two loki_query("dev", ...) calls, writing "t2" into in_cluster/"token" between them:
      # the captured Authorization headers == ["Bearer t1", "Bearer t2"]
  def test_proxy_addresses_the_apiserver_from_the_service_env(in_cluster): ...
      # request URL == "https://10.96.0.1:443/api/v1/namespaces/monitoring/services/loki:3100/proxy/loki/api/v1/query_range"
  def test_proxy_brackets_an_ipv6_service_host(in_cluster, monkeypatch): ...
      # KUBERNETES_SERVICE_HOST="fd00::1" -> k8s_proxy._apiserver_url() == "https://[fd00::1]:443"
  def test_proxy_trusts_the_serviceaccount_ca_from_memory(in_cluster, monkeypatch): ...
      # tempfile.NamedTemporaryFile patched to raise; _tls_verify() is an SSLContext with
      # CERT_REQUIRED, check_hostname, and exactly the CA "kubernetes-test-ca"
  def test_proxy_refuses_the_other_cluster(in_cluster): ...
      # pytest.raises(ClusterKeyError) on proxy_get("prd", ...); the respx route was never called

  # tests/test_kubectl.py
  def test_get_runs_kubectl_in_cluster_mode(monkeypatch): ...
      # argv == ["kubectl", "get", "pods", "-n", "kube-system", "-o", "json"]
  def test_describe_and_logs_carry_no_kubeconfig(monkeypatch): ...
      # "--kubeconfig" and "--context" in neither kubectl_describe's nor kubectl_logs's argv
  def test_kubectl_refuses_the_other_cluster(monkeypatch): ...
      # kubectl_get("prd", "pods", namespace="x") raises ClusterKeyError; subprocess.run never called
  ```
  Delete `_fake_kubeconfig`, `test_get_pods_passes_through`, `test_missing_kubeconfig_env_raises`
  and `test_the_cluster_ca_is_trusted_from_memory_never_a_temp_file`. Move the remaining
  observability tests to `in_cluster` and the `https://10.96.0.1:443` URLs.
- [ ] **Step 2:** `uv run --extra dev pytest tests/test_clusters.py tests/test_observability_tools.py tests/test_kubectl.py -q`.
  Expected: FAIL, with `ClusterKeyError` undefined.
- [ ] **Step 3:** Implement the interfaces.
  - `_apiserver_url()` builds `https://{host}:{port}`, bracketing a host that contains `:`.
  - `_tls_verify()` is `ssl.create_default_context(cadata=(SA_DIR / "ca.crt").read_text())`.
  - `_bearer_token()` reads `SA_DIR / "token"` on every call and strips it.
  - `proxy_get` and each kubectl tool call `clusters.assert_local(...)` first.
  - kubectl's argv starts `["kubectl", <verb>, ...]`.
  - Delete `_kubeconfig_for`, `_kubeconfig_path`, `_KUBECONFIG_PATHS` and their base64, yaml and
    tempfile imports.
  - Rewrite both module docstrings: the pod's ServiceAccount, rotated by the kubelet, so the token
    is never cached.
  - A test elsewhere that drives a real tool with `prd` sets `CLUSTER_KEY=prd` itself.
- [ ] **Step 4:** Run the three files, then the whole suite (`uv run --extra dev pytest -q`).
  Expected: PASS.
- [ ] **Step 5:** Commit: `feat: reach the cluster with the pod's ServiceAccount`.

### Task 3: One digest job per process

**Files:**
- Modify: `main.py`
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes `clusters.local_key()` (Task 2).
- Produces `main._required_env() -> None`, which raises one `RuntimeError` naming every missing
  setting. In this task it checks only `CLUSTER_KEY`, through `local_key()`.
- The lifespan builds a fresh `Scheduler()` (assigned to the module's `_scheduler`) and registers
  exactly the job `mode-A-<key>`.

- [ ] **Step 1: Write the failing tests**
  ```python
  def test_lifespan_registers_one_job_for_the_cluster_key(monkeypatch):
      monkeypatch.setenv("CLUSTER_KEY", "prd")
      monkeypatch.setenv("DAILY_DIGEST_HOUR", "6")
      monkeypatch.setenv("DAILY_DIGEST_MINUTE", "1")
      with TestClient(main.app):
          jobs = {j.id: j for j in main._scheduler._sched.get_jobs()}
          assert list(jobs) == ["mode-A-prd"]
          trigger = str(jobs["mode-A-prd"].trigger)
          assert "hour='6'" in trigger and "minute='1'" in trigger

  def test_startup_refuses_a_missing_cluster_key(monkeypatch):
      monkeypatch.delenv("CLUSTER_KEY")
      with pytest.raises(RuntimeError, match="CLUSTER_KEY"):
          with TestClient(main.app):
              pass
  ```
- [ ] **Step 2:** `uv run --extra dev pytest tests/test_main.py -q`. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - The lifespan calls `_required_env()`, then adds one `add_daily_digest` for `local_key()` at
    `int(DAILY_DIGEST_HOUR or 6):int(DAILY_DIGEST_MINUTE or 0)`.
  - Delete the `MODE_A_CLUSTERS` loop, the `KUBECONFIG_*` check and the `+ offset` minute
    arithmetic.
  - Rewrite the docstrings for a pod.
- [ ] **Step 4:** The suite passes.
- [ ] **Step 5:** Commit: `feat: one digest job per process, for CLUSTER_KEY`.

### Task 4: Remove the dead OAuth and `SANDBOX_REPO` paths

**Files:**
- Modify: `main.py`, `src/cluster_agent/llm.py`, `src/cluster_agent/dispatch.py`,
  `src/cluster_agent/modes/daily_digest.py`
- Create: `tests/test_dead_settings.py`
- Test: `tests/test_main.py`, `tests/test_llm.py`, `tests/test_dispatch.py`

**Interfaces:**
- `_required_env()` (Task 3) also requires `FINDINGS_REPO`, `DIGEST_REPO`, and
  `ANTHROPIC_API_KEY` whenever `ENABLED` is `true`.
- `dispatch._findings_repo() -> str | None` returns `FINDINGS_REPO` or `None`.
- `llm._sdk_query` sends only `x-api-key`.

- [ ] **Step 1: Write the failing tests**
  ```python
  # tests/test_main.py
  def test_startup_names_every_missing_setting(monkeypatch):
      monkeypatch.delenv("FINDINGS_REPO"); monkeypatch.delenv("DIGEST_REPO")
      with pytest.raises(RuntimeError) as e:
          with TestClient(main.app):
              pass
      assert "FINDINGS_REPO" in str(e.value) and "DIGEST_REPO" in str(e.value)

  @pytest.mark.parametrize("enabled,starts", [("true", False), ("false", True)])
  def test_startup_needs_an_api_key_only_when_enabled(monkeypatch, enabled, starts): ...
      # ANTHROPIC_API_KEY unset: RuntimeError matching "ANTHROPIC_API_KEY" iff not starts

  # tests/test_llm.py
  async def test_sdk_query_sends_the_api_key_as_x_api_key(monkeypatch): ...
      # headers["x-api-key"] == "sk-ant-api03-test"; no "Authorization", no "anthropic-beta"
  async def test_sdk_query_never_falls_back_to_an_oauth_token(monkeypatch): ...
      # ANTHROPIC_API_KEY unset, CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-x"
      # -> pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY")

  # tests/test_dispatch.py
  def test_findings_repo_has_no_fallback(monkeypatch): ...
      # FINDINGS_REPO unset, DIGEST_REPO and SANDBOX_REPO set -> d._findings_repo() is None

  # tests/test_dead_settings.py
  def test_no_code_reads_the_retired_settings():
      files = [*(ROOT / "src").rglob("*.py"), ROOT / "main.py"]
      for name in ("CLAUDE_CODE_OAUTH_TOKEN", "LLM_AUTH_MODE", "SANDBOX_REPO",
                   "MODE_A_CLUSTERS", "KUBECONFIG_"):
          assert not [p for p in files if name in p.read_text()], name
  ```
  - In `test_dispatch.py`, the four `SANDBOX_REPO` setenvs become
    `FINDINGS_REPO=guntars-rakitko/kube-infra`, and their expected refs become
    `guntars-rakitko/kube-infra#<n>`.
  - Delete the five `LLM_AUTH_MODE` tests in `test_main.py`.
- [ ] **Step 2:** Run the four files. Expected: FAIL.
- [ ] **Step 3:** Implement.
  - Delete `main.py`'s `_AUTH_MODE` block and its checks, and extend `_required_env()`.
  - In `llm.py`, delete `_OAUTH_BETA` and the `sk-ant-oat` branch. Missing `ANTHROPIC_API_KEY`
    raises `RuntimeError`. The docstring's Transport paragraph now says the API key bills the API
    account, and that the Agent SDK move is the second spec.
  - `dispatch._findings_repo()` reads only `FINDINGS_REPO`, and its warning names only that.
  - `daily_digest`'s summary repo is `os.environ["DIGEST_REPO"]`.
- [ ] **Step 4:** The suite passes.
- [ ] **Step 5:** Commit: `chore: drop the OAuth path and the SANDBOX_REPO fallback`.

### Task 5: Grafana by its in-cluster Service

**Files:**
- Modify: `src/cluster_agent/clusters.py`
- Test: `tests/test_clusters.py`, `tests/test_grafana.py`

- [ ] **Step 1: Write the failing test**
  ```python
  def test_the_grafana_map_names_the_in_cluster_service():
      url = "http://kube-prometheus-stack-grafana.monitoring.svc"
      assert clusters.GRAFANA_URLS == {"dev": url, "prd": url}
      assert set(clusters.GRAFANA_URLS) == set(clusters.CLUSTER_NAMES)
  ```
  Replace `test_the_grafana_map_names_each_keys_node_and_the_nodeport`. In `test_grafana.py`,
  the parametrized cases and respx routes use
  `http://kube-prometheus-stack-grafana.monitoring.svc/api/annotations`; the sentinel test stays.
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the map. The module docstring loses the node addresses, the NodePort
  and the NAS /24. The Service's port 80 maps to Grafana's container port 3000, which is where
  Task 12's network-policy pair opens.
- [ ] **Step 4:** The suite passes.
- [ ] **Step 5:** Commit: `feat: post annotations to Grafana's in-cluster Service`.

### Task 6: Log mining skips the agent's own namespace

**Files:**
- Modify: `src/cluster_agent/modes/digest_aggregator.py` (`_ALL_NAMESPACES`,
  `_POSTGRES_CONTAINERS`, and the `_RECORD_EXCLUDE` comment)
- Test: `tests/test_digest_aggregator.py`

**Interfaces:**
- Produces `AGENT_NAMESPACE = "cluster-agent"`.
- `_ALL_NAMESPACES == '{namespace=~".+", namespace!="cluster-agent"}'`.
- `_POSTGRES_CONTAINERS == '{namespace=~".+", namespace!="cluster-agent", container="postgres"}'`.

- [ ] **Step 1: Write the failing tests**
  ```python
  def test_mining_selectors_skip_the_agents_own_namespace():
      for sel in (da._ALL_NAMESPACES, da._POSTGRES_CONTAINERS):
          assert 'namespace=~".+"' in sel and 'namespace!="cluster-agent"' in sel

  def test_every_mining_query_skips_the_agents_own_namespace(): ...
      # drive aggregate_log_patterns("dev", ...) through the file's existing simulator,
      # recording every LogQL passed to loki_query_fn and loki_metric_query_fn.
      # Every recorded query containing 'namespace=~".+"' also contains
      # 'namespace!="cluster-agent"'; at least one such query was recorded (positive control).
  ```
- [ ] **Step 2:** Run. Expected: FAIL.
- [ ] **Step 3:** Implement the constants. The comment says why: in the cluster, the agent's
  audit lines quote its own LogQL, the same trap as Loki's query log.
- [ ] **Step 4:** The suite passes.
- [ ] **Step 5:** Commit: `fix: never mine the agent's own logs`.

### Task 7: The image

**Files:**
- Create: `Dockerfile`, `.dockerignore`
- Delete: `docker-compose.yaml`
- Test: `tests/test_requirements_lock.py`

**Interfaces:** Produces the Dockerfile that Task 8's CI builds:
- both stages `FROM python:3.14-slim@sha256:<digest>`;
- `ARG KUBECTL_VERSION=v1.36.4` and `ARG KUBECTL_SHA256=8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a`;
- exactly one `pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock.txt`
  into `/venv`;
- `COPY` of `main.py`, `src/` and `prompts/` into `/app`;
- `ENV PYTHONPATH=/app/src PATH=/venv/bin:$PATH`;
- `USER 10001:10001`;
- `CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9595"]`.

- [ ] **Step 1: Write the failing tests** in `test_requirements_lock.py`:
  - `_container_python()` reads `(3, 14)` from the Dockerfile's `FROM python:3.14-slim@sha256:`
    line, and `DOCKERFILE` replaces `COMPOSE`.
  - `test_every_runtime_package_has_a_wheel_for_the_container` accepts:
    - `cp314` or `abi3` wheels tagged `manylinux_2_N_x86_64` with N ≤ 41 (trixie's glibc is 2.41);
    - `manylinux2014_x86_64`;
    - pure-Python wheels.
  - `test_dockerfile_installs_exactly_the_requirements_file`: exactly one `pip install`, carrying
    `--require-hashes`, `--only-binary=:all:` and `-r requirements.lock.txt`. No `==` anywhere in
    the Dockerfile.
  - `test_dockerfile_pins_kubectl_by_checksum`: `ARG KUBECTL_VERSION=v1.36.4`;
    `ARG KUBECTL_SHA256=` followed by 64 hex characters; and a `sha256sum -c` line.
  - `test_dockerfile_runs_as_the_unprivileged_user`: the last `USER` line is `USER 10001:10001`.
  - Delete `test_compose_installs_exactly_the_requirements_file`.
- [ ] **Step 2:** Run. Expected: FAIL (no Dockerfile).
- [ ] **Step 3:** Write the Dockerfile, `.dockerignore` (`tests/`, `docs/`, `.git`,
  `__pycache__`, `.pytest_cache`, `.venv`) and delete the compose file.
  - Base digest: `docker buildx imagetools inspect python:3.14-slim --format '{{json .Manifest.Digest}}'`.
  - The build stage fetches kubectl with Python's `urllib`, because slim has no curl, and checks
    it with `echo "${KUBECTL_SHA256}  kubectl" | sha256sum -c`.
- [ ] **Step 4:** The suite passes. If Docker runs locally, also check:
  - `docker build -t cluster-agent:local .`;
  - `docker run --rm cluster-agent:local python -c "import cluster_agent, fastapi, uvicorn, jinja2"` succeeds;
  - `docker run --rm --entrypoint kubectl cluster-agent:local version --client` prints
    `Client Version: v1.36.4`;
  - `docker run --rm --entrypoint id cluster-agent:local -u` prints `10001`.

  Without local Docker, Task 8's PR build is this check.
- [ ] **Step 5:** Commit: `feat: build the agent as an image`.

### Task 8: CI, Renovate and branch protection

**Files:**
- Create: `.github/workflows/ci.yml`, `renovate.json`

- [ ] **Step 1:** Write `ci.yml` on `~/github/sms-gateway/.github/workflows/ci.yml`'s shape,
  copying its action SHAs.
  - **`test`** (pull requests and pushes to `main`): uv, pinned; `uv sync --frozen --extra dev`;
    `uv run --extra dev pytest -q`. pytest exits 5 when it collects nothing, so an empty run fails.
  - **`image`** (`needs: test`; the permissions `contents: read`, `packages: write` and
    `id-token: write` on this job only):
    - On a PR: build, load, and run the Task 7 smoke checks. No push.
    - On `main`: build and push `ghcr.io/guntars-rakitko/cluster-agent:${{ github.sha }}`, then
      `cosign sign --yes <image>@<digest>`.
    - Then `cosign verify --certificate-identity-regexp '^https://github\.com/guntars-rakitko/cluster-agent/\.github/workflows/ci\.yml@refs/heads/main$' --certificate-oidc-issuer https://token.actions.githubusercontent.com <image>@<digest>`.
    - Finally a job summary giving `newTag` and `digest`.
- [ ] **Step 2:** Write `renovate.json` on sms-gateway's:
  - the uv lock and the Dockerfile base digest;
  - github-actions, with `sigstore/cosign-installer` at `allowedVersions: "<4.0.0"`;
  - a regex custom manager for `ARG KUBECTL_VERSION` (datasource `github-releases`, depName
    `kubernetes/kubernetes`), with a description saying the checksum ARG is updated by hand in
    the same PR;
  - the `pip_requirements` manager disabled for `requirements.lock.txt`.
- [ ] **Step 3:** Push the branch and open the PR. Both jobs must be green, and the `image` log
  must show no push.
- [ ] **Step 4:** Protect `main`: required checks `test` and `image`, no force pushes. Use
  `gh api -X PUT repos/guntars-rakitko/cluster-agent/branches/main/protection --input <scratchpad json>`.
- [ ] **Step 5:** Commit: `ci: test, build, sign and verify the image`.

### Task 9: The repo's own docs

**Files:**
- Create: `CLAUDE.md`
- Modify: `README.md`, `docs/superpowers/specs/2026-05-23-cluster-agent-design.md` (a note under
  § 3.1, § 3.2 and § 3.6 pointing to the in-cluster spec)

- [ ] **Step 1:** Write `CLAUDE.md`, the current state, covering spec § 7's list:
  - what the agent is and how it runs;
  - deploying a change: merge, pin the digest in kube-infra dev, promote the same digest;
  - logs (`{namespace="cluster-agent"}`), metrics (`job="cluster-agent"`) and the four alerts;
  - the switches, and settings versus secrets;
  - a manual fire through `kubectl exec`, which is the main session's alone;
  - reading `state.db` with `python -c "import sqlite3 …"`.

  Rewrite `README.md` to match.
- [ ] **Step 2:** `grep -n -E 'docker-compose|manage\.sh|KUBECONFIG_|LLM_AUTH_MODE|docker exec' CLAUDE.md README.md`
  prints only lines that are marked as history.
- [ ] **Step 3:** Commit: `docs: CLAUDE.md and README for the in-cluster agent`.

### Task 10: Merge, and record the first image

- [ ] **Step 1:** With the PR green and the operator's OK, squash-merge.
- [ ] **Step 2:** From the `main` run's summary, record `newTag` (the merge commit's sha) and
  `digest`. Task 13 pins them.
- [ ] **Step 3:** If cosign is installed locally, run Task 8's `cosign verify` against
  `ghcr.io/guntars-rakitko/cluster-agent@<digest>`. Expected: `Verified OK`. Otherwise the CI
  verify step is the evidence.

## Phase 2: msa2-dev

### Task 11: Doppler keys for the new layout (live, main session)

- [ ] **Step 1:** Write a bash script in the scratchpad and run it with `bash`. It copies five
  values from `cluster-agent/prd` without printing them:

  | From `cluster-agent/prd` | To |
  |---|---|
  | `ANTHROPIC_API_KEY` | `shr` `SHARED_CLUSTER_AGENT_ANTHROPIC_API_KEY` |
  | `GH_APP_ID` | `shr` `SHARED_CLUSTER_AGENT_GH_APP_ID` |
  | `GH_APP_INSTALLATION_ID` | `shr` `SHARED_CLUSTER_AGENT_GH_APP_INSTALLATION_ID` |
  | `GH_APP_PRIVATE_KEY` | `shr` `SHARED_CLUSTER_AGENT_GH_APP_PRIVATE_KEY` |
  | `GRAFANA_SA_TOKEN_DEV` | `dev` `CLUSTER_AGENT_GRAFANA_SA_TOKEN` |

  Each copy is `doppler secrets get SRC --project cluster-agent --config prd --plain | doppler secrets set DST --project infrastructure --config CFG --silent`.
  Without `--silent`, the CLI prints the value.
- [ ] **Step 2:** Compare
  `doppler secrets get … --plain | shasum -a 256` of each source with its copy.
  - Stdin drops one trailing newline, so a PEM that differs only by that is re-set with
    `{ doppler secrets get SRC … --plain; echo; } | doppler secrets set DST … --silent` and
    compared again.
  - Expected: five matching pairs.

### Task 12: kube-infra `infra-configs` for msa2-dev

Worktree `~/github/kube-infra-wt-cluster-agent`, branch `feat/cluster-agent-in-cluster`, off
`origin/dev`. `P` = `flux-cd/infrastructure/configs/per-cluster/msa2-dev`.

**Files:**
- Create: `P/cluster-agent-namespace.yaml` (model: `P/sms-gateway-namespace.yaml`;
  `app.kubernetes.io/part-of: cluster-agent`, `env: dev`).
- Modify: `P/doppler-rbac-scoped.yaml`:
  - a `doppler-operator-secret-writer` Role and RoleBinding in `cluster-agent`, copying
    sms-gateway's block, with its `kubectl auth can-i` positive-control comment;
  - the header's namespace list gains `cluster-agent` (it writes `cluster-agent-shared`,
    `cluster-agent-env` and `ghcr-readonly`).
- Create: `P/secrets/ghcr-readonly-cluster-agent-dev.yaml` (model:
  `ghcr-readonly-web-tracker-dev.yaml`, namespace `cluster-agent`).
- Create: `P/secrets/cluster-agent-dev.yaml`, with two DopplerSecrets in `doppler-operator-system`:
  - **`cluster-agent-shr`**: token `doppler-token-infra-shr`, config `shr`.
    - Keys: the four `SHARED_CLUSTER_AGENT_*`, plus `SHARED_SES_W1_SMTP_HOST`,
      `SHARED_SES_W1_SMTP_PORT`, `SHARED_SES_W1_SMTP_USERNAME`, `SHARED_SES_W1_SMTP_PASSWORD` and
      `SHARED_SES_W1_FROM_DEFAULT`.
    - `processors.asName` gives the Global Constraints' env names.
    - `managedSecret` is `cluster-agent-shared` in `cluster-agent`.
  - **`cluster-agent-env`**: token `doppler-token-infra-dev`, config `dev`.
    - Key `CLUSTER_AGENT_GRAFANA_SA_TOKEN`, renamed to `GRAFANA_SA_TOKEN_DEV`.
    - `managedSecret` is `cluster-agent-env` in `cluster-agent`.
- Modify: `P/secrets/kustomization.yaml` (both files).
- Create: `P/kyverno-policy-verify-cluster-agent.yaml`, from `kyverno-policy-verify-sms-gateway.yaml`:
  - ClusterPolicy `verify-cluster-agent-images`, Enforce;
  - `imageReferences: ["ghcr.io/guntars-rakitko/cluster-agent*"]`;
  - `subjectRegExp: "^https://github\\.com/guntars-rakitko/cluster-agent/\\.github/workflows/ci\\.yml@refs/heads/main$"`.
- Create: `P/kyverno-imagevalidatingpolicy-verify-cluster-agent-images.yaml`: IVP
  `verify-cluster-agent-images-ivpol`, Audit, matchCondition
  `startsWith('ghcr.io/guntars-rakitko/cluster-agent')`.
- Create: `P/ciliumclusterwidenp-cluster-agent.yaml`, two CCNPs:
  - `cluster-agent-to-grafana`: egress from namespace `cluster-agent` to
    `{io.kubernetes.pod.namespace: monitoring, app.kubernetes.io/name: grafana}` on TCP 3000;
  - `grafana-from-cluster-agent`: ingress to those pods from namespace `cluster-agent` on TCP 3000.
- Modify: `P/kustomization.yaml`: the namespace, the CCNP and both Kyverno files, with one-line
  comments like sms-gateway's.
- Modify: `flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml`:
  - add the subject `{kind: ServiceAccount, name: cluster-agent, namespace: cluster-agent}` to
    ClusterRoleBinding `cluster-agent-readonly` and RoleBinding
    `monitoring/cluster-agent-services-proxy`, keeping the old subject;
  - the header says both subjects stay until the NAS retirement.
- Modify: `P/renovate.yaml` (its `repositories` list gains `'guntars-rakitko/cluster-agent'`) and
  the repository-access comment in `P/secrets/renovate-credentials.yaml`.
- **Operator:** widen the fine-grained PAT `kube-infra-renovate-pat` to the new repo, at
  github.com/settings/personal-access-tokens.

- [ ] **Step 1:** Write the files.
- [ ] **Step 2:** `kustomize build flux-cd/infrastructure/configs/per-cluster/msa2-dev >/dev/null && kustomize build flux-cd/infrastructure/configs/base >/dev/null`.
  Expected: exit 0.
- [ ] **Step 3:** `python3 scripts/ci/check-bootstrap-order.py --repo . --cluster msa2-dev` and
  `python3 scripts/ci/check-cluster-invariants.py --repo .`. Expected: exit 0. Without the pinned
  tools locally, the PR's `validate` run is the gate.
- [ ] **Step 4:** Commit: `feat(cluster-agent): namespace, secrets, RBAC and policies on msa2-dev`.

### Task 13: The app, on msa2-dev

**Files:**
- Create: `flux-cd/apps/cluster-agent/base/{kustomization,serviceaccount,deployment,pvc,podmonitor,ciliumnetworkpolicy}.yaml`,
  `flux-cd/apps/cluster-agent/dev/kustomization.yaml`
- Modify: `flux-cd/clusters/msa2-dev/apps.yaml`

Values the files pin:
- **ServiceAccount** `cluster-agent`, `automountServiceAccountToken: true`.
- **Deployment** `cluster-agent`:
  - Labels `app.kubernetes.io/name: cluster-agent` and `app.kubernetes.io/part-of: cluster-agent`.
    Annotation `secrets.doppler.com/reload: 'true'`.
  - `replicas: 1`, `strategy: Recreate`, `serviceAccountName: cluster-agent`,
    `imagePullSecrets: [ghcr-readonly]`.
  - Pod: `runAsNonRoot`, user, group and `fsGroup` 10001, seccomp `RuntimeDefault`.
  - Container `cluster-agent`: port `http` 9595, `readOnlyRootFilesystem: true`,
    `allowPrivilegeEscalation: false`, capabilities `drop: [ALL]`.
  - Resources: requests 50m CPU and 256Mi; limits 1 CPU and 1Gi.
  - Readiness and liveness: `GET /health` on `http`. Liveness `periodSeconds: 30`,
    `failureThreshold: 5`.
  - Env, in the base, from spec § 4.5:

    | Variable | Value |
    |---|---|
    | `LLM_MODEL` | `claude-sonnet-4-6` |
    | `DAILY_DIGEST_HOUR` | `6` |
    | `DAILY_DIGEST_MINUTE` | `0` |
    | `DAILY_DIGEST_WINDOW_HOURS` | `24` |
    | `DAILY_DIGEST_BUDGET_USD` | `0.75` |
    | `DIGEST_REPO` | `guntars-rakitko/cluster-agent-digest` |
    | `FINDINGS_REPO` | `guntars-rakitko/kube-infra` |
    | `FINDINGS_MIN_SEVERITY` | empty |
    | `DIGEST_SUMMARY` | `email,issue` |
    | `DIGEST_SUMMARY_EMAIL_TO` | today's value of the Doppler key of the same name (kube-infra is private; this public plan doesn't repeat it) |
    | `ENABLED` | `true` |
    | `DISABLED_MODES` | empty |
    | `STATE_DB_PATH` | `/data/state.db` |
    | `LOG_LEVEL` | `INFO` |
    | `HOME` | `/tmp` |
    | `KUBECACHEDIR` | `/tmp/kube-cache` |

  - `envFrom` the Secrets `cluster-agent-shared` and `cluster-agent-env`.
  - Volumes: `data` (PVC `cluster-agent-data`) at `/data`, and `tmp` (`emptyDir`) at `/tmp`.
- **PVC** `cluster-agent-data`: 1Gi, `local-path`, RWO.
- **PodMonitor** `cluster-agent`: selector `app.kubernetes.io/name: cluster-agent`; endpoint
  `port: http`, `path: /metrics`, `interval: 60s`, `scrapeTimeout: 15s`;
  `relabelings: [{targetLabel: job, replacement: cluster-agent}]`. Prometheus selects every
  PodMonitor (`podMonitorSelector: {}`).
- **CNPs** in namespace `cluster-agent`:
  - `default-deny` and `allow-dns-egress` (sms-gateway's);
  - `allow-egress-to-kube-apiserver` (monitoring rule 3's shape, TCP 6443);
  - `allow-egress-world` (`toEntities: [world]`, TCP 443 and 587);
  - `allow-prometheus-ingress`: from
    `{io.kubernetes.pod.namespace: monitoring, app.kubernetes.io/name: prometheus}` on TCP 9595.
- **dev overlay:** `images:` pins Task 10's `newTag` and `digest`, under a comment block like
  sms-gateway's (run id, PR). A patch sets `CLUSTER_KEY=dev`.
- **`apps.yaml`:** a `cluster-agent` Kustomization copying sms-gateway's block, with
  `path: ./flux-cd/apps/cluster-agent/dev` and `dependsOn: [infra-configs]`.

- [ ] **Step 1:** Write the files.
- [ ] **Step 2:** A scratchpad Python script reads `kustomize build flux-cd/apps/cluster-agent/dev`
  and asserts:
  - the Deployment's image is `ghcr.io/guntars-rakitko/cluster-agent:<newTag>@<digest>`;
  - `CLUSTER_KEY` is `dev` and `DAILY_DIGEST_MINUTE` is `0`;
  - the PodMonitor relabel sets `job` to `cluster-agent`;
  - every CNP other than `default-deny` has non-empty rules.

  Expected: all pass.
- [ ] **Step 3:** `yamllint` with kube-infra's config passes on the new files.
- [ ] **Step 4:** Commit: `feat(cluster-agent): the app on msa2-dev`.

### Task 14: Monitoring, and kube-infra's docs for the move

**Files:**
- Modify: `flux-cd/infrastructure/configs/base/prometheus-rules-cluster-agent.yaml`,
  `flux-cd/infrastructure/configs/base/grafana-dashboards/cluster-agent.yaml`, `CLAUDE.md`,
  `docs/ARCHITECTURE-SNAPSHOT.md`

- [ ] **Step 1: Rules.**
  - Every `job="cluster-agent-nas"` becomes `job="cluster-agent"`. Never a regex over both: spec
    § 4.7.
  - `ClusterAgentDown`: `up{job="cluster-agent"} == 0 or absent(up{job="cluster-agent"})`. Its
    summary reads correctly without `$labels.instance`.
  - Descriptions:
    - the cadence becomes "one run a day on this cluster (dev 06:00, prd 06:01 Europe/Riga)";
    - `ClusterAgentRunsFailing` says "1 = today's run failed";
    - the recipes become `kubectl -n cluster-agent get pods`,
      `kubectl -n cluster-agent logs deploy/cluster-agent --since=26h | tail -60` and Loki
      `{namespace="cluster-agent"}`;
    - the token-expiry text becomes "the pod's token is rotated by the kubelet; a 401 means a
      binding lost its subject";
    - `ClusterAgentDispatchErrors`'s Grafana text names the in-cluster Service, the CCNP
      `grafana-from-cluster-agent` and Doppler `infrastructure/<env>`
      `CLUSTER_AGENT_GRAFANA_SA_TOKEN`.
  - The header describes one agent per cluster, the PodMonitor relabel, and why the rules don't
    match both labels.
- [ ] **Step 2: Dashboard.** The uptime panel selects `job="cluster-agent"`, and the header prose
  follows.
- [ ] **Step 3: kube-infra `CLAUDE.md`.**
  - A new cluster-agent section: namespace, roles, policy pairs, alerts, pins.
  - § SSO architecture's Grafana client list gains the in-cluster agent. The NAS path stays listed
    until Task 19.
  - § Post-bootstrap operator tasks: the Grafana token recipe writes `infrastructure/<env>`
    `CLUSTER_AGENT_GRAFANA_SA_TOKEN`, and the token's name becomes `cluster-agent-<ts>`.
  - The cross-repo cosign rule names its fourth repo.
  - § "Kyverno is held on 1.19.x" gains `verify-cluster-agent-images`.
  - Related Repositories gains `cluster-agent`.

  `docs/ARCHITECTURE-SNAPSHOT.md`: the agent's placement.
- [ ] **Step 4:** Verify:
  - `kustomize build flux-cd/infrastructure/configs/base >/dev/null`;
  - `python3 scripts/ci/check-alert-runbook-urls.py` exits 0;
  - `git grep -n 'cluster-agent-nas' -- flux-cd/infrastructure/configs/base/prometheus-rules-cluster-agent.yaml flux-cd/infrastructure/configs/base/grafana-dashboards/cluster-agent.yaml`
    prints nothing.
- [ ] **Step 5:** Commit: `feat(cluster-agent): alerts and dashboard for the in-cluster agent`.

### Task 15: Cut msa2-dev over (live, main session)

- [ ] **Step 1:** Open the kube-infra PR (Tasks 12–14) to `dev`, linking the cluster-agent PR
  and this plan. With `validate` green and the operator's OK, merge.
- [ ] **Step 2:** Read-only, on msa2-dev:
  - `flux get kustomizations -n flux-system | grep -E 'infra-configs|cluster-agent'` shows
    Ready True;
  - `kubectl -n cluster-agent get pods` shows `1/1 Running`.
- [ ] **Step 3:** Take dev off the NAS: set Doppler `cluster-agent/prd` `MODE_A_CLUSTERS=prd`,
  then `cd ~/github/truenas-infra && ./manage.sh phase apps --only cluster-agent --apply`.
  Confirm that `process_start_time_seconds` at `http://10.10.10.10:9595/metrics` changed, and that
  `/health` lists only prd's job.
- [ ] **Step 4:** Read-only checks:
  - `kubectl get clusterpolicy verify-cluster-agent-images -o jsonpath='{.spec.validationFailureAction}'`
    prints `Enforce`, and the pod runs the pinned digest.
  - `kubectl auth can-i list pods -A --as=system:serviceaccount:cluster-agent:cluster-agent`
    answers `yes`. The same with `get secrets -n cluster-agent` and with
    `create pods -n cluster-agent` answers `no`.
  - In dev's Prometheus, `up{job="cluster-agent"} == 1` holds, and
    `cluster_agent_dispatch_errors_total{job="cluster-agent"}` exists: the positive control for
    the job label, since the code starts every surface's counter at 0. (Not
    `cluster_agent_run_total`: a labelled counter has no series before its first increment, so it
    appears only with the first scheduled run, Step 7.)
  - `kubectl -n monitoring get prometheusrule cluster-agent-alerts -o yaml | grep -c 'absent(up{job="cluster-agent"})'`
    prints `1`, and Prometheus evaluates `absent(up{job="cluster-agent-x"})` to `1` (Review
    Focus 1).
- [ ] **Step 5:** From the pod, through `kubectl -n cluster-agent exec deploy/cluster-agent -- python -c …`.
  Exec is a cluster write and the main session's alone.
  - `from cluster_agent.tools.github import gh_get_installation_token as t; t(); print("ok")`
    prints `ok`. The audit log redacts the token (Review Focus 4).
  - `import httpx; print(httpx.get("http://kube-prometheus-stack-grafana.monitoring.svc/api/health").status_code)`
    prints `200`.
  - `import smtplib, os; s = smtplib.SMTP(os.environ["SES_SMTP_HOST"], int(os.environ["SES_SMTP_PORT"]), timeout=10); s.starttls(); print(s.noop()[0])`
    prints `250`. No login, no mail.
- [ ] **Step 6:** A manual digest run:
  `… python -c "import asyncio; from cluster_agent.modes.daily_digest import run_async; print(asyncio.run(run_async(cluster='dev')))"`.
  Expected: the printed `DigestResult` has `error=None` (its first step checked
  `flux-system/cluster-env` against the key), and a digest-summary issue labelled `msa2-dev` is
  in `cluster-agent-digest`. ⚠ A manual run is a second process: its log lines go to the exec
  session, never to `kubectl logs` or Loki, and its counters never reach `/metrics`. So read the
  printed result, and leave the logs, Loki and the metrics to the scheduled run (Step 7).
  (Corrected at the final review, 2026-10-17: this step read "no error in `kubectl logs`", which
  a failed manual run also passes.)
- [ ] **Step 7:** After the scheduled 06:00 run:
  - `cluster_agent_run_total{job="cluster-agent",status=~"success|success_quiet"}` exists;
  - Loki `{namespace="cluster-agent"} |= "agent_run_id"` returns that run's audit lines, and some
    of them contain `panic` (the positive control);
  - its summary lists no log pattern in `cluster-agent` (Review Focus 3).
- [ ] **Step 8:** Token rotation (Review Focus 2). kube-apiserver accepts a kubelet token for a year
  past its nominal hour (`--service-account-extend-token-expiration`, the default, which Talos
  leaves on), so "a run after an hour succeeds" would pass with a cached token too. The check that
  can fail: once the pod has run for more than an hour and a run has happened since, Loki
  `{pod=~"kube-apiserver-.*"} |= "system:serviceaccount:cluster-agent:cluster-agent" |= "stale-token"`
  returns nothing. The same query without `|= "stale-token"` is the positive control: the audit
  policy's catch-all Metadata rule logs this ServiceAccount's requests, with their annotations.
  Record the evidence in the PR. (Corrected at the final review, 2026-10-17.)

### Task 16: msa2-prd — its twins, the promotion, the checks

- [ ] **Step 1:** On a kube-infra branch off `dev`, add:
  - the `per-cluster/msa2-prd` twins of Task 12: namespace with `env: prd`, the Doppler RBAC
    pair, `secrets/ghcr-readonly-cluster-agent-prd.yaml`, and `secrets/cluster-agent-prd.yaml`
    (token `doppler-token-infra-prd`, config `prd`, `CLUSTER_AGENT_GRAFANA_SA_TOKEN` renamed to
    `GRAFANA_SA_TOKEN_PRD`);
  - the Kyverno ClusterPolicy only (the IVP twin is dev-only), the CCNP pair and the
    kustomization entries;
  - `flux-cd/apps/cluster-agent/prd/kustomization.yaml`, with dev's exact `newTag` and `digest`,
    `CLUSTER_KEY=prd` and `DAILY_DIGEST_MINUTE=1`;
  - the `cluster-agent` Kustomization in `clusters/msa2-prd/apps.yaml`.
- [ ] **Step 2:** Doppler: `infrastructure/prd` `CLUSTER_AGENT_GRAFANA_SA_TOKEN` from
  `GRAFANA_SA_TOKEN_PRD`, with Task 11's hash comparison.
- [ ] **Step 3:** Render `kustomize build flux-cd/apps/cluster-agent/prd` and assert:
  `CLUSTER_KEY` is `prd`, `DAILY_DIGEST_MINUTE` is `1`, and the image@digest equals dev's render
  (Review Focus 5). `validate` is green on the PR.
- [ ] **Step 4:** With the operator's OK, merge to `dev`. Then run the house promotion to `main`:
  wait for the promote snapshot's own release-coherence check, then merge with the operator's OK.
- [ ] **Step 5:** At the promotion merge, set `MODE_A_CLUSTERS` to empty in Doppler
  `cluster-agent/prd` and apply it as in Task 15 step 3. Confirm the restart.
- [ ] **Step 6:** Run Task 15 steps 2 and 4 to 8 on msa2-prd (label `msa2-prd`, minute 1).

## Phase 3: retirement, the same day as Task 16

### Task 17: Delete the NAS side (operator runs the deletions; main session verifies)

- [ ] **Step 1 (operator):** `ssh truenas_admin@10.10.5.10 'sudo midclt call -job app.delete cluster-agent'`.
  Main session: `sudo midclt call app.query '[["name","=","cluster-agent"]]'` prints `[]`.
- [ ] **Step 2 (main session):** `sudo midclt call filesystem.stat /mnt/.ix-apps/app_configs/cluster-agent`
  must fail, meaning the directory is gone. ⚠ If it still exists, the operator deletes it before
  anything else: it holds the rendered compose with every secret.
- [ ] **Step 3 (operator):** `ssh -t truenas_admin@10.10.5.10 'sudo rm -rf /mnt/tank/system/apps-config/cluster-agent'`.
  Main session: `filesystem.stat` on that path fails.
- [ ] **Step 4:** Main session: `mc ls nas-prd/cluster-agent` and `mc ls nas-dev/cluster-agent`
  print nothing. Operator: `mc rb nas-prd/cluster-agent && mc rb nas-dev/cluster-agent`.

### Task 18: The truenas-infra retirement PR

Worktree `~/github/truenas-infra-wt-cluster-agent-retire`, off `origin/main`.

**Files:**
- Delete: `apps/cluster-agent/` and the nine moved documents (their history lives in the new
  repo); `scripts/render-cluster-agent-kubeconfigs.sh`;
  `tests/test_render_cluster_agent_kubeconfigs.py`.
- Modify:
  - `config/apps.yaml`: drop the entry;
  - `src/truenas_infra/modules/apps.py`: drop the `_DOPPLER_KEYS_PER_APP` and
    `_DOPPLER_PROJECT_PER_APP` entries, the `CLUSTER_AGENT_*` constants, the upload dispatch,
    `_requirement_pins`, `_verify_cluster_agent_requirements_export`,
    `_ensure_cluster_agent_config_via_ctx` and `_git_ls_files`;
  - `tests/test_apps.py` and `tests/test_verify.py`: the `cluster-agent` app in their fixtures
    and mocks;
  - `renovate.json`: drop the `requirements.lock.txt` rule;
  - `scripts/setup-minio-{buckets,encryption,lifecycle}.sh`: drop the bucket;
    `tests/test_minio_setup_scripts.py` adds `"cluster-agent"` to `RETIRED_BUCKETS`;
  - `CLAUDE.md`: the sections in spec § 6;
  - `README.md`, `manage.sh`'s uv comment, and `scripts/setup-talos-shutdown-orchestrator.sh`'s
    prose.

- [ ] **Step 1:** Make the changes.
- [ ] **Step 2:** `uv run pytest -q` at the repo root passes.
- [ ] **Step 3:** `./manage.sh phase apps`, a dry run, names no cluster-agent and fails on nothing.
- [ ] **Step 4:** `git grep -n -i 'cluster-agent' -- . ':!docs/superpowers'` prints only the
  pointer and history lines.
- [ ] **Step 5:** Close the four Renovate PRs on `renovate/{prometheus_client-0.x,pydantic-2.x,tenacity-9.x,python-3.x}`
  with the comment "moved to guntars-rakitko/cluster-agent".
- [ ] **Step 6:** Open the PR, linking the others. Merge with the operator's OK.

### Task 19: The kube-infra retirement PR, and proof that the old tokens are dead

**Files:**
- `base/cluster-agent-rbac.yaml`: drop the `flux-system` ServiceAccount and its subject in both
  bindings, and rewrite the header.
- `base/grafana-nas-nodeport.yaml`: delete it, with its `base/kustomization.yaml` entry.
- `base/ciliumnetworkpolicy-monitoring.yaml`: drop rules 18 and 18d. Rule 18c's prose, the
  header and rule 21's comment lose the NAS.
- `helmreleases/kube-prometheus-stack.yaml`: drop the `cluster-agent-nas` scrape job.
- `clusters/msa2-{dev,prd}/infrastructure.yaml`: drop `CLUSTER_NAS_DATA_IP`.
- The comment in `per-cluster/msa2-prd/ciliumnetworkpolicy-giks-v1-box.yaml`.
- `CLAUDE.md`:
  - the SSO list loses the NAS path;
  - the NAS container list and the MinIO bucket list lose the agent;
  - the old token section goes.
- Re-point the links to the moved design spec, in the RBAC header, the kustomization comment and
  the dashboard header, to
  `https://github.com/guntars-rakitko/cluster-agent/blob/main/docs/superpowers/specs/2026-05-23-cluster-agent-design.md`.

- [ ] **Step 1:** Make the changes. `validate` must be green. With the operator's OK, merge to
  `dev`.
- [ ] **Step 2:** Once Flux has applied it on msa2-dev, decode `KUBECONFIG_DEV` from Doppler
  `cluster-agent/prd` into a 0600 scratchpad file and run `kubectl get ns` with it. Expected:
  `error: You must be logged in to the server (Unauthorized)`. Delete the file.
- [ ] **Step 3:** Promote to `main` through the house flow, with the operator's OK. Repeat step 2
  with `KUBECONFIG_PRD`.

### Task 20: Doppler, and the operator's UI steps

- [ ] **Step 1 (operator):** After Task 19's two 401s, run `doppler projects delete cluster-agent`.
  The NAS kubeconfigs, and the last copy of every retired key, go with it.
- [ ] **Step 2 (operator):** In the TrueNAS UI, remove the NOPASSWD lines
  `/usr/bin/docker restart cluster-agent` and `/usr/bin/docker exec cluster-agent *`.
- [ ] **Step 3:** Main session confirms with `doppler projects | grep -c cluster-agent`. Expected:
  `0`.

### Task 21: Docs outside the code repos

- [ ] **Step 1: wiki** (worktree off `origin/main`):
  - Rewrite `docs/runbooks/cluster-agent-runbook.md` and `docs/cluster-agent/index.md`.
  - Re-point `docs/cluster-agent/policy.md`, `docs/runbooks/msa2-rebuild-from-zero.md`,
    `docs/runbooks/ses-setup.md` and `docs/runbooks/credentials-inventory.md`.
  - Update the prose pages listed in spec § 6.
  - Add an entry to `docs/cluster-agent/phase-history.md`.
  - In `sync-map.yaml`, map `cluster-agent/CLAUDE.md` to `docs/projects/cluster-agent.md`, with
    its `.pages` and `.gitignore` lines as the other repos have. `~/github/cluster-agent` must
    exist first.
  - After the merge, run `./tools/deploy.sh --verify`.
- [ ] **Step 2: dotfiles:** `claude/workspace-CLAUDE.md` gains a `cluster-agent` row in the
  Applications table.
- [ ] **Step 3: mikrotik-infra:** fix its two `CLAUDE.md` lines about the NAS hosting the agent.
- [ ] **Step 4:** Each PR links the cluster-agent and kube-infra PRs.

### Task 22: Memory

- [ ] **Step 1:** `project_cluster_agent_in_cluster_opus_2026_10_17.md`: spec 1 done, with its
  dates and PR links; the next step is the Opus 5.5 spec.
- [ ] **Step 2:** `project_cluster_agent_token_expiry_2026_09_05.md`: retired, the agent is
  in-cluster and no rotation is due. Update the `MEMORY.md` index lines.
