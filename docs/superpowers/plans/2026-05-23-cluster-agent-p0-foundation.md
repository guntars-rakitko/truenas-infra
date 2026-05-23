# cluster-agent P0 (Foundation) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the `cluster-agent` foundation — docker container on NAS, /health + /metrics endpoints, MCP tool scaffolding that can reach all upstream sources (K8s/Loki/Prometheus/Alertmanager/MinIO/B2/GitHub), state.db plumbing, observability (Prometheus scrape + Grafana dashboard), and docs — **with no LLM calls yet**. Lays the foundation for P1 (Mode A enable) to drop in cleanly post-June-15.

**Architecture:** Single Python container on NAS (mirrors `amtctl` shape: `python:3.14-alpine` base, persistent venv, code uploaded by `manage.sh phase apps`). FastAPI for /health + /metrics. APScheduler for cron-like job orchestration. MCP tool functions with mandatory audit-log emit to Loki. SQLite state.db on bind-mount, nightly backup to new `cluster-agent` MinIO bucket. Read-only K8s ServiceAccounts in both clusters, plus dedicated test-restore namespace in dev. GitHub App `cluster-agent` for repo operations. OAuth credentials for Anthropic surfaced via the existing `_render_compose` `configs:` pattern.

**Tech Stack:** Python 3.14, FastAPI, APScheduler, `prometheus_client`, `httpx`, `pydantic`, `claude-agent-sdk` (imported but unused in P0), `sqlite3` (stdlib), `pytest`. Container base `python:3.14-alpine`. Reuses existing truenas-infra patterns (`modules/apps.py`, `setup-minio-*.sh`).

**Reference:** Design spec at `truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md`.

---

## Pre-flight (operator manual; NOT automatable)

Before any task below can be executed, the operator must complete these one-time setup actions. **These are documented here so the engineer knows what's expected; they're outside the plan's automated scope.**

- [ ] **Pre-1: Generate Doppler keys** (one-time)

  Set these in Doppler `infrastructure/ops` via `doppler secrets set`:
  ```sh
  # GitHub App (created in Pre-2)
  doppler secrets set CLUSTER_AGENT_GH_APP_ID=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_GH_APP_PRIVATE_KEY=... --project infrastructure --config ops  # base64'd PEM
  doppler secrets set CLUSTER_AGENT_GH_APP_INSTALLATION_ID=... --project infrastructure --config ops

  # Claude Agent SDK OAuth credentials (operator runs `claude login` then base64s ~/.claude/.credentials.json)
  doppler secrets set CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS="$(base64 < ~/.claude/.credentials.json)" \
    --project infrastructure --config ops

  # K8s read-only ServiceAccount tokens (filled after Task 4 lands the SAs)
  doppler secrets set CLUSTER_AGENT_KUBECONFIG_DEV=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_KUBECONFIG_PRD=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_KUBECONFIG_TEST_RESTORE_DEV=... --project infrastructure --config ops

  # Observability stack — reuse existing creds where possible
  doppler secrets set CLUSTER_AGENT_LOKI_BASIC_AUTH_DEV=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_LOKI_BASIC_AUTH_PRD=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_DEV=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_PRD=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_DEV=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_PRD=... --project infrastructure --config ops

  # Grafana annotation API tokens (one per cluster)
  doppler secrets set CLUSTER_AGENT_GRAFANA_API_TOKEN_DEV=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_GRAFANA_API_TOKEN_PRD=... --project infrastructure --config ops

  # MinIO + B2 (reuse existing keys if possible)
  doppler secrets set CLUSTER_AGENT_MINIO_NAS_KEY_ID=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_MINIO_NAS_SECRET_KEY=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_B2_KEY_ID=... --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_B2_APP_KEY=... --project infrastructure --config ops

  # Kill switches (default: all enabled)
  doppler secrets set CLUSTER_AGENT_ENABLED=true --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_DISABLED_MODES="" --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_AUTOMERGE_DISABLED_REPOS="" --project infrastructure --config ops
  ```

  SMTP creds (`SMTP_USERNAME`, `SMTP_PASSWORD`) are **reused** from the existing kube-prometheus-stack Alertmanager Doppler keys — do not duplicate.

- [ ] **Pre-2: Register the `cluster-agent` GitHub App**

  In github.com → Settings → Developer settings → GitHub Apps → "New GitHub App":
  - Name: `cluster-agent`
  - Homepage: `https://wiki.w1.lv/cluster-agent/`
  - Webhook: disabled (we poll, not push)
  - Per-repo permissions per spec § 6.1 table (Issues RW everywhere; PRs RW on writable repos; Contents R or RW per table; never admin/settings/org/secrets/packages/workflows)
  - Install on: all 7 repos owned by `guntars-rakitko` (`wiki`, `hw-validation`, `kube-infra`, `truenas-infra`, `mikrotik-infra`, `bios-config`, `GIKS`)
  - After creation: download the private key PEM, copy the App ID + Installation ID
  - Push App ID, private key (base64), and Installation ID into Doppler per Pre-1

- [ ] **Pre-3: Run `claude login` on NAS (or on operator laptop, then copy)**

  ```sh
  # On NAS via SSH, OR on operator laptop:
  claude login    # opens browser, completes OAuth flow
  # Resulting file: ~/.claude/.credentials.json
  base64 < ~/.claude/.credentials.json   # paste into CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS
  ```

  Note: until June 15, 2026, Agent SDK calls draw against the operator's main Max interactive pool. P0 has no LLM calls so this only matters for P1+.

---

## Phase 0a: Cross-repo plumbing — K8s RBAC manifests

This phase lands the K8s ServiceAccounts the agent will authenticate as. Lives in `kube-infra` (the GitOps source of truth).

### Task 1: Read-only ServiceAccount manifests for both clusters

**Files:**
- Create: `kube-infra/flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml`
- Modify: `kube-infra/flux-cd/infrastructure/configs/base/kustomization.yaml` (add new file)

- [ ] **Step 1: Create the read-only ClusterRole + ServiceAccount manifest**

  Create `kube-infra/flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml`:

  ```yaml
  # cluster-agent ServiceAccount + read-only ClusterRole.
  # See truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md § 6.2.
  #
  # The agent (running on NAS) authenticates via the SA token mounted
  # at /var/run/secrets/.../token, supplied via a kubeconfig in Doppler
  # (CLUSTER_AGENT_KUBECONFIG_{DEV,PRD}). Token issued one-time after
  # this manifest reconciles; operator extracts via:
  #
  #   kubectl -n flux-system create token cluster-agent-readonly \
  #     --duration=2160h > kubeconfig-dev-cluster-agent.yaml
  #
  # See wiki/docs/runbooks/cluster-agent-runbook.md § Token Rotation
  # for the full extract + kubeconfig render procedure (lands in Task 26).
  ---
  apiVersion: v1
  kind: ServiceAccount
  metadata:
    name: cluster-agent-readonly
    namespace: flux-system
    annotations:
      description: "Read-only SA for the cluster-agent (running on NAS)."
  ---
  apiVersion: rbac.authorization.k8s.io/v1
  kind: ClusterRole
  metadata:
    name: cluster-agent-readonly
  rules:
    - apiGroups: [""]
      resources:
        - pods
        - pods/log
        - nodes
        - namespaces
        - events
        - services
        - configmaps
        - persistentvolumes
        - persistentvolumeclaims
      verbs: [get, list, watch]
    - apiGroups: ["apps"]
      resources: [deployments, statefulsets, daemonsets, replicasets]
      verbs: [get, list, watch]
    - apiGroups: ["batch"]
      resources: [jobs, cronjobs]
      verbs: [get, list, watch]
    - apiGroups: ["helm.toolkit.fluxcd.io"]
      resources: [helmreleases]
      verbs: [get, list, watch]
    - apiGroups: ["kustomize.toolkit.fluxcd.io"]
      resources: [kustomizations]
      verbs: [get, list, watch]
    - apiGroups: ["source.toolkit.fluxcd.io"]
      resources: [gitrepositories, helmrepositories, ocirepositories]
      verbs: [get, list, watch]
    - apiGroups: ["longhorn.io"]
      resources: [volumes, backups, snapshots, recurringjobs]
      verbs: [get, list, watch]
    - apiGroups: ["velero.io"]
      resources: [backups, restores, schedules, backupstoragelocations]
      verbs: [get, list, watch]
    - apiGroups: ["cert-manager.io"]
      resources: [certificates, issuers, clusterissuers]
      verbs: [get, list, watch]
    - apiGroups: ["traefik.io"]
      resources: [ingressroutes, middlewares]
      verbs: [get, list, watch]
    - apiGroups: ["monitoring.coreos.com"]
      resources: [servicemonitors, podmonitors, prometheusrules, alertmanagers, prometheuses]
      verbs: [get, list, watch]
    # Explicit DENIES would go here if K8s RBAC supported them; instead
    # we rely on the absence of grants. Specifically NEVER granted:
    #   - secrets (any verb)
    #   - pods/exec, pods/attach, pods/portforward
    #   - *.scale subresources
    #   - anything/eviction
  ---
  apiVersion: rbac.authorization.k8s.io/v1
  kind: ClusterRoleBinding
  metadata:
    name: cluster-agent-readonly
  roleRef:
    apiGroup: rbac.authorization.k8s.io
    kind: ClusterRole
    name: cluster-agent-readonly
  subjects:
    - kind: ServiceAccount
      name: cluster-agent-readonly
      namespace: flux-system
  ```

- [ ] **Step 2: Register file in kustomization**

  In `kube-infra/flux-cd/infrastructure/configs/base/kustomization.yaml`, find the `resources:` list and add:

  ```yaml
    - cluster-agent-rbac.yaml
  ```

  (preserve existing ordering; insert alphabetically among siblings).

- [ ] **Step 3: Verify kustomize build is clean**

  Run from `kube-infra/`:
  ```sh
  kubectl kustomize flux-cd/infrastructure/configs/base/ | grep -E "cluster-agent-readonly" | head -10
  ```
  Expected: 4 lines (SA, ClusterRole, ClusterRoleBinding name occurrences) — confirms manifests render.

- [ ] **Step 4: Commit**

  ```sh
  cd /Users/gunrak/github/kube-infra
  git checkout -b chore/cluster-agent-rbac
  git add flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml \
          flux-cd/infrastructure/configs/base/kustomization.yaml
  git commit -m "$(cat <<'EOF'
  feat(rbac): add cluster-agent-readonly SA + ClusterRole

  Read-only ServiceAccount for the cluster-agent (running on NAS).
  Per spec § 6.2: get/list/watch on most resources, NEVER secrets or
  pods/exec.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 2: Test-restore Role + Namespace (dev cluster only)

**Files:**
- Create: `kube-infra/flux-cd/infrastructure/configs/per-cluster/dev/cluster-agent-test-restore.yaml`
- Modify: `kube-infra/flux-cd/infrastructure/configs/per-cluster/dev/kustomization.yaml`

- [ ] **Step 1: Create namespace + Role + SA for test-restore (dev only)**

  Create `kube-infra/flux-cd/infrastructure/configs/per-cluster/dev/cluster-agent-test-restore.yaml`:

  ```yaml
  # Dedicated namespace for the cluster-agent's Mode G (backup verification).
  # Lives in dev cluster ONLY — prd is never used as a test-restore target
  # (spec § 6.2). The agent spins up a throwaway MSSQL pod here, runs
  # RESTORE DATABASE + DBCC CHECKDB, then deletes it.
  ---
  apiVersion: v1
  kind: Namespace
  metadata:
    name: cluster-agent-tests
    labels:
      # Cluster-agent's test workloads are exempt from PSA restricted
      # (same documented exemption as sql-* namespaces — MSSQL on Linux
      # can't exec sqlservr under PSA restricted; see kube-infra #297).
      pod-security.kubernetes.io/enforce: privileged
      pod-security.kubernetes.io/audit: baseline
      pod-security.kubernetes.io/warn: baseline
  ---
  apiVersion: v1
  kind: ServiceAccount
  metadata:
    name: cluster-agent-test-restore
    namespace: cluster-agent-tests
  ---
  apiVersion: rbac.authorization.k8s.io/v1
  kind: Role
  metadata:
    name: cluster-agent-test-restore
    namespace: cluster-agent-tests
  rules:
    - apiGroups: [""]
      resources: [pods, configmaps, services, persistentvolumeclaims]
      verbs: [get, list, watch, create, delete, patch]
    - apiGroups: ["batch"]
      resources: [jobs]
      verbs: [get, list, watch, create, delete]
  ---
  apiVersion: rbac.authorization.k8s.io/v1
  kind: RoleBinding
  metadata:
    name: cluster-agent-test-restore
    namespace: cluster-agent-tests
  roleRef:
    apiGroup: rbac.authorization.k8s.io
    kind: Role
    name: cluster-agent-test-restore
  subjects:
    - kind: ServiceAccount
      name: cluster-agent-test-restore
      namespace: cluster-agent-tests
  ```

- [ ] **Step 2: Register file in per-cluster dev kustomization**

  In `kube-infra/flux-cd/infrastructure/configs/per-cluster/dev/kustomization.yaml`, add to `resources:`:

  ```yaml
    - cluster-agent-test-restore.yaml
  ```

- [ ] **Step 3: Verify kustomize build**

  ```sh
  kubectl kustomize flux-cd/infrastructure/configs/per-cluster/dev/ \
    | grep -E "cluster-agent-test" | head -10
  ```
  Expected: ≥4 matching lines (namespace, SA, Role, RoleBinding).

- [ ] **Step 4: Commit**

  ```sh
  cd /Users/gunrak/github/kube-infra
  git add flux-cd/infrastructure/configs/per-cluster/dev/cluster-agent-test-restore.yaml \
          flux-cd/infrastructure/configs/per-cluster/dev/kustomization.yaml
  git commit -m "$(cat <<'EOF'
  feat(rbac): add cluster-agent-test-restore ns + Role (dev only)

  Dedicated namespace for Mode G backup-verification test pods.
  Per spec § 6.2: dev only, never prd.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 3: PR + tag promote to prd

**Files:**
- Read-only: `kube-infra/tools/promote-to-prd.sh`

- [ ] **Step 1: Push branch + open PR**

  ```sh
  cd /Users/gunrak/github/kube-infra
  git push -u origin chore/cluster-agent-rbac
  gh pr create --title "feat(rbac): cluster-agent SA + RBAC for both clusters" --body "$(cat <<'EOF'
  ## Summary

  - Adds `cluster-agent-readonly` SA + ClusterRole (read-only, no secrets, no exec) — both clusters
  - Adds `cluster-agent-test-restore` ns + namespaced Role — dev only
  - Pre-req for P0 of the cluster-agent (truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md)

  ## Test plan
  - [ ] CI passes
  - [ ] After merge + Flux reconcile, verify on dev:
        `kubectl get sa cluster-agent-readonly -n flux-system`
        `kubectl get ns cluster-agent-tests`
  - [ ] Same on prd (read-only SA only)

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```

- [ ] **Step 2: Merge (after review + CI green)**

  ```sh
  gh pr merge --squash --delete-branch
  ```

- [ ] **Step 3: Verify dev reconciliation**

  ```sh
  export KUBECONFIG=/Users/gunrak/github/kube-infra/talos-os/kubeconfig-dev
  kubectl get sa cluster-agent-readonly -n flux-system
  kubectl get ns cluster-agent-tests
  kubectl get role cluster-agent-test-restore -n cluster-agent-tests
  ```
  Expected: all 3 exist.

- [ ] **Step 4: Promote to prd via semver tag**

  ```sh
  cd /Users/gunrak/github/kube-infra
  ./tools/promote-to-prd.sh patch
  ```
  Watch the script's prompt for the new tag (e.g. `v0.3.0`). Confirm.

- [ ] **Step 5: Verify prd reconciliation**

  ```sh
  export KUBECONFIG=/Users/gunrak/github/kube-infra/talos-os/kubeconfig-prd
  kubectl get sa cluster-agent-readonly -n flux-system
  # cluster-agent-tests ns should NOT exist on prd
  kubectl get ns cluster-agent-tests 2>&1 | grep -q NotFound && echo "OK: ns absent on prd"
  ```

### Task 4: Extract SA tokens + render kubeconfigs

**Files:**
- Create (temporary, gitignored): `/tmp/kubeconfig-cluster-agent-dev.yaml`
- Create (temporary, gitignored): `/tmp/kubeconfig-cluster-agent-prd.yaml`
- Create (temporary, gitignored): `/tmp/kubeconfig-cluster-agent-test-restore-dev.yaml`

- [ ] **Step 1: Extract dev read-only token + render kubeconfig**

  ```sh
  export KUBECONFIG=/Users/gunrak/github/kube-infra/talos-os/kubeconfig-dev
  TOKEN=$(kubectl create token cluster-agent-readonly -n flux-system --duration=2160h)
  CA=$(kubectl config view --raw -o jsonpath='{.clusters[?(@.name=="dev")].cluster.certificate-authority-data}')
  SERVER=$(kubectl config view --raw -o jsonpath='{.clusters[?(@.name=="dev")].cluster.server}')

  cat > /tmp/kubeconfig-cluster-agent-dev.yaml <<EOF
  apiVersion: v1
  kind: Config
  clusters:
    - name: dev
      cluster:
        server: $SERVER
        certificate-authority-data: $CA
  users:
    - name: cluster-agent-readonly
      user:
        token: $TOKEN
  contexts:
    - name: dev
      context:
        cluster: dev
        user: cluster-agent-readonly
  current-context: dev
  EOF
  ```

- [ ] **Step 2: Smoke-test the kubeconfig**

  ```sh
  KUBECONFIG=/tmp/kubeconfig-cluster-agent-dev.yaml kubectl get pods -A 2>&1 | head -5
  KUBECONFIG=/tmp/kubeconfig-cluster-agent-dev.yaml kubectl get secrets -A 2>&1 | head -3
  ```
  Expected: pods list works; secrets list returns `Error from server (Forbidden)`.

- [ ] **Step 3: Repeat for prd**

  Same procedure as Step 1-2 but `KUBECONFIG=kubeconfig-prd` and write to `/tmp/kubeconfig-cluster-agent-prd.yaml`.

- [ ] **Step 4: Extract dev test-restore token**

  ```sh
  export KUBECONFIG=/Users/gunrak/github/kube-infra/talos-os/kubeconfig-dev
  TOKEN=$(kubectl create token cluster-agent-test-restore -n cluster-agent-tests --duration=2160h)
  # Use same SERVER + CA as Step 1
  cat > /tmp/kubeconfig-cluster-agent-test-restore-dev.yaml <<EOF
  apiVersion: v1
  kind: Config
  clusters:
    - name: dev
      cluster:
        server: $SERVER
        certificate-authority-data: $CA
  users:
    - name: cluster-agent-test-restore
      user:
        token: $TOKEN
  contexts:
    - name: dev
      context:
        cluster: dev
        user: cluster-agent-test-restore
        namespace: cluster-agent-tests
  current-context: dev
  EOF
  ```

- [ ] **Step 5: Push all 3 kubeconfigs to Doppler**

  ```sh
  doppler secrets set CLUSTER_AGENT_KUBECONFIG_DEV="$(base64 < /tmp/kubeconfig-cluster-agent-dev.yaml)" \
    --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_KUBECONFIG_PRD="$(base64 < /tmp/kubeconfig-cluster-agent-prd.yaml)" \
    --project infrastructure --config ops
  doppler secrets set CLUSTER_AGENT_KUBECONFIG_TEST_RESTORE_DEV="$(base64 < /tmp/kubeconfig-cluster-agent-test-restore-dev.yaml)" \
    --project infrastructure --config ops
  rm -f /tmp/kubeconfig-cluster-agent-*.yaml
  ```

---

## Phase 0b: NAS infra — MinIO bucket + app registry

### Task 5: Add `cluster-agent` MinIO bucket

**Files:**
- Modify: `truenas-infra/scripts/setup-minio-buckets.sh`
- Modify: `truenas-infra/scripts/setup-minio-lifecycle.sh`

- [ ] **Step 1: Append `cluster-agent` to canonical bucket list**

  In `truenas-infra/scripts/setup-minio-buckets.sh`, find the bash array of canonical buckets (look for `velero`, `longhorn`, `mssql-backups`, `etcd-snapshots`) and add `cluster-agent` to it. Preserve existing array structure and idempotency pattern. Example:

  ```sh
  CANONICAL_BUCKETS=(velero longhorn mssql-backups etcd-snapshots cluster-agent)
  ```

  (Exact variable name depends on existing script — match what's there.)

- [ ] **Step 2: Add 30-day ILM rule for `cluster-agent` bucket**

  In `truenas-infra/scripts/setup-minio-lifecycle.sh`, append after the existing `mssql-backups` rule:

  ```sh
  # cluster-agent: 30-day ILM. State.db nightly backup is small + fully
  # replayable from Loki + GH issues; no reason to retain >30d.
  for alias in nas-prd nas-dev; do
      mc ilm rule add --expire-days "30" "$alias/cluster-agent" || true
  done
  ```

- [ ] **Step 3: Run both scripts against nas-prd + nas-dev**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  ./scripts/setup-minio-buckets.sh
  ./scripts/setup-minio-lifecycle.sh
  ./scripts/setup-minio-encryption.sh   # enables SSE-S3 default on the new bucket
  ```

- [ ] **Step 4: Verify**

  ```sh
  mc ls nas-prd/ | grep cluster-agent
  mc ls nas-dev/ | grep cluster-agent
  mc ilm rule ls nas-prd/cluster-agent
  mc encrypt info nas-prd/cluster-agent  # should show: SSE-S3
  ```

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add scripts/setup-minio-buckets.sh scripts/setup-minio-lifecycle.sh
  git commit -m "$(cat <<'EOF'
  feat(minio): add cluster-agent bucket (30d ILM, SSE-S3)

  Backing store for cluster-agent state.db nightly backups.
  Per truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md § 3.5.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 6: Register Doppler keys in `apps.py`

**Files:**
- Modify: `truenas-infra/src/truenas_infra/modules/apps.py`

- [ ] **Step 1: Find `_DOPPLER_KEYS_PER_APP` and add cluster-agent entry**

  Open `truenas-infra/src/truenas_infra/modules/apps.py` and locate the `_DOPPLER_KEYS_PER_APP` dict (referenced from `truenas-infra/CLAUDE.md` § Secrets). Add this entry, alphabetically sorted among existing app entries:

  ```python
      "cluster-agent": [
          # Auth → Anthropic via Claude Agent SDK (Max subscription OAuth)
          "CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS",
          # GitHub App
          "CLUSTER_AGENT_GH_APP_ID",
          "CLUSTER_AGENT_GH_APP_PRIVATE_KEY",
          "CLUSTER_AGENT_GH_APP_INSTALLATION_ID",
          # K8s — read-only + dedicated test-restore
          "CLUSTER_AGENT_KUBECONFIG_DEV",
          "CLUSTER_AGENT_KUBECONFIG_PRD",
          "CLUSTER_AGENT_KUBECONFIG_TEST_RESTORE_DEV",
          # Observability stack
          "CLUSTER_AGENT_LOKI_BASIC_AUTH_DEV",
          "CLUSTER_AGENT_LOKI_BASIC_AUTH_PRD",
          "CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_DEV",
          "CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_PRD",
          "CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_DEV",
          "CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_PRD",
          "CLUSTER_AGENT_GRAFANA_API_TOKEN_DEV",
          "CLUSTER_AGENT_GRAFANA_API_TOKEN_PRD",
          # S3 backup verification
          "CLUSTER_AGENT_MINIO_NAS_KEY_ID",
          "CLUSTER_AGENT_MINIO_NAS_SECRET_KEY",
          "CLUSTER_AGENT_B2_KEY_ID",
          "CLUSTER_AGENT_B2_APP_KEY",
          # Kill switches (operator-controlled)
          "CLUSTER_AGENT_ENABLED",
          "CLUSTER_AGENT_DISABLED_MODES",
          "CLUSTER_AGENT_AUTOMERGE_DISABLED_REPOS",
      ],
  ```

- [ ] **Step 2: Verify all listed keys exist in Doppler**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  for k in CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS CLUSTER_AGENT_GH_APP_ID \
           CLUSTER_AGENT_GH_APP_PRIVATE_KEY CLUSTER_AGENT_GH_APP_INSTALLATION_ID \
           CLUSTER_AGENT_KUBECONFIG_DEV CLUSTER_AGENT_KUBECONFIG_PRD \
           CLUSTER_AGENT_KUBECONFIG_TEST_RESTORE_DEV CLUSTER_AGENT_ENABLED ; do
    doppler secrets get "$k" --project infrastructure --config ops --plain >/dev/null 2>&1 \
      && echo "ok: $k" \
      || echo "MISSING: $k"
  done
  ```

- [ ] **Step 3: Add to `config/apps.yaml`**

  In `truenas-infra/config/apps.yaml`, find the apps list and append:

  ```yaml
  apps:
    # ... existing apps ...
    - name: cluster-agent
      enabled: true
      # Wave-3 startup: depends on minio (logs go to Loki via the NAS
      # log shipper; minio access for state.db backup). amtctl is wave-3.
  ```

  Match the exact YAML keys used by existing entries (look at one and mirror).

- [ ] **Step 4: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git checkout -b feat/cluster-agent-p0
  git add src/truenas_infra/modules/apps.py config/apps.yaml \
          scripts/setup-minio-buckets.sh scripts/setup-minio-lifecycle.sh
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): register Doppler keys + app entry

  Registers cluster-agent as a NAS app and wires up Doppler key
  delivery (via _render_compose) for the 22 secrets the agent needs.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Phase 0c: Container scaffolding

### Task 7: docker-compose.yaml

**Files:**
- Create: `truenas-infra/apps/cluster-agent/docker-compose.yaml`
- Create: `truenas-infra/apps/cluster-agent/README.md`

- [ ] **Step 1: Create the compose file**

  Create `truenas-infra/apps/cluster-agent/docker-compose.yaml`:

  ```yaml
  # cluster-agent — LLM-driven SRE assistant for the homelab K8s clusters.
  # Spec: truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md
  #
  # Auth: Claude Agent SDK using operator's Max 5x OAuth credentials
  #   (CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS in Doppler, surfaced via
  #    Compose `configs:` block, same pattern as the AIStor license).
  #
  # Image strategy: python:3.14-alpine base + persistent /venv (mirrors
  # amtctl/stress-dashboard). Self-healing venv bootstrap rebuilds if
  # Python version changes.
  #
  # Networking: egress-only. Outbound to clusters (10.10.5.*, 10.10.10.*,
  # 10.10.15.*), Loki/Prom/AM/Grafana (https://*-{dev,prd}.w1.lv),
  # GitHub API, Anthropic API, MinIO (https://s3-*.w1.lv:9000), B2
  # (https://s3.eu-central-NNN.backblazeb2.com).
  # Ingress: just the Prometheus scrape on :9595 (mgmt VLAN).
  configs:
    claude_oauth:
      # Doppler key CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS is the
      # base64-encoded contents of ~/.claude/.credentials.json. The
      # _render_compose layer in modules/apps.py substitutes this
      # value at deploy time, same as the AIStor license pattern.
      content: "${CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS_B64DECODED}"
  services:
    cluster-agent:
      image: python:3.14-alpine
      container_name: cluster-agent
      restart: unless-stopped
      ports:
        # Mgmt VLAN bind only — Prometheus scrapes this directly.
        - "10.10.5.10:9595:9595"
      configs:
        - source: claude_oauth
          target: /claude/.credentials.json
          mode: 0400
      volumes:
        # App code — uploaded by _ensure_cluster_agent_config_via_ctx
        # (added to modules/apps.py in Task 8). Same pattern as amtctl.
        - /mnt/tank/system/apps-config/cluster-agent/code:/app:ro
        # Prompts (versioned in git, uploaded with code)
        - /mnt/tank/system/apps-config/cluster-agent/code/prompts:/prompts:ro
        # Writable state (SQLite DB)
        - /mnt/tank/system/apps-config/cluster-agent/data:/data
        # Persistent venv across image bumps
        - /mnt/tank/system/apps-config/cluster-agent/venv:/venv
      environment:
        # ── Identity / kill switches
        - CLUSTER_AGENT_ENABLED=${CLUSTER_AGENT_ENABLED}
        - CLUSTER_AGENT_DISABLED_MODES=${CLUSTER_AGENT_DISABLED_MODES}
        - CLUSTER_AGENT_AUTOMERGE_DISABLED_REPOS=${CLUSTER_AGENT_AUTOMERGE_DISABLED_REPOS}
        # ── Claude
        - CLAUDE_CREDENTIALS_PATH=/claude/.credentials.json
        # ── GitHub App
        - CLUSTER_AGENT_GH_APP_ID=${CLUSTER_AGENT_GH_APP_ID}
        - CLUSTER_AGENT_GH_APP_PRIVATE_KEY=${CLUSTER_AGENT_GH_APP_PRIVATE_KEY}
        - CLUSTER_AGENT_GH_APP_INSTALLATION_ID=${CLUSTER_AGENT_GH_APP_INSTALLATION_ID}
        # ── K8s
        - CLUSTER_AGENT_KUBECONFIG_DEV=${CLUSTER_AGENT_KUBECONFIG_DEV}
        - CLUSTER_AGENT_KUBECONFIG_PRD=${CLUSTER_AGENT_KUBECONFIG_PRD}
        - CLUSTER_AGENT_KUBECONFIG_TEST_RESTORE_DEV=${CLUSTER_AGENT_KUBECONFIG_TEST_RESTORE_DEV}
        # ── Observability stack
        - CLUSTER_AGENT_LOKI_BASIC_AUTH_DEV=${CLUSTER_AGENT_LOKI_BASIC_AUTH_DEV}
        - CLUSTER_AGENT_LOKI_BASIC_AUTH_PRD=${CLUSTER_AGENT_LOKI_BASIC_AUTH_PRD}
        - CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_DEV=${CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_DEV}
        - CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_PRD=${CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_PRD}
        - CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_DEV=${CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_DEV}
        - CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_PRD=${CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_PRD}
        - CLUSTER_AGENT_GRAFANA_API_TOKEN_DEV=${CLUSTER_AGENT_GRAFANA_API_TOKEN_DEV}
        - CLUSTER_AGENT_GRAFANA_API_TOKEN_PRD=${CLUSTER_AGENT_GRAFANA_API_TOKEN_PRD}
        # ── S3
        - CLUSTER_AGENT_MINIO_NAS_KEY_ID=${CLUSTER_AGENT_MINIO_NAS_KEY_ID}
        - CLUSTER_AGENT_MINIO_NAS_SECRET_KEY=${CLUSTER_AGENT_MINIO_NAS_SECRET_KEY}
        - CLUSTER_AGENT_B2_KEY_ID=${CLUSTER_AGENT_B2_KEY_ID}
        - CLUSTER_AGENT_B2_APP_KEY=${CLUSTER_AGENT_B2_APP_KEY}
        # ── Runtime
        - PATH=/venv/bin:/usr/local/bin:/usr/bin:/bin
        - PYTHONPATH=/app
        - STATE_DB_PATH=/data/state.db
        - LOG_LEVEL=INFO
      working_dir: /app
      command:
        - sh
        - -c
        - |
          set -e
          # Self-healing venv (same shape as amtctl). Rebuild on python
          # version drift (e.g. image bump 3.14 → 3.15).
          if ! /venv/bin/python -c 'import fastapi, apscheduler, prometheus_client, httpx, pydantic, claude_agent_sdk' 2>/dev/null; then
            echo "venv missing or stale — rebuilding…"
            python -m venv --clear /venv
            /venv/bin/pip install --no-cache-dir -q \
              'fastapi>=0.115' \
              'uvicorn[standard]>=0.32' \
              'apscheduler>=3.10' \
              'prometheus_client>=0.21' \
              'httpx>=0.28' \
              'pydantic>=2.10' \
              'pyjwt>=2.10' \
              'claude-agent-sdk>=0.1.0' \
              'tenacity>=9.0' \
              'structlog>=24.4'
          fi
          # mc + kubectl + flux CLIs are installed at venv build time too:
          if ! command -v kubectl >/dev/null 2>&1; then
            apk add --no-cache kubectl mc curl jq sqlite
          fi
          # gh CLI lives in alpine community repo
          if ! command -v gh >/dev/null 2>&1; then
            apk add --no-cache github-cli || true
          fi
          exec /venv/bin/uvicorn main:app --host 0.0.0.0 --port 9595
      healthcheck:
        test: ["CMD", "wget", "-q", "-O-", "http://localhost:9595/health"]
        interval: 30s
        timeout: 5s
        retries: 3
        start_period: 60s
      mem_limit: 1g
      cpus: 1.0
      # Hardening
      read_only: false   # /venv + /data writable; tmpfs not needed since
                         # we have explicit bind-mounts for everything
      cap_drop: [ALL]
      security_opt:
        - no-new-privileges:true
  ```

- [ ] **Step 2: Create README.md**

  Create `truenas-infra/apps/cluster-agent/README.md`:

  ```markdown
  # cluster-agent

  LLM-driven SRE assistant for the homelab. Reads alerts/logs/state from
  both K8s clusters, produces actionable GH issues, triages Renovate PRs,
  runs scheduled backup-verification + doctrine-compliance scans.

  ## Design + spec

  See `truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md`
  for the full design.

  ## Runbook

  See `wiki/docs/runbooks/cluster-agent-runbook.md` for start/stop,
  disable modes, revert auto-merges, interpret /metrics, where to
  look in Loki.

  ## Status

  | Phase | Status |
  |---|---|
  | P0 — Foundation | in progress |
  | P1 — Mode A on dev (sandbox) | not started |
  | P2 — Mode A on dev+prd | not started |
  | P3-P7 | not started |
  ```

- [ ] **Step 3: Verify compose syntax**

  ```sh
  cd /Users/gunrak/github/truenas-infra/apps/cluster-agent
  docker compose config >/dev/null && echo "OK"
  ```

  Note: this won't substitute Doppler env vars in isolation; the verification is just YAML correctness.

- [ ] **Step 4: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/docker-compose.yaml apps/cluster-agent/README.md
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): add docker-compose + README

  Self-healing venv + python:3.14-alpine pattern (matches amtctl).
  OAuth credentials surfaced via Compose configs: block (matches
  AIStor license pattern). Healthcheck on /health, Prometheus
  scrape on 9595.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 8: Wire the `_render_compose` OAuth surfacing

**Files:**
- Modify: `truenas-infra/src/truenas_infra/modules/apps.py`

- [ ] **Step 1: Find existing `_render_compose` and confirm the substitution mechanism**

  Read `truenas-infra/src/truenas_infra/modules/apps.py` and locate `_render_compose`. Note how `MINIO_AISTOR_LICENSE` is currently substituted via `configs:` block (see `truenas-infra/CLAUDE.md` § "Object store: MinIO AIStor Free" for the pattern). The mechanism: `_render_compose` reads the raw template and replaces `${CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS_B64DECODED}` with the base64-decoded value of `CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS`.

- [ ] **Step 2: Add the b64-decode substitution rule**

  In `_render_compose` (or the helper it calls), find the AIStor license decoding logic and mirror it for cluster-agent. The pattern looks roughly like:

  ```python
  # Existing — AIStor license:
  if "MINIO_AISTOR_LICENSE" in raw:
      rendered = rendered.replace(
          "${MINIO_AISTOR_LICENSE}",
          self._doppler.get("MINIO_AISTOR_LICENSE"),
      )

  # NEW — cluster-agent OAuth creds (also b64-decoded for Compose configs:):
  import base64
  if "CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS_B64DECODED" in raw:
      encoded = self._doppler.get("CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS")
      decoded = base64.b64decode(encoded).decode("utf-8")
      rendered = rendered.replace(
          "${CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS_B64DECODED}",
          decoded,
      )
  ```

  **Match the exact style of the existing AIStor handling** — the snippet above is illustrative; replicate the truenas-infra idiom. If the AIStor handling uses a helper method, add cluster-agent to that helper instead of inlining.

- [ ] **Step 3: Add `_ensure_cluster_agent_config_via_ctx` helper**

  Locate `_ensure_amtctl_config_via_ctx` in `modules/apps.py` (per CLAUDE.md `amtctl` reference). Create `_ensure_cluster_agent_config_via_ctx` alongside it with the same shape — uploads `apps/cluster-agent/{src,prompts}/` to `/mnt/tank/system/apps-config/cluster-agent/code/` via the TrueNAS file API. **Don't include venv or data dirs** (the venv self-heals; data persists).

- [ ] **Step 4: Register the helper in the apps phase dispatch**

  Add a registry entry alongside other apps so that `manage.sh phase apps --apply` uploads cluster-agent's code:

  ```python
  # In the apps-phase dispatcher, alongside _ensure_amtctl_config_via_ctx etc.:
  if "cluster-agent" in apps_to_deploy:
      _ensure_cluster_agent_config_via_ctx(ctx)
  ```

  Mirror the existing convention exactly.

- [ ] **Step 5: Smoke-test the render path (without applying)**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  ./manage.sh phase apps   # dry-run (no --apply)
  ```
  Expected: no errors; cluster-agent appears in the planned changes.

- [ ] **Step 6: Commit**

  ```sh
  git add src/truenas_infra/modules/apps.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): wire _render_compose OAuth surfacing + code upload

  Adds a b64-decode substitution rule for the Claude OAuth credentials
  (same shape as the AIStor license handling) and a code-upload helper
  matching the amtctl pattern.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Phase 0d: Python skeleton + state

### Task 9: Repository layout + pyproject (for local dev/test only)

**Files:**
- Create: `truenas-infra/apps/cluster-agent/pyproject.toml`
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/__init__.py`
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/main.py` (will fill in Task 10)
- Create: `truenas-infra/apps/cluster-agent/tests/__init__.py`
- Create: `truenas-infra/apps/cluster-agent/.gitignore`

- [ ] **Step 1: pyproject.toml (for local dev — not used by container)**

  Create `truenas-infra/apps/cluster-agent/pyproject.toml`:

  ```toml
  [project]
  name = "cluster-agent"
  version = "0.1.0"
  description = "LLM-driven SRE assistant for homelab K8s clusters"
  requires-python = ">=3.13"
  dependencies = [
      "fastapi>=0.115",
      "uvicorn[standard]>=0.32",
      "apscheduler>=3.10",
      "prometheus_client>=0.21",
      "httpx>=0.28",
      "pydantic>=2.10",
      "pyjwt>=2.10",
      "claude-agent-sdk>=0.1.0",
      "tenacity>=9.0",
      "structlog>=24.4",
  ]

  [project.optional-dependencies]
  dev = [
      "pytest>=8.3",
      "pytest-asyncio>=0.25",
      "pytest-cov>=6.0",
      "respx>=0.22",  # httpx mocking
      "freezegun>=1.5",
  ]

  [tool.pytest.ini_options]
  asyncio_mode = "auto"
  testpaths = ["tests"]

  [tool.ruff]
  line-length = 100
  ```

- [ ] **Step 2: Package init files**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/__init__.py`:
  ```python
  """cluster-agent — LLM-driven SRE assistant for the homelab K8s clusters."""

  __version__ = "0.1.0"
  ```

  Create `truenas-infra/apps/cluster-agent/tests/__init__.py` (empty file).

- [ ] **Step 3: .gitignore**

  Create `truenas-infra/apps/cluster-agent/.gitignore`:
  ```
  __pycache__/
  *.py[cod]
  .pytest_cache/
  .coverage
  htmlcov/
  .venv/
  *.egg-info/
  data/
  ```

- [ ] **Step 4: Verify pip install works (local sanity)**

  ```sh
  cd /Users/gunrak/github/truenas-infra/apps/cluster-agent
  python3 -m venv .venv
  ./.venv/bin/pip install -e ".[dev]"
  ./.venv/bin/python -c "import cluster_agent; print(cluster_agent.__version__)"
  ```
  Expected: prints `0.1.0`.

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/pyproject.toml \
          apps/cluster-agent/src/cluster_agent/__init__.py \
          apps/cluster-agent/tests/__init__.py \
          apps/cluster-agent/.gitignore
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): pyproject + package scaffolding

  Local dev/test pyproject (container uses inline pip install via
  self-healing venv, not pyproject; this is for laptop iteration).

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 10: state.db schema + dedup module

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/state/__init__.py`
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/state/db.py`
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/state/dedup.py`
- Create: `truenas-infra/apps/cluster-agent/tests/test_dedup.py`

- [ ] **Step 1: Write the failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_dedup.py`:

  ```python
  """Dedup logic — the cluster-agent's anti-spam mechanism for GH issues."""
  import datetime as dt
  from cluster_agent.state.db import StateDB
  from cluster_agent.state.dedup import DedupAction, lookup, record


  def test_new_dedup_key_returns_create(tmp_path):
      """A never-seen dedup_key → action=create."""
      db = StateDB(tmp_path / "state.db")
      action = lookup(db, "alert:Foo:pod-0:dev")
      assert action == DedupAction.create


  def test_open_issue_returns_comment(tmp_path):
      """An open issue for this dedup_key → action=comment (don't reopen)."""
      db = StateDB(tmp_path / "state.db")
      record(db, "alert:Foo:pod-0:dev", gh_issue_ref="kube-infra#100", state="open")
      action = lookup(db, "alert:Foo:pod-0:dev")
      assert action == DedupAction.comment
      assert action.gh_issue_ref == "kube-infra#100"


  def test_recently_closed_issue_returns_reopen(tmp_path):
      """A closed-but-within-7d issue → action=reopen."""
      db = StateDB(tmp_path / "state.db")
      record(
          db, "alert:Foo:pod-0:dev",
          gh_issue_ref="kube-infra#100",
          state="closed",
          closed_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3),
      )
      action = lookup(db, "alert:Foo:pod-0:dev")
      assert action == DedupAction.reopen
      assert action.gh_issue_ref == "kube-infra#100"


  def test_long_closed_issue_returns_create(tmp_path):
      """A closed-over-7d-ago issue → action=create (fresh issue)."""
      db = StateDB(tmp_path / "state.db")
      record(
          db, "alert:Foo:pod-0:dev",
          gh_issue_ref="kube-infra#100",
          state="closed",
          closed_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10),
      )
      action = lookup(db, "alert:Foo:pod-0:dev")
      assert action == DedupAction.create
  ```

- [ ] **Step 2: Run tests — verify they fail**

  ```sh
  cd /Users/gunrak/github/truenas-infra/apps/cluster-agent
  ./.venv/bin/pytest tests/test_dedup.py -v
  ```
  Expected: 4 FAIL (ImportError on `cluster_agent.state.db` etc.).

- [ ] **Step 3: Implement `state/db.py`**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/state/__init__.py` (empty).

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/state/db.py`:

  ```python
  """SQLite state database — single source of truth for findings + dedup."""
  from __future__ import annotations
  import sqlite3
  from pathlib import Path
  from typing import Any


  SCHEMA = """
  CREATE TABLE IF NOT EXISTS findings (
      dedup_key       TEXT PRIMARY KEY,
      gh_issue_ref    TEXT,             -- "owner/repo#NN" or NULL
      state           TEXT NOT NULL,    -- 'open' / 'closed'
      created_at      TEXT NOT NULL,    -- ISO8601 UTC
      last_seen_at    TEXT NOT NULL,    -- ISO8601 UTC
      closed_at       TEXT,             -- ISO8601 UTC or NULL
      mode            TEXT NOT NULL,    -- 'A' / 'B' / etc.
      cluster         TEXT NOT NULL,    -- 'dev' / 'prd' / 'nas' / 'global'
      severity        TEXT NOT NULL,    -- 'high' / 'medium' / 'low' / 'info'
      payload_json    TEXT NOT NULL     -- full Finding schema as JSON
  );

  CREATE INDEX IF NOT EXISTS idx_findings_state ON findings(state);
  CREATE INDEX IF NOT EXISTS idx_findings_cluster ON findings(cluster);

  CREATE TABLE IF NOT EXISTS mode_runs (
      run_id          TEXT PRIMARY KEY,    -- ULID
      mode            TEXT NOT NULL,
      cluster         TEXT,                -- nullable for global modes
      started_at      TEXT NOT NULL,
      ended_at        TEXT,
      status          TEXT,                -- 'success' / 'error' / 'aborted_budget'
      cost_usd        REAL,
      input_tokens    INTEGER,
      output_tokens   INTEGER,
      cache_read_tokens INTEGER,
      error_message   TEXT
  );

  CREATE TABLE IF NOT EXISTS pr_triages (
      pr_ref          TEXT PRIMARY KEY,    -- 'owner/repo#NN'
      triaged_at      TEXT NOT NULL,
      verdict         TEXT NOT NULL,       -- 'auto_merge' / 'skip' / 'comment_only'
      reason          TEXT,
      gh_comment_id   INTEGER
  );

  CREATE TABLE IF NOT EXISTS phase_history (
      phase           TEXT PRIMARY KEY,    -- 'P0' / 'P1' / ...
      entered_at      TEXT NOT NULL,
      exited_at       TEXT,
      operator_note   TEXT
  );
  """


  class StateDB:
      """Thin wrapper around sqlite3 with schema bootstrap + helpers."""

      def __init__(self, path: Path | str) -> None:
          self.path = Path(path)
          self.path.parent.mkdir(parents=True, exist_ok=True)
          self._conn = sqlite3.connect(
              str(self.path), check_same_thread=False, isolation_level=None
          )
          self._conn.row_factory = sqlite3.Row
          self._conn.execute("PRAGMA journal_mode=WAL")
          self._conn.execute("PRAGMA foreign_keys=ON")
          self._conn.executescript(SCHEMA)

      def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
          return self._conn.execute(sql, params)

      def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
          return self._conn.execute(sql, params).fetchone()

      def close(self) -> None:
          self._conn.close()
  ```

- [ ] **Step 4: Implement `state/dedup.py`**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/state/dedup.py`:

  ```python
  """Dedup logic — controls when to create/comment/reopen GH issues."""
  from __future__ import annotations
  import datetime as dt
  import enum
  from dataclasses import dataclass

  from .db import StateDB


  REOPEN_WINDOW = dt.timedelta(days=7)


  class _DedupActionKind(enum.Enum):
      CREATE = "create"
      COMMENT = "comment"
      REOPEN = "reopen"


  @dataclass
  class DedupAction:
      """Result of dedup lookup. `gh_issue_ref` set when kind != create."""
      kind: _DedupActionKind
      gh_issue_ref: str | None = None

      def __eq__(self, other: object) -> bool:
          # Equality with bare enum members for ergonomic tests:
          #   action == DedupAction.create
          if isinstance(other, _DedupActionKind):
              return self.kind == other
          if isinstance(other, DedupAction):
              return self.kind == other.kind and self.gh_issue_ref == other.gh_issue_ref
          return NotImplemented

      def __hash__(self) -> int:
          return hash((self.kind, self.gh_issue_ref))


  # Class-attribute sentinels so callers can write `DedupAction.create`.
  DedupAction.create = _DedupActionKind.CREATE
  DedupAction.comment = _DedupActionKind.COMMENT
  DedupAction.reopen = _DedupActionKind.REOPEN


  def _now() -> dt.datetime:
      return dt.datetime.now(dt.timezone.utc)


  def lookup(db: StateDB, dedup_key: str) -> DedupAction:
      """Decide what to do for a finding with this dedup_key."""
      row = db.fetchone(
          "SELECT gh_issue_ref, state, closed_at FROM findings WHERE dedup_key=?",
          (dedup_key,),
      )
      if row is None:
          return DedupAction(kind=_DedupActionKind.CREATE)
      if row["state"] == "open":
          return DedupAction(kind=_DedupActionKind.COMMENT, gh_issue_ref=row["gh_issue_ref"])
      # closed
      if row["closed_at"]:
          closed_at = dt.datetime.fromisoformat(row["closed_at"])
          if _now() - closed_at <= REOPEN_WINDOW:
              return DedupAction(kind=_DedupActionKind.REOPEN, gh_issue_ref=row["gh_issue_ref"])
      return DedupAction(kind=_DedupActionKind.CREATE)


  def record(
      db: StateDB,
      dedup_key: str,
      *,
      gh_issue_ref: str | None = None,
      state: str = "open",
      closed_at: dt.datetime | None = None,
      mode: str = "A",
      cluster: str = "dev",
      severity: str = "medium",
      payload_json: str = "{}",
  ) -> None:
      """Upsert a finding."""
      now_iso = _now().isoformat()
      closed_iso = closed_at.isoformat() if closed_at else None
      db.execute(
          """
          INSERT INTO findings (
              dedup_key, gh_issue_ref, state, created_at, last_seen_at, closed_at,
              mode, cluster, severity, payload_json
          ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(dedup_key) DO UPDATE SET
              gh_issue_ref=excluded.gh_issue_ref,
              state=excluded.state,
              last_seen_at=excluded.last_seen_at,
              closed_at=excluded.closed_at,
              severity=excluded.severity,
              payload_json=excluded.payload_json
          """,
          (
              dedup_key, gh_issue_ref, state, now_iso, now_iso, closed_iso,
              mode, cluster, severity, payload_json,
          ),
      )
  ```

- [ ] **Step 5: Run tests — verify they pass**

  ```sh
  cd /Users/gunrak/github/truenas-infra/apps/cluster-agent
  ./.venv/bin/pytest tests/test_dedup.py -v
  ```
  Expected: 4 PASS.

- [ ] **Step 6: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/state/ \
          apps/cluster-agent/tests/test_dedup.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): SQLite state schema + dedup logic

  Schema covers findings (dedup_key PK), mode_runs (cost tracking),
  pr_triages (Mode I dedup), and phase_history (rollout audit).
  Dedup returns one of: create (new), comment (open issue exists),
  reopen (closed <7d ago).

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 11: Finding schema (Pydantic)

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/schema.py`
- Create: `truenas-infra/apps/cluster-agent/tests/test_schema.py`

- [ ] **Step 1: Write the failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_schema.py`:

  ```python
  """Finding schema — the JSON contract between LLM output and persistence."""
  import datetime as dt
  import pytest
  from pydantic import ValidationError
  from cluster_agent.schema import Finding, Evidence


  def test_minimal_finding_parses():
      f = Finding(
          id="01JKR8Q9M0000000000000",
          mode="A",
          cluster="dev",
          severity="high",
          title="Test",
          summary="Test summary",
          dedup_key="alert:Foo:bar:dev",
          confidence=0.8,
          evidence=[],
      )
      assert f.severity == "high"
      assert f.created_at is not None


  def test_evidence_with_excerpt():
      ev = Evidence(type="log", ref="loki:foo", excerpt="error: ...")
      assert ev.type == "log"


  def test_invalid_severity_rejected():
      with pytest.raises(ValidationError):
          Finding(
              id="01JKR8Q9M0000000000000",
              mode="A",
              cluster="dev",
              severity="critical",   # not allowed
              title="x", summary="x", dedup_key="x",
              confidence=0.5, evidence=[],
          )


  def test_confidence_clamped_0_1():
      with pytest.raises(ValidationError):
          Finding(
              id="01JKR8Q9M0000000000000",
              mode="A",
              cluster="dev",
              severity="low",
              title="x", summary="x", dedup_key="x",
              confidence=1.5,        # > 1
              evidence=[],
          )


  def test_serialization_roundtrip():
      f = Finding(
          id="01JKR8Q9M0000000000000",
          mode="A", cluster="dev", severity="medium",
          title="x", summary="x", dedup_key="x",
          confidence=0.7, evidence=[Evidence(type="alert", ref="AM/X")],
      )
      json_str = f.model_dump_json()
      f2 = Finding.model_validate_json(json_str)
      assert f2 == f
  ```

- [ ] **Step 2: Run tests — verify failure**

  ```sh
  cd /Users/gunrak/github/truenas-infra/apps/cluster-agent
  ./.venv/bin/pytest tests/test_schema.py -v
  ```
  Expected: ImportError.

- [ ] **Step 3: Implement `schema.py`**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/schema.py`:

  ```python
  """Finding schema — the persistence boundary between LLM output and storage.

  See spec § 4.4 for the full schema.
  """
  from __future__ import annotations
  import datetime as dt
  from typing import Literal, Annotated
  from pydantic import BaseModel, Field, field_validator


  Mode = Literal["A", "B", "D", "E", "F", "G", "H", "I", "J"]
  Cluster = Literal["dev", "prd", "nas", "global"]
  Severity = Literal["high", "medium", "low", "info"]


  class Evidence(BaseModel):
      """One piece of evidence the agent looked at."""
      type: Literal["alert", "log", "metric", "commit", "helmrelease", "pr", "issue", "doc"]
      ref: str
      excerpt: str | None = None


  class AutoAction(BaseModel):
      """Action the agent already took as part of resolving this finding."""
      type: Literal["draft_pr", "comment", "label", "issue_create"]
      ref: str


  class Finding(BaseModel):
      """Structured output of an agent mode run.

      One Finding may correspond to one GH issue (subject to dedup) or
      one wiki report entry. Stored in state.db, dispatched to GH/wiki/
      email/Grafana per the spec § 3.3.
      """
      id: Annotated[str, Field(min_length=26, max_length=26)]   # ULID
      created_at: dt.datetime = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc))
      mode: Mode
      cluster: Cluster
      severity: Severity
      title: str
      summary: str
      evidence: list[Evidence]
      root_cause_hypothesis: str | None = None
      confidence: Annotated[float, Field(ge=0.0, le=1.0)]
      recommended_action: str | None = None
      runbook_ref: str | None = None
      auto_action: AutoAction | None = None
      dedup_key: str

      @field_validator("title")
      @classmethod
      def title_must_be_short(cls, v: str) -> str:
          if len(v) > 200:
              raise ValueError("title must be ≤ 200 chars (GH issue title limit)")
          return v
  ```

- [ ] **Step 4: Run tests — verify pass**

  ```sh
  ./.venv/bin/pytest tests/test_schema.py -v
  ```
  Expected: 5 PASS.

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/schema.py \
          apps/cluster-agent/tests/test_schema.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): Pydantic Finding schema

  Enforces the spec § 4.4 contract: ULID id, enumerated mode/severity/
  cluster, confidence in [0,1], title ≤ 200 chars (GH limit).

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Phase 0e: MCP tools (read-only) + audit log

### Task 12: Audit-log wrapper

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/__init__.py`
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/audit.py`
- Create: `truenas-infra/apps/cluster-agent/tests/test_audit.py`

- [ ] **Step 1: Write the failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_audit.py`:

  ```python
  """Audit log wrapper — every MCP tool call must produce one JSON line."""
  import json
  from cluster_agent.tools.audit import audit, AuditEvent


  def test_audit_decorator_emits_one_event(capsys):
      @audit(tool="dummy_get")
      def dummy_get(arg: str) -> str:
          return "result"

      result = dummy_get("input")
      assert result == "result"
      out = capsys.readouterr().out.strip()
      event = json.loads(out)
      assert event["tool"] == "dummy_get"
      assert event["params"] == {"arg": "input"}
      assert event["status"] == "ok"
      assert "agent_run_id" in event


  def test_audit_redacts_known_secret_fields(capsys):
      @audit(tool="dummy_with_secret", redact=["token"])
      def dummy_with_secret(token: str, public: str) -> str:
          return "ok"

      dummy_with_secret("super-secret", "visible")
      event = json.loads(capsys.readouterr().out.strip())
      assert event["params"]["token"] == "***REDACTED***"
      assert event["params"]["public"] == "visible"


  def test_audit_captures_exception(capsys):
      @audit(tool="dummy_fail")
      def dummy_fail() -> None:
          raise ValueError("boom")

      try:
          dummy_fail()
      except ValueError:
          pass
      event = json.loads(capsys.readouterr().out.strip())
      assert event["status"] == "error"
      assert "ValueError" in event["error"]
      assert "boom" in event["error"]
  ```

- [ ] **Step 2: Run tests — verify fail**

  ```sh
  cd /Users/gunrak/github/truenas-infra/apps/cluster-agent
  ./.venv/bin/pytest tests/test_audit.py -v
  ```

- [ ] **Step 3: Implement audit-log wrapper**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/__init__.py` (empty).

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/audit.py`:

  ```python
  """Audit-log decorator — wraps every MCP tool call.

  Emits one JSON line per call to stdout. The NAS log shipper picks
  it up and routes to Loki, where it's indexed by `tool`, `agent_run_id`,
  `status`. Per spec § 4.3: "Every action is queryable in Loki forever."
  """
  from __future__ import annotations
  import contextvars
  import functools
  import inspect
  import json
  import sys
  import time
  import traceback
  import uuid
  from dataclasses import asdict, dataclass, field
  from typing import Any, Callable


  # Set per-run by the mode runner so all tool calls during a run share an ID.
  _current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
      "agent_run_id", default=None
  )


  def set_run_id(run_id: str) -> None:
      _current_run_id.set(run_id)


  def get_run_id() -> str:
      v = _current_run_id.get()
      if v is None:
          # P0: no mode runner yet; fall back to a per-call UUID.
          return f"adhoc-{uuid.uuid4()}"
      return v


  @dataclass
  class AuditEvent:
      tool: str
      params: dict[str, Any]
      status: str = "ok"
      result_summary: str | None = None
      duration_ms: float = 0.0
      agent_run_id: str = ""
      error: str | None = None
      ts: float = field(default_factory=time.time)


  def _summarize(result: Any) -> str:
      """One-line summary of the result for the audit log."""
      s = repr(result)
      return s if len(s) <= 200 else s[:197] + "..."


  def audit(
      tool: str,
      *,
      redact: list[str] | None = None,
      summarize: Callable[[Any], str] = _summarize,
  ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
      """Decorator that emits one JSON audit line per call.

      Args:
          tool: short name (e.g. "kubectl_get", "loki_query")
          redact: param names to replace with ***REDACTED*** in the audit log
                  (e.g. ["token", "password", "api_key"])
          summarize: callable that turns the return value into a one-line summary
      """
      redact_set = set(redact or [])

      def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
          sig = inspect.signature(fn)

          @functools.wraps(fn)
          def wrapper(*args: Any, **kwargs: Any) -> Any:
              start = time.perf_counter()
              bound = sig.bind(*args, **kwargs)
              bound.apply_defaults()
              params = {
                  k: ("***REDACTED***" if k in redact_set else v)
                  for k, v in bound.arguments.items()
              }
              event = AuditEvent(
                  tool=tool,
                  params=params,
                  agent_run_id=get_run_id(),
              )
              try:
                  result = fn(*args, **kwargs)
                  event.result_summary = summarize(result)
                  return result
              except Exception as exc:
                  event.status = "error"
                  event.error = f"{type(exc).__name__}: {exc}"
                  raise
              finally:
                  event.duration_ms = (time.perf_counter() - start) * 1000
                  print(json.dumps(asdict(event), default=str), file=sys.stdout, flush=True)

          return wrapper

      return decorator
  ```

- [ ] **Step 4: Run tests — verify pass**

  ```sh
  ./.venv/bin/pytest tests/test_audit.py -v
  ```
  Expected: 3 PASS.

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/tools/__init__.py \
          apps/cluster-agent/src/cluster_agent/tools/audit.py \
          apps/cluster-agent/tests/test_audit.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): audit-log decorator for MCP tools

  Per spec § 4.3: every tool call emits one JSON audit event to stdout
  → NAS log shipper → Loki. Redaction list per call for secrets.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 13: kubectl tool (read-only, regex-scoped)

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/kubectl.py`
- Create: `truenas-infra/apps/cluster-agent/tests/test_kubectl.py`

- [ ] **Step 1: Write the failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_kubectl.py`:

  ```python
  """kubectl tool — read-only, allowlist-enforced."""
  import pytest
  from cluster_agent.tools.kubectl import kubectl_get, ToolError


  def test_get_pods_passes_through(monkeypatch):
      """A valid kubectl get pods invocation is allowed."""
      called = {}
      def fake_run(cmd, **kw):
          called["cmd"] = cmd
          class R:
              returncode = 0
              stdout = '{"items":[]}'
              stderr = ""
          return R()
      monkeypatch.setattr("subprocess.run", fake_run)
      result = kubectl_get("dev", "pods", namespace="kube-system")
      assert called["cmd"][0:3] == ["kubectl", "--context", "dev"]
      assert "pods" in called["cmd"]
      assert result == {"items": []}


  def test_get_secrets_blocked(monkeypatch):
      """Listing secrets is hard-blocked even though RBAC also forbids."""
      with pytest.raises(ToolError, match="secrets"):
          kubectl_get("dev", "secrets", namespace="default")


  def test_exec_blocked(monkeypatch):
      """pods/exec is never allowed."""
      with pytest.raises(ToolError, match="exec"):
          kubectl_get("dev", "pods/exec", namespace="default")
  ```

- [ ] **Step 2: Run — fail**

  ```sh
  ./.venv/bin/pytest tests/test_kubectl.py -v
  ```

- [ ] **Step 3: Implement `tools/kubectl.py`**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/kubectl.py`:

  ```python
  """kubectl wrapper — read-only, allowlist-enforced.

  Per spec § 4.3: scoped allowlist + audit-log emit. Even though RBAC
  also blocks secrets, we double-block in code so a misconfigured token
  can't accidentally read them.
  """
  from __future__ import annotations
  import json
  import re
  import subprocess
  from typing import Any

  from .audit import audit


  class ToolError(RuntimeError):
      pass


  # Read-only verbs + resources allowed
  ALLOWED_RESOURCES = re.compile(
      r"^(pods|pods/log|nodes|namespaces|events|services|configmaps|"
      r"persistentvolumes|persistentvolumeclaims|deployments|statefulsets|"
      r"daemonsets|replicasets|jobs|cronjobs|helmreleases|kustomizations|"
      r"gitrepositories|helmrepositories|ocirepositories|volumes\.longhorn\.io|"
      r"backups\.velero\.io|certificates|ingressroutes|servicemonitors|"
      r"podmonitors|prometheusrules)$"
  )

  # Hard-banned even with read RBAC
  BANNED_RESOURCES = {"secrets", "pods/exec", "pods/attach", "pods/portforward"}


  @audit(tool="kubectl_get")
  def kubectl_get(
      context: str,
      resource: str,
      *,
      name: str | None = None,
      namespace: str | None = None,
      label_selector: str | None = None,
      field_selector: str | None = None,
      output: str = "json",
  ) -> dict[str, Any]:
      """Read-only kubectl get against the named context."""
      if resource in BANNED_RESOURCES:
          raise ToolError(f"resource '{resource}' is hard-banned for the agent")
      if not ALLOWED_RESOURCES.match(resource):
          raise ToolError(f"resource '{resource}' not in agent allowlist")

      cmd = ["kubectl", "--context", context, "get", resource]
      if name:
          cmd.append(name)
      if namespace:
          cmd.extend(["-n", namespace])
      else:
          cmd.append("-A")
      if label_selector:
          cmd.extend(["-l", label_selector])
      if field_selector:
          cmd.extend(["--field-selector", field_selector])
      cmd.extend(["-o", output])

      r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
      if r.returncode != 0:
          raise ToolError(f"kubectl get failed: {r.stderr.strip()}")
      if output == "json":
          return json.loads(r.stdout)
      return {"raw": r.stdout}


  @audit(tool="kubectl_describe")
  def kubectl_describe(
      context: str,
      resource: str,
      name: str,
      *,
      namespace: str | None = None,
  ) -> str:
      if resource in BANNED_RESOURCES:
          raise ToolError(f"resource '{resource}' is hard-banned")
      cmd = ["kubectl", "--context", context, "describe", resource, name]
      if namespace:
          cmd.extend(["-n", namespace])
      r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
      if r.returncode != 0:
          raise ToolError(f"kubectl describe failed: {r.stderr.strip()}")
      return r.stdout


  @audit(tool="kubectl_logs")
  def kubectl_logs(
      context: str,
      pod: str,
      namespace: str,
      *,
      container: str | None = None,
      tail: int = 200,
      since: str | None = None,
  ) -> str:
      cmd = ["kubectl", "--context", context, "logs", pod, "-n", namespace, f"--tail={tail}"]
      if container:
          cmd.extend(["-c", container])
      if since:
          cmd.extend(["--since", since])
      r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
      if r.returncode != 0:
          raise ToolError(f"kubectl logs failed: {r.stderr.strip()}")
      return r.stdout
  ```

- [ ] **Step 4: Run — pass**

  ```sh
  ./.venv/bin/pytest tests/test_kubectl.py -v
  ```
  Expected: 3 PASS.

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/tools/kubectl.py \
          apps/cluster-agent/tests/test_kubectl.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): kubectl tool (read-only, allowlist-enforced)

  Wraps `kubectl get/describe/logs` with regex allowlist + hard-banned
  resource list (secrets, pods/exec). Audit-logged via the @audit
  decorator.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 14: Loki + Prometheus + Alertmanager HTTP tools

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/loki.py`
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/prometheus.py`
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/alertmanager.py`
- Create: `truenas-infra/apps/cluster-agent/tests/test_observability_tools.py`

- [ ] **Step 1: Write failing tests**

  Create `truenas-infra/apps/cluster-agent/tests/test_observability_tools.py`:

  ```python
  """Loki, Prometheus, Alertmanager tools — HTTP, read-only, basic-auth."""
  import respx
  import httpx
  import pytest
  from cluster_agent.tools.loki import loki_query
  from cluster_agent.tools.prometheus import prometheus_query
  from cluster_agent.tools.alertmanager import alertmanager_alerts


  @respx.mock
  def test_loki_query_returns_streams(monkeypatch):
      monkeypatch.setenv("CLUSTER_AGENT_LOKI_BASIC_AUTH_DEV", "user:pass")
      url = "https://logs-dev.w1.lv/loki/api/v1/query_range"
      respx.get(url).mock(return_value=httpx.Response(200, json={
          "status": "success",
          "data": {"resultType": "streams", "result": [{"stream": {"app": "x"}, "values": []}]},
      }))
      result = loki_query("dev", '{app="x"}', limit=10)
      assert result["status"] == "success"
      assert len(result["data"]["result"]) == 1


  @respx.mock
  def test_prometheus_query_returns_vector(monkeypatch):
      monkeypatch.setenv("CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_DEV", "user:pass")
      url = "https://metrics-dev.w1.lv/api/v1/query"
      respx.get(url).mock(return_value=httpx.Response(200, json={
          "status": "success",
          "data": {"resultType": "vector", "result": []},
      }))
      result = prometheus_query("dev", "up")
      assert result["status"] == "success"


  @respx.mock
  def test_alertmanager_lists_active(monkeypatch):
      monkeypatch.setenv("CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_DEV", "user:pass")
      url = "https://alerts-dev.w1.lv/api/v2/alerts"
      respx.get(url).mock(return_value=httpx.Response(200, json=[
          {"labels": {"alertname": "Watchdog"}, "status": {"state": "active"}},
      ]))
      alerts = alertmanager_alerts("dev")
      assert len(alerts) == 1
      assert alerts[0]["labels"]["alertname"] == "Watchdog"
  ```

- [ ] **Step 2: Run — fail**

  ```sh
  ./.venv/bin/pytest tests/test_observability_tools.py -v
  ```

- [ ] **Step 3: Implement Loki tool**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/loki.py`:

  ```python
  """Loki LogQL query tool — HTTP, read-only, basic-auth."""
  from __future__ import annotations
  import datetime as dt
  import os
  from typing import Any
  import httpx

  from .audit import audit


  def _auth(cluster: str) -> tuple[str, str]:
      raw = os.environ[f"CLUSTER_AGENT_LOKI_BASIC_AUTH_{cluster.upper()}"]
      user, _, password = raw.partition(":")
      return (user, password)


  @audit(tool="loki_query", redact=["password"])
  def loki_query(
      cluster: str,
      logql: str,
      *,
      start: dt.datetime | None = None,
      end: dt.datetime | None = None,
      limit: int = 100,
  ) -> dict[str, Any]:
      """LogQL query against logs-{cluster}.w1.lv. Read-only by API."""
      now = dt.datetime.now(dt.timezone.utc)
      start = start or now - dt.timedelta(hours=1)
      end = end or now
      params = {
          "query": logql,
          "start": str(int(start.timestamp() * 1e9)),
          "end": str(int(end.timestamp() * 1e9)),
          "limit": limit,
      }
      url = f"https://logs-{cluster}.w1.lv/loki/api/v1/query_range"
      r = httpx.get(url, params=params, auth=_auth(cluster), timeout=15)
      r.raise_for_status()
      return r.json()
  ```

- [ ] **Step 4: Implement Prometheus tool**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/prometheus.py`:

  ```python
  """Prometheus PromQL tool — HTTP, read-only."""
  from __future__ import annotations
  import os
  from typing import Any
  import httpx

  from .audit import audit


  def _auth(cluster: str) -> tuple[str, str]:
      raw = os.environ[f"CLUSTER_AGENT_PROMETHEUS_BASIC_AUTH_{cluster.upper()}"]
      user, _, password = raw.partition(":")
      return (user, password)


  @audit(tool="prometheus_query", redact=["password"])
  def prometheus_query(cluster: str, promql: str) -> dict[str, Any]:
      """Instant PromQL query."""
      url = f"https://metrics-{cluster}.w1.lv/api/v1/query"
      r = httpx.get(url, params={"query": promql}, auth=_auth(cluster), timeout=15)
      r.raise_for_status()
      return r.json()


  @audit(tool="prometheus_query_range", redact=["password"])
  def prometheus_query_range(
      cluster: str,
      promql: str,
      *,
      start: str,
      end: str,
      step: str = "60s",
  ) -> dict[str, Any]:
      """Range PromQL query. start/end as RFC3339 or unix timestamps."""
      url = f"https://metrics-{cluster}.w1.lv/api/v1/query_range"
      r = httpx.get(
          url,
          params={"query": promql, "start": start, "end": end, "step": step},
          auth=_auth(cluster),
          timeout=30,
      )
      r.raise_for_status()
      return r.json()
  ```

- [ ] **Step 5: Implement Alertmanager tool**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/alertmanager.py`:

  ```python
  """Alertmanager tool — HTTP, read-only."""
  from __future__ import annotations
  import os
  from typing import Any
  import httpx

  from .audit import audit


  def _auth(cluster: str) -> tuple[str, str]:
      raw = os.environ[f"CLUSTER_AGENT_ALERTMANAGER_BASIC_AUTH_{cluster.upper()}"]
      user, _, password = raw.partition(":")
      return (user, password)


  @audit(tool="alertmanager_alerts", redact=["password"])
  def alertmanager_alerts(
      cluster: str,
      *,
      active: bool = True,
      silenced: bool = False,
      inhibited: bool = False,
  ) -> list[dict[str, Any]]:
      """List alerts."""
      url = f"https://alerts-{cluster}.w1.lv/api/v2/alerts"
      r = httpx.get(
          url,
          params={"active": str(active).lower(), "silenced": str(silenced).lower(),
                  "inhibited": str(inhibited).lower()},
          auth=_auth(cluster),
          timeout=15,
      )
      r.raise_for_status()
      return r.json()
  ```

- [ ] **Step 6: Run — pass**

  ```sh
  ./.venv/bin/pytest tests/test_observability_tools.py -v
  ```
  Expected: 3 PASS.

- [ ] **Step 7: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/tools/loki.py \
          apps/cluster-agent/src/cluster_agent/tools/prometheus.py \
          apps/cluster-agent/src/cluster_agent/tools/alertmanager.py \
          apps/cluster-agent/tests/test_observability_tools.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): Loki + Prometheus + Alertmanager tools

  HTTP, read-only, basic-auth (creds from Doppler env). All audit-logged
  with the password param redacted.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 15: GitHub App tool (read for now, write surface stubs)

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/github.py`
- Create: `truenas-infra/apps/cluster-agent/tests/test_github.py`

- [ ] **Step 1: Write failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_github.py`:

  ```python
  """GitHub App tool — JWT → installation token → REST calls."""
  import datetime as dt
  import respx
  import httpx
  from cluster_agent.tools.github import (
      _build_app_jwt,
      gh_get_installation_token,
      gh_list_prs,
  )


  TEST_PEM = """-----BEGIN RSA PRIVATE KEY-----
  MIIEpAIBAAKCAQEAv8x+VG5ZkF3JZ8z3KrwYV6yhP3FwGKLkXxhJSv7q3eHGRpwM
  ...
  -----END RSA PRIVATE KEY-----"""


  def test_jwt_has_iss_iat_exp(monkeypatch):
      monkeypatch.setenv("CLUSTER_AGENT_GH_APP_ID", "12345")
      monkeypatch.setenv("CLUSTER_AGENT_GH_APP_PRIVATE_KEY",
                         "LS0tLS1CRUdJTi...")  # base64 PEM (will be decoded)
      # Just check the function calls jwt.encode with the right payload shape
      called = {}
      def fake_encode(payload, key, algorithm):
          called["payload"] = payload
          return "fake.jwt.token"
      import cluster_agent.tools.github as gh
      monkeypatch.setattr(gh.jwt, "encode", fake_encode)
      token = _build_app_jwt()
      assert token == "fake.jwt.token"
      assert called["payload"]["iss"] == "12345"
      assert "iat" in called["payload"]
      assert "exp" in called["payload"]


  @respx.mock
  def test_gh_list_prs_returns_array(monkeypatch):
      monkeypatch.setenv("CLUSTER_AGENT_GH_APP_ID", "12345")
      monkeypatch.setenv("CLUSTER_AGENT_GH_APP_PRIVATE_KEY", "ignored-in-mock")
      monkeypatch.setenv("CLUSTER_AGENT_GH_APP_INSTALLATION_ID", "67890")

      import cluster_agent.tools.github as gh
      monkeypatch.setattr(gh, "_build_app_jwt", lambda: "fake.jwt")
      monkeypatch.setattr(gh, "gh_get_installation_token", lambda: "ghs_faketoken")

      url = "https://api.github.com/repos/guntars-rakitko/kube-infra/pulls"
      respx.get(url).mock(return_value=httpx.Response(200, json=[
          {"number": 1, "title": "test pr", "state": "open"},
      ]))
      prs = gh_list_prs("guntars-rakitko/kube-infra")
      assert len(prs) == 1
      assert prs[0]["title"] == "test pr"
  ```

- [ ] **Step 2: Run — fail**

  ```sh
  ./.venv/bin/pytest tests/test_github.py -v
  ```

- [ ] **Step 3: Implement GitHub tool**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/github.py`:

  ```python
  """GitHub App authentication + read tools.

  JWT signed with the App private key → installation access token →
  REST API calls. Token cached in memory for ~50min (GH default 1h).
  """
  from __future__ import annotations
  import base64
  import datetime as dt
  import os
  import time
  from typing import Any

  import httpx
  import jwt   # pyjwt

  from .audit import audit


  _GH_API = "https://api.github.com"
  _token_cache: dict[str, Any] = {"token": None, "expires_at": 0.0}


  def _build_app_jwt() -> str:
      """Sign a 10-minute JWT with the App's private key."""
      app_id = os.environ["CLUSTER_AGENT_GH_APP_ID"]
      pem_b64 = os.environ["CLUSTER_AGENT_GH_APP_PRIVATE_KEY"]
      pem = base64.b64decode(pem_b64)
      now = int(time.time())
      return jwt.encode(
          {"iss": app_id, "iat": now - 60, "exp": now + 540},
          pem,
          algorithm="RS256",
      )


  @audit(tool="gh_get_installation_token")
  def gh_get_installation_token() -> str:
      """Trade the App JWT for a short-lived installation token (cached)."""
      now = time.time()
      if _token_cache["token"] and _token_cache["expires_at"] - now > 60:
          return _token_cache["token"]
      installation_id = os.environ["CLUSTER_AGENT_GH_APP_INSTALLATION_ID"]
      r = httpx.post(
          f"{_GH_API}/app/installations/{installation_id}/access_tokens",
          headers={"Authorization": f"Bearer {_build_app_jwt()}",
                   "Accept": "application/vnd.github+json"},
          timeout=15,
      )
      r.raise_for_status()
      data = r.json()
      _token_cache["token"] = data["token"]
      _token_cache["expires_at"] = dt.datetime.fromisoformat(
          data["expires_at"].replace("Z", "+00:00")
      ).timestamp()
      return data["token"]


  def _gh_headers() -> dict[str, str]:
      return {
          "Authorization": f"Bearer {gh_get_installation_token()}",
          "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
      }


  @audit(tool="gh_list_prs")
  def gh_list_prs(repo: str, *, state: str = "open") -> list[dict[str, Any]]:
      r = httpx.get(
          f"{_GH_API}/repos/{repo}/pulls",
          params={"state": state, "per_page": 50},
          headers=_gh_headers(),
          timeout=15,
      )
      r.raise_for_status()
      return r.json()


  @audit(tool="gh_get_pr")
  def gh_get_pr(repo: str, number: int) -> dict[str, Any]:
      r = httpx.get(
          f"{_GH_API}/repos/{repo}/pulls/{number}",
          headers=_gh_headers(),
          timeout=15,
      )
      r.raise_for_status()
      return r.json()


  @audit(tool="gh_get_commit")
  def gh_get_commit(repo: str, sha: str) -> dict[str, Any]:
      r = httpx.get(
          f"{_GH_API}/repos/{repo}/commits/{sha}",
          headers=_gh_headers(),
          timeout=15,
      )
      r.raise_for_status()
      return r.json()


  # ── Write surface (used in P1+) — kept as stubs in P0 so P0 imports work
  # without exercising the write path.

  @audit(tool="gh_issue_create")
  def gh_issue_create(repo: str, title: str, body: str, *, labels: list[str] | None = None) -> dict[str, Any]:
      payload: dict[str, Any] = {"title": title, "body": body}
      if labels:
          payload["labels"] = labels
      r = httpx.post(
          f"{_GH_API}/repos/{repo}/issues",
          json=payload,
          headers=_gh_headers(),
          timeout=15,
      )
      r.raise_for_status()
      return r.json()


  @audit(tool="gh_issue_comment")
  def gh_issue_comment(repo: str, number: int, body: str) -> dict[str, Any]:
      r = httpx.post(
          f"{_GH_API}/repos/{repo}/issues/{number}/comments",
          json={"body": body},
          headers=_gh_headers(),
          timeout=15,
      )
      r.raise_for_status()
      return r.json()


  @audit(tool="gh_pr_comment")
  def gh_pr_comment(repo: str, number: int, body: str) -> dict[str, Any]:
      # PR comments are issue comments (GH API distinction)
      return gh_issue_comment(repo, number, body)
  ```

- [ ] **Step 4: Run — pass**

  ```sh
  ./.venv/bin/pytest tests/test_github.py -v
  ```

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/tools/github.py \
          apps/cluster-agent/tests/test_github.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): GitHub App tool (read + write surface stubs)

  JWT-signed App auth → installation token (cached). Read tools used
  in P0 smoke; write tools (issue_create, comment) are functional but
  unexercised until P1+.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 16: mc tool (MinIO + B2)

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/mc.py`
- Create: `truenas-infra/apps/cluster-agent/tests/test_mc.py`

- [ ] **Step 1: Write failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_mc.py`:

  ```python
  """mc tool — wraps minio-client CLI for MinIO + B2."""
  from cluster_agent.tools.mc import mc_ls, mc_stat


  def test_mc_ls_calls_correct_alias(monkeypatch):
      called = {}
      def fake_run(cmd, **kw):
          called["cmd"] = cmd
          class R:
              returncode = 0
              stdout = '[{"key":"foo","size":100}]'
              stderr = ""
          return R()
      monkeypatch.setattr("subprocess.run", fake_run)
      result = mc_ls("nas-prd/mssql-backups/")
      assert called["cmd"][0] == "mc"
      assert "nas-prd/mssql-backups/" in called["cmd"]
      assert isinstance(result, list)
  ```

- [ ] **Step 2: Run — fail**

  ```sh
  ./.venv/bin/pytest tests/test_mc.py -v
  ```

- [ ] **Step 3: Implement mc tool**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/tools/mc.py`:

  ```python
  """mc tool — minio-client CLI wrapper for MinIO + B2.

  Aliases (`nas-prd`, `nas-dev`, `b2-eu`) are configured at container
  start by setup script (Task 19). Read tools used in P0; write tools
  (cp for state.db backup) used by the agent's scheduler in P1+.
  """
  from __future__ import annotations
  import json
  import subprocess
  from typing import Any

  from .audit import audit


  class McError(RuntimeError):
      pass


  @audit(tool="mc_ls")
  def mc_ls(path: str, *, recursive: bool = False) -> list[dict[str, Any]]:
      """List objects at path (e.g. 'nas-prd/mssql-backups/')."""
      cmd = ["mc", "ls", "--json"]
      if recursive:
          cmd.append("--recursive")
      cmd.append(path)
      r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
      if r.returncode != 0:
          raise McError(f"mc ls failed: {r.stderr.strip()}")
      return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


  @audit(tool="mc_stat")
  def mc_stat(path: str) -> dict[str, Any]:
      r = subprocess.run(["mc", "stat", "--json", path], capture_output=True, text=True, timeout=15)
      if r.returncode != 0:
          raise McError(f"mc stat failed: {r.stderr.strip()}")
      return json.loads(r.stdout)


  @audit(tool="mc_cp")
  def mc_cp(src: str, dst: str) -> None:
      """Copy one object. Used by scheduler for state.db backup."""
      r = subprocess.run(["mc", "cp", "--json", src, dst], capture_output=True, text=True, timeout=300)
      if r.returncode != 0:
          raise McError(f"mc cp failed: {r.stderr.strip()}")
  ```

- [ ] **Step 4: Run — pass**

  ```sh
  ./.venv/bin/pytest tests/test_mc.py -v
  ```

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/tools/mc.py \
          apps/cluster-agent/tests/test_mc.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): mc tool (MinIO + B2)

  Wraps `mc ls/stat/cp --json`. Aliases assumed configured at container
  start (alias setup in Task 19).

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Phase 0f: /metrics + /health + scheduler skeleton

### Task 17: Prometheus metrics module

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/emit/__init__.py`
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/emit/metrics.py`
- Create: `truenas-infra/apps/cluster-agent/tests/test_metrics.py`

- [ ] **Step 1: Write failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_metrics.py`:

  ```python
  """Prometheus metrics emit module."""
  from cluster_agent.emit.metrics import (
      cluster_agent_run_total,
      cluster_agent_finding_total,
      cluster_agent_anthropic_cost_usd_total,
      cluster_agent_last_success_timestamp,
      render,
  )


  def test_counter_increments():
      before = cluster_agent_run_total.labels(mode="A", status="success")._value.get()
      cluster_agent_run_total.labels(mode="A", status="success").inc()
      after = cluster_agent_run_total.labels(mode="A", status="success")._value.get()
      assert after == before + 1


  def test_render_includes_all_required_metrics():
      output = render()
      for metric_name in [
          "cluster_agent_run_total",
          "cluster_agent_run_duration_seconds",
          "cluster_agent_finding_total",
          "cluster_agent_open_findings",
          "cluster_agent_pr_action_total",
          "cluster_agent_anthropic_tokens_total",
          "cluster_agent_anthropic_cost_usd_total",
          "cluster_agent_last_success_timestamp",
          "cluster_agent_backup_verification_status",
          "cluster_agent_doctrine_drift_count",
      ]:
          assert metric_name in output, f"missing metric: {metric_name}"
  ```

- [ ] **Step 2: Run — fail**

  ```sh
  ./.venv/bin/pytest tests/test_metrics.py -v
  ```

- [ ] **Step 3: Implement metrics module**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/emit/__init__.py` (empty).

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/emit/metrics.py`:

  ```python
  """Prometheus metrics — see spec § 3.4."""
  from __future__ import annotations
  from prometheus_client import (
      Counter, Gauge, Histogram, REGISTRY, generate_latest, CONTENT_TYPE_LATEST,
  )


  cluster_agent_run_total = Counter(
      "cluster_agent_run_total",
      "Total mode runs by status",
      ["mode", "status"],
  )

  cluster_agent_run_duration_seconds = Histogram(
      "cluster_agent_run_duration_seconds",
      "Time per mode run",
      ["mode"],
      buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1800),
  )

  cluster_agent_finding_total = Counter(
      "cluster_agent_finding_total",
      "Findings produced",
      ["mode", "severity", "category"],
  )

  cluster_agent_open_findings = Gauge(
      "cluster_agent_open_findings",
      "Currently open findings (GH issues opened by agent)",
      ["severity"],
  )

  cluster_agent_pr_action_total = Counter(
      "cluster_agent_pr_action_total",
      "PR actions taken",
      ["action"],  # comment / auto_merge / skip_for_review
  )

  cluster_agent_anthropic_tokens_total = Counter(
      "cluster_agent_anthropic_tokens_total",
      "Anthropic API token counts",
      ["kind"],  # input / output / cache_read
  )

  cluster_agent_anthropic_cost_usd_total = Counter(
      "cluster_agent_anthropic_cost_usd_total",
      "Cumulative Anthropic spend in USD",
  )

  cluster_agent_last_success_timestamp = Gauge(
      "cluster_agent_last_success_timestamp",
      "Unix epoch of last successful run per mode",
      ["mode"],
  )

  cluster_agent_backup_verification_status = Gauge(
      "cluster_agent_backup_verification_status",
      "1=pass, 0=fail per target",
      ["target"],  # b2 / velero / litestream
  )

  cluster_agent_doctrine_drift_count = Gauge(
      "cluster_agent_doctrine_drift_count",
      "Doctrine violations found in last scan",
      ["repo"],
  )


  def render() -> str:
      return generate_latest(REGISTRY).decode("utf-8")
  ```

- [ ] **Step 4: Run — pass**

  ```sh
  ./.venv/bin/pytest tests/test_metrics.py -v
  ```

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/emit/ \
          apps/cluster-agent/tests/test_metrics.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): Prometheus metrics module

  All 10 metrics from spec § 3.4 declared. Both clusters scrape this
  endpoint from the NAS (scrape config added in Task 20).

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 18: FastAPI app (/health + /metrics endpoints)

**Files:**
- Create: `truenas-infra/apps/cluster-agent/main.py` (top-level, container expects this name)
- Create: `truenas-infra/apps/cluster-agent/tests/test_main.py`

- [ ] **Step 1: Write failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_main.py`:

  ```python
  """FastAPI app smoke."""
  import pytest
  from fastapi.testclient import TestClient
  from main import app


  @pytest.fixture
  def client():
      return TestClient(app)


  def test_health_endpoint(client):
      r = client.get("/health")
      assert r.status_code == 200
      body = r.json()
      assert "status" in body
      assert body["status"] in ("ok", "degraded")
      assert "modes" in body


  def test_metrics_endpoint(client):
      r = client.get("/metrics")
      assert r.status_code == 200
      assert "cluster_agent_run_total" in r.text
  ```

- [ ] **Step 2: Run — fail**

  ```sh
  ./.venv/bin/pytest tests/test_main.py -v
  ```

- [ ] **Step 3: Implement main.py**

  Create `truenas-infra/apps/cluster-agent/main.py`:

  ```python
  """cluster-agent — FastAPI entrypoint.

  Mounted at /app by docker-compose; uvicorn invoked as `main:app`.
  Exposes /health (Docker + ops use) and /metrics (Prometheus scrape).
  Scheduled jobs run via APScheduler in a background thread (Task 19).
  """
  from __future__ import annotations
  import os
  import time
  from fastapi import FastAPI, Response
  from prometheus_client import CONTENT_TYPE_LATEST

  from cluster_agent.emit.metrics import render, cluster_agent_last_success_timestamp


  app = FastAPI(title="cluster-agent", version="0.1.0")
  _BOOT_TIME = time.time()


  @app.get("/health")
  async def health() -> dict:
      """Container healthcheck + ops endpoint.

      Returns ok/degraded based on per-mode last-success timestamps.
      In P0 there are no modes running, so this is mostly an "is the
      container alive" check.
      """
      modes: dict[str, dict] = {}
      enabled = os.environ.get("CLUSTER_AGENT_ENABLED", "true").lower() == "true"
      disabled_modes = {
          m.strip() for m in os.environ.get("CLUSTER_AGENT_DISABLED_MODES", "").split(",") if m.strip()
      }
      # In P0 no modes yet — just report config visibility
      return {
          "status": "ok",
          "version": "0.1.0",
          "uptime_seconds": int(time.time() - _BOOT_TIME),
          "enabled": enabled,
          "disabled_modes": list(disabled_modes),
          "modes": modes,
      }


  @app.get("/metrics")
  async def metrics() -> Response:
      """Prometheus scrape endpoint."""
      return Response(content=render(), media_type=CONTENT_TYPE_LATEST)


  @app.get("/")
  async def root() -> dict:
      return {
          "name": "cluster-agent",
          "version": "0.1.0",
          "endpoints": ["/health", "/metrics"],
          "docs": "wiki.w1.lv/runbooks/cluster-agent-runbook",
      }


  if __name__ == "__main__":
      import uvicorn
      uvicorn.run(app, host="0.0.0.0", port=9595)
  ```

- [ ] **Step 4: Run — pass**

  ```sh
  cd /Users/gunrak/github/truenas-infra/apps/cluster-agent
  PYTHONPATH=src ./.venv/bin/pytest tests/test_main.py -v
  ```
  Expected: 2 PASS.

- [ ] **Step 5: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/main.py apps/cluster-agent/tests/test_main.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): FastAPI app with /health + /metrics

  /health used by Docker healthcheck + ops. /metrics scraped by both
  clusters' Prometheus.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

### Task 19: APScheduler skeleton + Doppler-driven kill switch

**Files:**
- Create: `truenas-infra/apps/cluster-agent/src/cluster_agent/scheduler.py`
- Modify: `truenas-infra/apps/cluster-agent/main.py` (wire scheduler startup)
- Create: `truenas-infra/apps/cluster-agent/tests/test_scheduler.py`

- [ ] **Step 1: Write failing test**

  Create `truenas-infra/apps/cluster-agent/tests/test_scheduler.py`:

  ```python
  """Scheduler — APScheduler skeleton, no modes yet in P0."""
  from cluster_agent.scheduler import Scheduler, is_mode_enabled


  def test_mode_enabled_when_default(monkeypatch):
      monkeypatch.setenv("CLUSTER_AGENT_ENABLED", "true")
      monkeypatch.delenv("CLUSTER_AGENT_DISABLED_MODES", raising=False)
      assert is_mode_enabled("A") is True


  def test_mode_disabled_globally(monkeypatch):
      monkeypatch.setenv("CLUSTER_AGENT_ENABLED", "false")
      assert is_mode_enabled("A") is False


  def test_mode_disabled_individually(monkeypatch):
      monkeypatch.setenv("CLUSTER_AGENT_ENABLED", "true")
      monkeypatch.setenv("CLUSTER_AGENT_DISABLED_MODES", "A,F,J")
      assert is_mode_enabled("A") is False
      assert is_mode_enabled("B") is True


  def test_scheduler_starts_and_stops():
      s = Scheduler()
      s.start()
      assert s.running
      s.shutdown(wait=False)
      assert not s.running
  ```

- [ ] **Step 2: Run — fail**

  ```sh
  ./.venv/bin/pytest tests/test_scheduler.py -v
  ```

- [ ] **Step 3: Implement scheduler**

  Create `truenas-infra/apps/cluster-agent/src/cluster_agent/scheduler.py`:

  ```python
  """APScheduler-based mode scheduler.

  In P0: empty — no modes registered. The skeleton + kill-switch logic
  lands here so P1's mode registration is a one-liner add per mode.
  """
  from __future__ import annotations
  import os
  from apscheduler.schedulers.background import BackgroundScheduler


  def is_mode_enabled(mode: str) -> bool:
      """Check the two kill switches: global + per-mode."""
      if os.environ.get("CLUSTER_AGENT_ENABLED", "true").lower() != "true":
          return False
      disabled = {
          m.strip() for m in os.environ.get("CLUSTER_AGENT_DISABLED_MODES", "").split(",") if m.strip()
      }
      return mode not in disabled


  class Scheduler:
      """Wraps APScheduler with our kill-switch + audit hook."""

      def __init__(self) -> None:
          self._sched = BackgroundScheduler(timezone="Europe/Riga")
          self.running = False

      def start(self) -> None:
          self._sched.start()
          self.running = True

      def shutdown(self, wait: bool = True) -> None:
          self._sched.shutdown(wait=wait)
          self.running = False

      def add_mode(self, mode: str, func, trigger: str, **trigger_kwargs) -> None:
          """Add a mode runner with the kill switch wrapping its execution."""
          def wrapped():
              if not is_mode_enabled(mode):
                  return
              func()
          self._sched.add_job(wrapped, trigger=trigger, id=f"mode-{mode}", **trigger_kwargs)
  ```

- [ ] **Step 4: Wire scheduler into main.py**

  Edit `truenas-infra/apps/cluster-agent/main.py`. Add at the top:

  ```python
  from cluster_agent.scheduler import Scheduler
  ```

  Add a startup + shutdown hook:

  ```python
  _scheduler = Scheduler()


  @app.on_event("startup")
  async def _start_scheduler() -> None:
      _scheduler.start()
      # P0: no modes registered yet. P1+ will add via _scheduler.add_mode(...)


  @app.on_event("shutdown")
  async def _stop_scheduler() -> None:
      _scheduler.shutdown(wait=False)
  ```

  Replace the `if __name__ == "__main__":` block to also use uvicorn but with the lifespan hooks honored.

- [ ] **Step 5: Run — pass**

  ```sh
  ./.venv/bin/pytest tests/test_scheduler.py tests/test_main.py -v
  ```
  Expected: all PASS.

- [ ] **Step 6: Commit**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git add apps/cluster-agent/src/cluster_agent/scheduler.py \
          apps/cluster-agent/main.py \
          apps/cluster-agent/tests/test_scheduler.py
  git commit -m "$(cat <<'EOF'
  feat(cluster-agent): APScheduler skeleton + kill switches

  P0 lands the scheduler infrastructure with global + per-mode kill
  switches (CLUSTER_AGENT_ENABLED, CLUSTER_AGENT_DISABLED_MODES). No
  modes registered yet — P1 adds Mode A.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ```

---

## Phase 0g: Observability (Prometheus + Grafana)

### Task 20: Prometheus scrape config (both clusters)

**Files:**
- Modify: `kube-infra/flux-cd/infrastructure/helmreleases/kube-prometheus-stack.yaml`

- [ ] **Step 1: Add NAS scrape job to kube-prometheus-stack values**

  Open `kube-infra/flux-cd/infrastructure/helmreleases/kube-prometheus-stack.yaml`. Locate `spec.values.prometheus.prometheusSpec.additionalScrapeConfigs:` (or add it if absent). Add:

  ```yaml
              additionalScrapeConfigs:
                - job_name: cluster-agent-nas
                  scrape_interval: 60s
                  scrape_timeout: 15s
                  metrics_path: /metrics
                  static_configs:
                    - targets:
                        - 10.10.5.10:9595
                      labels:
                        instance: nas
                        # NAS lives outside cluster — sourced via mgmt VLAN
                        # (10.10.5.0/24). Both clusters scrape independently.
  ```

- [ ] **Step 2: Verify YAML parses + render**

  ```sh
  cd /Users/gunrak/github/kube-infra
  yq '.spec.values.prometheus.prometheusSpec.additionalScrapeConfigs' \
     flux-cd/infrastructure/helmreleases/kube-prometheus-stack.yaml
  ```
  Expected: shows the new `cluster-agent-nas` job.

- [ ] **Step 3: PR + merge**

  ```sh
  cd /Users/gunrak/github/kube-infra
  git checkout -b feat/cluster-agent-scrape
  git add flux-cd/infrastructure/helmreleases/kube-prometheus-stack.yaml
  git commit -m "$(cat <<'EOF'
  feat(monitoring): scrape cluster-agent on NAS (both clusters)

  Adds an additionalScrapeConfigs entry for the cluster-agent's /metrics
  endpoint at 10.10.5.10:9595. Both dev + prd Prometheus pick this up
  via the shared HelmRelease values.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  git push -u origin feat/cluster-agent-scrape
  gh pr create --title "feat(monitoring): scrape cluster-agent on NAS" --body "$(cat <<'EOF'
  ## Summary
  - Adds cluster-agent-nas scrape job to kube-prometheus-stack values
  - Both clusters scrape the same NAS endpoint independently

  ## Test plan
  - [ ] CI passes
  - [ ] After merge + Flux reconcile + tag promote: `cluster-agent-nas` job appears in dev Prometheus Targets UI
  - [ ] Same for prd

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```

- [ ] **Step 4: Merge after review, then promote to prd**

  ```sh
  gh pr merge --squash --delete-branch
  ./tools/promote-to-prd.sh patch
  ```

### Task 21: Grafana dashboard ConfigMap

**Files:**
- Create: `kube-infra/flux-cd/infrastructure/configs/base/dashboards/cluster-agent.json` (skeleton)
- Modify: `kube-infra/flux-cd/infrastructure/configs/base/dashboards/kustomization.yaml`

- [ ] **Step 1: Create a minimal dashboard JSON (real panels added in P1+)**

  Create `kube-infra/flux-cd/infrastructure/configs/base/dashboards/cluster-agent.json`:

  ```json
  {
    "annotations": { "list": [] },
    "editable": true,
    "graphTooltip": 0,
    "panels": [
      {
        "type": "stat",
        "title": "Agent uptime (seconds since last container restart)",
        "datasource": { "type": "prometheus" },
        "targets": [{ "expr": "time() - process_start_time_seconds{job=\"cluster-agent-nas\"}" }],
        "id": 1,
        "gridPos": { "h": 4, "w": 6, "x": 0, "y": 0 }
      },
      {
        "type": "stat",
        "title": "Total mode runs (last 24h)",
        "datasource": { "type": "prometheus" },
        "targets": [{ "expr": "sum(increase(cluster_agent_run_total[24h]))" }],
        "id": 2,
        "gridPos": { "h": 4, "w": 6, "x": 6, "y": 0 }
      },
      {
        "type": "stat",
        "title": "Anthropic spend (cumulative USD)",
        "datasource": { "type": "prometheus" },
        "targets": [{ "expr": "cluster_agent_anthropic_cost_usd_total" }],
        "id": 3,
        "gridPos": { "h": 4, "w": 6, "x": 12, "y": 0 }
      },
      {
        "type": "stat",
        "title": "Open findings (high severity)",
        "datasource": { "type": "prometheus" },
        "targets": [{ "expr": "cluster_agent_open_findings{severity=\"high\"}" }],
        "id": 4,
        "gridPos": { "h": 4, "w": 6, "x": 18, "y": 0 }
      },
      {
        "type": "timeseries",
        "title": "Mode runs by status (rate, 1h window)",
        "datasource": { "type": "prometheus" },
        "targets": [{ "expr": "rate(cluster_agent_run_total[1h])", "legendFormat": "{{mode}} {{status}}" }],
        "id": 5,
        "gridPos": { "h": 8, "w": 24, "x": 0, "y": 4 }
      }
    ],
    "schemaVersion": 39,
    "title": "Cluster Agent",
    "uid": "cluster-agent",
    "version": 1
  }
  ```

- [ ] **Step 2: Wrap in ConfigMap manifest**

  Create `kube-infra/flux-cd/infrastructure/configs/base/dashboards/cluster-agent-configmap.yaml`:

  ```yaml
  apiVersion: v1
  kind: ConfigMap
  metadata:
    name: cluster-agent-dashboard
    namespace: monitoring
    labels:
      # kube-prometheus-stack's grafana sidecar picks up dashboards
      # marked with this label.
      grafana_dashboard: "1"
  data:
    cluster-agent.json: |-
      __DASHBOARD_JSON_PLACEHOLDER__
  ```

  Then run from `kube-infra/`:
  ```sh
  python3 -c '
  import json, pathlib
  src = pathlib.Path("flux-cd/infrastructure/configs/base/dashboards/cluster-agent.json")
  dst = pathlib.Path("flux-cd/infrastructure/configs/base/dashboards/cluster-agent-configmap.yaml")
  payload = json.dumps(json.loads(src.read_text()), separators=(",", ":"))
  dst.write_text(dst.read_text().replace("__DASHBOARD_JSON_PLACEHOLDER__", payload))
  '
  rm flux-cd/infrastructure/configs/base/dashboards/cluster-agent.json
  ```

- [ ] **Step 3: Register in dashboards kustomization**

  Edit `kube-infra/flux-cd/infrastructure/configs/base/dashboards/kustomization.yaml`, add to `resources:`:
  ```yaml
    - cluster-agent-configmap.yaml
  ```

- [ ] **Step 4: Verify build**

  ```sh
  cd /Users/gunrak/github/kube-infra
  kubectl kustomize flux-cd/infrastructure/configs/base/dashboards/ \
    | grep cluster-agent
  ```

- [ ] **Step 5: Commit + PR + promote**

  ```sh
  git checkout -b feat/cluster-agent-dashboard
  git add flux-cd/infrastructure/configs/base/dashboards/cluster-agent-configmap.yaml \
          flux-cd/infrastructure/configs/base/dashboards/kustomization.yaml
  git commit -m "$(cat <<'EOF'
  feat(monitoring): cluster-agent Grafana dashboard

  Minimal P0 dashboard — uptime, run counts, spend, open findings.
  Richer per-mode panels land in P1+.

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  git push -u origin feat/cluster-agent-dashboard
  gh pr create --title "feat(monitoring): cluster-agent Grafana dashboard (P0)" --body "Skeleton dashboard; richer panels in P1+."
  ```

  After merge: `./tools/promote-to-prd.sh patch`.

---

## Phase 0h: Documentation (wiki)

### Task 22: Wiki runbook + policy + phase-history pages

**Files:**
- Create: `wiki/docs/runbooks/cluster-agent-runbook.md`
- Create: `wiki/docs/cluster-agent/policy.md`
- Create: `wiki/docs/cluster-agent/phase-history.md`
- Create: `wiki/docs/cluster-agent/prompts/.gitkeep`

- [ ] **Step 1: Create the runbook**

  Create `wiki/docs/runbooks/cluster-agent-runbook.md`:

  ```markdown
  # cluster-agent runbook

  Operational guide for the LLM-driven homelab SRE assistant.

  - **Spec:** [`truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md`](https://github.com/guntars-rakitko/truenas-infra/blob/main/docs/superpowers/specs/2026-05-23-cluster-agent-design.md)
  - **Repo:** [`truenas-infra/apps/cluster-agent/`](https://github.com/guntars-rakitko/truenas-infra/tree/main/apps/cluster-agent)
  - **Container host:** NAS (10.10.5.10)
  - **Metrics:** `http://10.10.5.10:9595/metrics`, scraped by both clusters

  ## Start / stop / restart

  ```sh
  ssh root@nas
  cd /mnt/tank/system/apps-compose/cluster-agent
  docker compose up -d
  docker compose stop
  docker compose restart
  docker compose logs -f --tail=100
  ```

  Or via `truenas-infra/manage.sh`:
  ```sh
  cd ~/github/truenas-infra
  ./manage.sh phase apps --apply
  ```

  ## Kill switches

  | Granularity | Mechanism |
  |---|---|
  | All modes | `docker compose stop cluster-agent` |
  | All modes (soft) | `doppler secrets set CLUSTER_AGENT_ENABLED=false --project infrastructure --config ops` then restart |
  | Per mode | `doppler secrets set CLUSTER_AGENT_DISABLED_MODES=A,J --project infrastructure --config ops` then restart |
  | Per-repo auto-merge | `doppler secrets set CLUSTER_AGENT_AUTOMERGE_DISABLED_REPOS=kube-infra --project infrastructure --config ops` then restart |

  ## Where to look when something looks off

  | Symptom | Look at |
  |---|---|
  | Container not running | `docker compose logs --tail=200`, then Loki `{app="cluster-agent"}` |
  | Mode not firing | `/health` endpoint → check `disabled_modes` and `enabled` |
  | LLM cost spike | Grafana "Cluster Agent" dashboard → "Anthropic spend" panel |
  | Bad finding | Loki: `{app="cluster-agent"} \|= "<finding_id>"` → full LLM rationale + tool chain |
  | Bad auto-merge | `kube-infra/tools/revert-auto-merge.sh 1h` |

  ## Token rotation

  K8s ServiceAccount tokens issued by `kubectl create token --duration=2160h` (90 days). Calendar reminder rotates every 90d:

  ```sh
  export KUBECONFIG=~/github/kube-infra/talos-os/kubeconfig-dev
  NEW_TOKEN=$(kubectl create token cluster-agent-readonly -n flux-system --duration=2160h)
  # Re-render kubeconfig (see Task 4 of P0 plan), base64, push to Doppler
  doppler secrets set CLUSTER_AGENT_KUBECONFIG_DEV="$(...)" --project infrastructure --config ops
  # Restart container
  ssh root@nas docker compose -f /mnt/tank/system/apps-compose/cluster-agent/docker-compose.yaml restart
  ```

  Same for prd + test-restore.

  ## OAuth credential refresh (Claude Agent SDK)

  The Agent SDK auto-refreshes the OAuth token in the container. If
  the token is revoked (laptop wipe, explicit Claude logout, etc.)
  the container can't self-recover.

  Recovery:
  ```sh
  claude login   # on operator laptop
  base64 < ~/.claude/.credentials.json   # output → Doppler
  doppler secrets set CLUSTER_AGENT_CLAUDE_OAUTH_CREDENTIALS="..." --project infrastructure --config ops
  # Restart container to re-read
  ```

  ## Phase history

  See [`cluster-agent/phase-history.md`](../cluster-agent/phase-history.md).
  ```

- [ ] **Step 2: Create policy.md (auto-synced placeholder)**

  Create `wiki/docs/cluster-agent/policy.md`:

  ```markdown
  # cluster-agent auto-merge policy

  **Source:** `truenas-infra/apps/cluster-agent/policy.yaml`
  (rendered into the system prompt at runtime).

  **Auto-sync:** This page is auto-generated from `policy.yaml` by
  the cluster-agent's deploy hook. Manual edits will be overwritten.

  ## Current policy

  (P0: policy.yaml not yet defined — placeholder. Full content lands
  in P1 when Mode I/J prompts are wired.)

  See spec § 5 for the textual specification.
  ```

- [ ] **Step 3: Create phase-history.md**

  Create `wiki/docs/cluster-agent/phase-history.md`:

  ```markdown
  # cluster-agent phase history

  Rollout audit log. One entry per phase transition. Operator-approved.

  | Phase | Entered | Exited | Operator | Note |
  |---|---|---|---|---|
  | P0 — Foundation | 2026-MM-DD | — | guntars-rakitko | (in progress) |
  ```

- [ ] **Step 4: Empty prompts/ dir**

  ```sh
  mkdir -p wiki/docs/cluster-agent/prompts
  touch wiki/docs/cluster-agent/prompts/.gitkeep
  ```

- [ ] **Step 5: Register pages in mkdocs**

  Open `wiki/mkdocs.yml`, find the `nav:` section, add:
  ```yaml
    - cluster-agent:
        - Runbook: runbooks/cluster-agent-runbook.md
        - Policy: cluster-agent/policy.md
        - Phase history: cluster-agent/phase-history.md
  ```
  (Match existing nav style.)

- [ ] **Step 6: Build + verify wiki**

  ```sh
  cd /Users/gunrak/github/wiki
  ./tools/deploy.sh --verify
  ```
  Expected: deploy succeeds, new pages accessible at https://wiki.w1.lv/runbooks/cluster-agent-runbook/.

- [ ] **Step 7: Commit**

  ```sh
  cd /Users/gunrak/github/wiki
  git add docs/runbooks/cluster-agent-runbook.md \
          docs/cluster-agent/ mkdocs.yml
  git commit -m "$(cat <<'EOF'
  docs(cluster-agent): runbook + policy + phase-history stubs

  Operator-facing docs for the cluster-agent (P0 foundation).

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  git push
  ```

---

## Phase 0i: Deploy + smoke

### Task 23: Deploy to NAS + verify smoke

**Files:**
- Read-only: `truenas-infra/manage.sh`

- [ ] **Step 1: Commit all remaining cluster-agent work + push**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  git push -u origin feat/cluster-agent-p0
  gh pr create --title "feat(cluster-agent): P0 foundation" --body "$(cat <<'EOF'
  ## Summary
  Implements P0 of the cluster-agent (truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md).

  No LLM calls in P0 — pure foundation: container, /health, /metrics,
  MCP tool scaffolding for kubectl/loki/prometheus/alertmanager/mc/gh,
  SQLite state + dedup, scheduler skeleton, all kill switches.

  ## Companion PRs
  - kube-infra: RBAC (merged in Task 3)
  - kube-infra: scrape config (Task 20)
  - kube-infra: dashboard (Task 21)

  ## Test plan
  - [x] All unit tests pass (`pytest tests/`)
  - [ ] After merge + `manage.sh phase apps --apply`, verify on NAS:
        `docker compose ps cluster-agent` → Running
        `curl http://10.10.5.10:9595/health` → 200, status: ok
        `curl http://10.10.5.10:9595/metrics` → 200, cluster_agent_*
  - [ ] Both clusters' Prometheus shows `cluster-agent-nas` job UP
  - [ ] Grafana "Cluster Agent" dashboard renders

  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  EOF
  )"
  ```

- [ ] **Step 2: Merge after review**

  ```sh
  gh pr merge --squash --delete-branch
  ```

- [ ] **Step 3: Deploy to NAS**

  ```sh
  cd /Users/gunrak/github/truenas-infra
  ./manage.sh phase apps --apply
  ```
  Watch for cluster-agent in the planned changes; confirm.

- [ ] **Step 4: Smoke-test from laptop**

  ```sh
  curl -s http://10.10.5.10:9595/health | jq
  curl -s http://10.10.5.10:9595/metrics | grep cluster_agent_run_total
  curl -s http://10.10.5.10:9595/ | jq
  ```
  Expected:
  - `/health` returns 200 with `status: ok`, `enabled: true`
  - `/metrics` returns text/plain with `# HELP cluster_agent_run_total ...`
  - `/` returns the basic identity JSON

- [ ] **Step 5: Verify both clusters scrape the NAS**

  ```sh
  for ctx in dev prd; do
    export KUBECONFIG=/Users/gunrak/github/kube-infra/talos-os/kubeconfig-$ctx
    echo "=== $ctx ==="
    kubectl exec -n monitoring prometheus-kube-prometheus-stack-prometheus-0 -c prometheus -- \
      wget -qO- http://localhost:9090/api/v1/targets 2>&1 \
      | python3 -c "import json,sys; d=json.load(sys.stdin); print([t['health'] for t in d['data']['activeTargets'] if 'cluster-agent' in t['labels'].get('job','')])"
  done
  ```
  Expected: `['up']` for both clusters.

- [ ] **Step 6: Verify Grafana dashboard renders**

  Open https://grafana-dev.w1.lv/d/cluster-agent — confirm panels show data (uptime > 0, others may be 0 since no modes are running yet — that's expected for P0).

- [ ] **Step 7: 3-day smoke watch (P0 advancement gate)**

  Set a 3-day calendar reminder. Confirm at the end of day 3:
  - Container hasn't restarted (`docker compose ps` shows uptime ≥ 3d)
  - `/health` returns `status: ok` whole time
  - Prometheus targets remain UP
  - No errors in Loki: `{app="cluster-agent"} |= "ERROR"` should be empty
  - Update `wiki/docs/cluster-agent/phase-history.md`: mark P0 exited, P1 entered (or held)

### Task 24: P0 retrospective entry

**Files:**
- Modify: `wiki/docs/cluster-agent/phase-history.md`

- [ ] **Step 1: Add P0 retrospective**

  After 3-day smoke is clean, edit `wiki/docs/cluster-agent/phase-history.md`:

  ```markdown
  | P0 — Foundation | 2026-MM-DD | 2026-MM-DD | guntars-rakitko | All smoke checks green. Container uptime > 3d. /metrics being scraped by both clusters. Dashboard rendering. Ready to author P1 plan. |
  ```

  Also note any surprises encountered during P0 (the unknown-unknowns that the next phase's plan should account for).

- [ ] **Step 2: Deploy wiki update**

  ```sh
  cd /Users/gunrak/github/wiki
  git add docs/cluster-agent/phase-history.md
  git commit -m "$(cat <<'EOF'
  docs(cluster-agent): mark P0 complete

  Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
  EOF
  )"
  ./tools/deploy.sh --verify
  git push
  ```

---

## Done with P0

P1 plan (Mode A on dev, sandbox repo) will be written separately by the
`writing-plans` skill when P0 is verified green.

---

## Plan Self-Review

**Spec coverage:** Every section of the spec that's relevant to P0 has at least one task:
- § 1 (overview), § 2 (scope), § 3.1 (where it lives) → Task 7 (compose)
- § 3.2 (what it talks to) → Tasks 12-16 (MCP tools)
- § 3.3 (reporting surfaces) → wiki tasks (22), GH tool (15)
- § 3.4 (Grafana) → Tasks 17, 20, 21
- § 3.5 (state) → Task 5 (MinIO bucket), Task 10 (schema)
- § 3.6 (auth) → Tasks 7-8 (compose configs:), Pre-3 (claude login)
- § 4.1-4.5 (agent loop, prompts, tools, schema, budgets) → P0 lands tools (12-16) + schema (11); prompts + loop deferred to P1
- § 5 (auto-merge policy) → P0 leaves placeholder (`policy.yaml` lands in P1 when Mode I/J wire up)
- § 6 (security) → Tasks 1-4 (RBAC), 7 (hardening), 12 (audit), 22 (token rotation runbook)
- § 7 (testing + rollout) → Tasks 9-19 (TDD throughout), 23-24 (deploy + retrospective)

**Placeholder scan:** No "TBD" / "TODO" in actionable steps. The wiki phase-history entry has `2026-MM-DD` placeholders by design (operator fills in when the phase transitions actually happen).

**Type consistency:** `Finding` / `Evidence` / `AutoAction` consistent between schema (Task 11) and dedup (Task 10). `DedupAction` API: `DedupAction.create` / `.comment` / `.reopen` sentinel-style attributes, same usage in tests and production. `Scheduler.add_mode(mode, func, trigger, **trigger_kwargs)` signature consistent. Audit decorator `@audit(tool=..., redact=...)` consistent across all 12+ tools.

**Spec requirements without a task:** None for P0 scope. All P1+ modes (A/B/D/E/F/G/H/I/J) deferred to phase-specific plans.
