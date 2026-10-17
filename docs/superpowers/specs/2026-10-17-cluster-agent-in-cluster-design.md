# cluster-agent — move into the clusters (one per cluster, own repo, CI-built image)

**Status:** Draft — 2026-10-17
**Operator decisions (2026-10-17):**
- Run the agent **inside each cluster**, one instance per cluster. Rejected: staying on the NAS,
  and one instance in msa2-prd that reads msa2-dev too (§ 3.1).
- The code moves to a **new repo, `guntars-rakitko/cluster-agent`**, with its git history and its
  design documents (§ 3.2).
- Each cluster's `state.db` **starts empty** (§ 3.3).
- The NAS app, its token account, the `grafana-nas` port and the NAS-side code are **deleted at
  cutover**, not kept stopped for a week (§ 3.4).
- The legacy OAuth path goes ("it never worked, only normal Claude API"): `LLM_AUTH_MODE`,
  `CLAUDE_CODE_OAUTH_TOKEN` and the bearer-token code.
- This is the first of two specs. The second moves the agent to Claude Opus 5.5 through the Claude
  Agent SDK and adds a verify-before-filing stage, on the platform built here (§ 10).

**Amends:** [`2026-05-23-cluster-agent-design.md`](2026-05-23-cluster-agent-design.md) § 3.1
(where it lives), § 3.2 (what it talks to) and § 3.6 (authentication). The routing of
[`2026-07-06-cluster-agent-digest-graduation.md`](2026-07-06-cluster-agent-digest-graduation.md)
(findings to `kube-infra`, summaries to `cluster-agent-digest`) is unchanged.
**Implementation homes:** the new `cluster-agent` repo (code, image, CI); `kube-infra`
(`flux-cd/apps/cluster-agent`, RBAC, network policy, monitoring); `truenas-infra` (removal);
`wiki` (runbook and pages); `dotfiles` (the workspace repo table).

---

## 1. Why

The agent's job is a daily read of each cluster's alerts, logs and changes. Everything it reads
lives in the clusters, yet it runs on the NAS, and most of its operational trouble comes from
that:

1. **Its tokens expire, and expiry is invisible.** It reaches each cluster with a hand-minted
   ServiceAccount token. The first ones expired on 2026-08-21 and the agent produced nothing for
   14 days. The current ones were minted for a year and expire on 2027-09-04. Inside a cluster
   the pod's projected token is rotated by the kubelet, so this failure mode and the yearly
   re-mint both disappear.
2. **A change reaches both clusters at once.** One NAS container serves dev and prd. In a cluster,
   the dev instance runs a new image first, and prd gets the same digest after a dev check, as
   every other app here does.
3. **No CI.** truenas-infra has none, so the agent's 4,719 lines of tests run only on the
   operator's laptop. The new repo runs them on every PR.
4. **The deploy path is bespoke.** `manage.sh phase apps` uploads the code file by file, the
   container rebuilds its venv at start, a Doppler change needs an apply (not a redeploy), and
   files deleted from git stay on the NAS because TrueNAS 25.10 has no file-delete API (37 stale
   `.pyc` files today). A built image removes all of it.
5. **The NAS image can't take the next step.** The second spec runs the agent through the Claude
   Agent SDK. `claude-agent-sdk` 0.2.168 ships wheels only for glibc Linux (`manylinux_2_17`),
   macOS and Windows, because each bundles the `claude` CLI. There is no musl wheel, and the NAS
   container is `python:3.14-alpine`.
6. **NAS-only plumbing.** The `grafana-nas` NodePort and its network policy exist only for this
   agent, and so do the two NOPASSWD sudo lines for `docker restart/exec cluster-agent`.

**"If the cluster is down, it is down" (operator, 2026-10-17).** The NAS placement looks like
independence from the clusters, but it buys none that matters here:
- The digest is not the outage detector. Each cluster's `Watchdog` alert pings healthchecks.io
  every 60 s, and healthchecks.io pages by email and a separate Pushover app when the pings stop
  (kube-infra `CLAUDE.md`, the `watchdog-dms` receiver). That path survives a dead cluster.
- When a cluster is down, the NAS agent can't read it either. Its first step, the Alertmanager
  history through that cluster's apiserver, fails.
- The one thing the NAS gave was accidental: both Prometheus instances scrape the one container,
  so a failed dev run could alert from prd. The dead-man's switch already covers a down cluster.
- An agent that starts again after a reboot reports the reboot as a known event in its next
  digest, which is the digest's job.

## 2. Non-goals

- **No change in what the agent does.** Same model (`claude-sonnet-4-6`), prompts, signals,
  dedup, issue formats, routing and schedule (dev 06:00, prd 06:01 Europe/Riga).
- No new modes and no verify stage. Both come with the second spec.
- No change to the `cluster-agent[bot]` GitHub App, its installation or its permissions.
- No backup of `state.db`. Losing it costs dedup memory and nothing else, as today (truenas-infra
  `CLAUDE.md` § setup-minio-buckets.sh).
- No change to how Loki, Prometheus and Alertmanager are reached (§ 3.5).

## 3. Decisions

### 3.1 Placement: one agent per cluster

| Option | For | Against |
|---|---|---|
| **One per cluster (chosen)** | Self-rotating tokens; dev runs a change first; no cross-cluster credentials; the same app pattern as sms-gateway | Two instances to deploy (one manifest, two overlays); an agent can't report its own cluster's outage (the dead-man's switch does, § 1) |
| Stay on the NAS | Smallest change; one instance sees both | Every problem in § 1 stays, and the container must leave Alpine for the second spec anyway |
| One in msa2-prd reading both | One instance, as today | msa2-dev's token still expires by hand; no dev-first rollout of agent changes |

### 3.2 Code home: a new repo

`guntars-rakitko/cluster-agent` (private), like `sms-gateway` and `web-tracker`: own CI, own
Renovate, deployed by kube-infra. Rejected: keeping the code in truenas-infra (the NAS repo, which
the agent no longer runs on, has no CI) and putting it in kube-infra (GitOps config; app code and
image builds there would break the pattern every other app follows).

### 3.3 State: each cluster starts with an empty `state.db`

On 2026-10-17 no finding issue is open (`gh issue list -R guntars-rakitko/kube-infra --label
cluster-agent --state open` is empty). An empty database loses only the 7-day reopen link: a
finding closed in the last week that fires again files a new issue instead of the "fired again
after closure" comment. At most one duplicate, in the first week. Copying the NAS database into
both clusters was the alternative; it isn't worth a write step.

### 3.4 The NAS side is deleted at cutover

As soon as both clusters run the new agent and pass their checks (§ 5), the NAS app, its files,
the old token account and the NAS-only plumbing go, the same day. This revokes the long-lived
tokens at once. The cost is the rollback: after deletion it means redeploying the NAS app from git
history and minting new tokens (§ 5.5).

### 3.5 Loki, Prometheus and Alertmanager stay behind the apiserver proxy

The tools keep calling `{apiserver}/api/v1/namespaces/monitoring/services/<svc>:<port>/proxy/…`.
The pod's ServiceAccount gets the existing `cluster-agent-services-proxy` Role (`get` on exactly
those three services). This keeps the tool code unchanged, keeps that Role as the boundary, and
needs no new network-policy pairs into `monitoring` for them: the traffic is pod → apiserver, and
apiserver → monitoring is already allowed by monitoring's rule 18a
(`allow-{loki,prometheus,alertmanager}-ingress-from-apiserver`). That rule exists for exactly this
path, so it **stays** when the NAS-only rules go (§ 5.4). Direct Service access is a possible later
simplification, not part of this move.

### 3.6 Grafana annotations go to the in-cluster Service

The agent posts annotations to `kube-prometheus-stack-grafana.monitoring.svc` with its Grafana
service-account token, as it does today through the NodePort. This needs one network-policy pair
(agent egress, Grafana ingress, § 4.4). The `grafana-nas` NodePort, which exists only for the NAS
agent, is deleted at cutover.

### 3.7 Settings in git, secrets in Doppler

Today all 29 keys of Doppler `cluster-agent/prd` reach the container as env, rendered at deploy.
After the move, only secrets stay in Doppler. Model, schedule, repos, budget and switches become
manifest env: reviewed in PRs, versioned, and the prd minute offset is just its overlay. A change
to a secret restarts the pod through the Doppler operator's reload; a change to a setting is a
commit that Flux applies. § 4.5 places every key.

**The secrets move into Doppler `infrastructure`, the project every in-cluster app reads.** No
cluster holds a token for the `cluster-agent` project (kube-infra `bootstrap.sh`
`_doppler_token_specs` mints only `infra-<env>`, `infra-shr` and `giks-<env>`). The other apps'
DopplerSecrets read `infrastructure/<env>` and `infrastructure/shr` with the tokens the clusters
already have (sms-gateway's `per-cluster/msa2-dev/secrets/sms-gateway-dev.yaml`). The agent does
the same:
- the secrets both clusters share go in `shr` (the API key, the GitHub App);
- SES comes straight from shr's existing `SHARED_SES_W1_*` (the `ses-w1` pattern), which ends the
  hand-kept mirror in `cluster-agent/prd`;
- each cluster's Grafana token goes in its `infrastructure/<env>`.

The `cluster-agent` project is deleted at § 5.4.
- **The trade-off:** the dedicated project isolated these keys from the clusters' tokens. Both
  clusters now need them, so that isolation would buy nothing.
- **What it costs:** the shr token can read the agent's API key and GitHub App key. Anything that
  can read that token in `doppler-operator-system` can too, as it already can every other shared
  key.

### 3.8 CI on GitHub-hosted runners

Like sms-gateway's `ci.yml`. The repo sees a few PRs a month, so the hosted minutes are small
next to the budget kube-infra#1637 brought under the free 3,000 a month, cosign's keyless signing
uses the job's GitHub OIDC token, and no new ARC scale set is needed.

## 4. Design

### 4.1 The repo and the history move

- **History:** run `git filter-repo` on a fresh clone of truenas-infra, keeping
  `apps/cluster-agent/` (renamed to the repo root) and the agent's design documents (renamed to
  `docs/superpowers/specs/` and `docs/superpowers/plans/` in the new repo):
  - specs: `2026-05-23-cluster-agent-design.md`, `2026-05-26-cluster-agent-p3-log-mining.md`,
    `2026-05-26-cluster-agent-roadmap-reshape.md`,
    `2026-07-06-cluster-agent-digest-graduation.md`, `2026-07-16-finding-state-reconcile.md`, and
    this spec;
  - plans: `2026-05-23-cluster-agent-p0-foundation.md`, `2026-05-25-cluster-agent-p1-mode-a-enable.md`,
    and this spec's implementation plan.

  Push the result as the new repo's `main`. truenas-infra deletes the same paths in its retirement
  PR (§ 5.4) and keeps a pointer in `CLAUDE.md` and `README.md`.
- **Layout:** `main.py`, `src/cluster_agent/`, `prompts/`, `tests/`, `pyproject.toml`, `uv.lock`,
  `requirements.lock.txt`, `Dockerfile`, `.github/workflows/ci.yml`, `renovate.json`, `CLAUDE.md`
  (current state), `README.md`, `docs/`. The truenas-only files go: `docker-compose.yaml`, the
  `__pycache__` / `.pytest_cache` leftovers.
- **Settings:** default branch `main`, squash merges, the `test` job required before merge.

### 4.2 The image

- **Base:** `python:3.14-slim` (Debian trixie, glibc), pinned by digest.
- **Python dependencies:** a build stage runs
  `pip install --require-hashes --only-binary=:all: -r requirements.lock.txt` into `/venv`. The
  lock chain stays exactly as today (`pyproject.toml` → `uv.lock` → hashed `uv export`).
  `tests/test_requirements_lock.py` reads the Python minor from the `Dockerfile`'s `FROM` line
  instead of the compose `image:` tag, and checks for `manylinux` x86_64 or pure-Python wheels
  instead of `musllinux`. Its compose-specific checks (no inline pins in the startup script) go
  with the compose file.
- **kubectl:** `v1.36.4`, the clusters' version (`kubectl version` on both, 2026-10-17),
  downloaded from `dl.k8s.io/release/v1.36.4/bin/linux/amd64/kubectl` in the build and checked
  against a sha256 written in the Dockerfile (`8b8f088da2dab964f853b38464033b1be15ede2839eca751482357c45abdd05a`
  for v1.36.4). A version bump that forgets the checksum fails the build. Today's `apk add kubectl`
  pins nothing.
- **No sqlite3 CLI.** Inspecting `state.db` uses Python's `sqlite3` module (§ 7). One package
  fewer.
- **Runtime:** user `10001:10001`, code in `/app` (read-only at run time),
  `PYTHONPATH=/app/src`, `PATH=/venv/bin:…`, `CMD uvicorn main:app --host 0.0.0.0 --port 9595`.
  Nothing installs at start.

### 4.3 CI and Renovate

- **`ci.yml`**, following sms-gateway's:
  - `test` on every PR and every push to `main`: install uv (pinned), `uv sync --frozen --extra
    dev`, `uv run --extra dev pytest`. The job fails if pytest collects no tests (a run of 0 is a
    false clean).
  - `image` on push to `main`, after `test`: build `linux/amd64`, push
    `ghcr.io/guntars-rakitko/cluster-agent:<commit sha>`, sign it keylessly with cosign, verify
    the signature, and print the tag and digest to pin in kube-infra. `sigstore/cosign-installer`
    stays at v3.10.1 (cosign v2, classic `.sig`): kube-infra's Kyverno verifies that format, and
    v4 would fail it (kube-infra's cross-repo cosign rule, sms-gateway `ci.yml` header).
  - The image job smoke-tests the image: `python -c "import cluster_agent, fastapi, uvicorn"` and
    `kubectl version --client`.
  - Every action pinned by commit SHA.
- **The fourth signer.** kube-infra's standing cross-repo cosign rule says the signing pins of
  kube-infra, GIKS and sms-gateway are lifted together, never alone. This repo joins that list
  (kube-infra `CLAUDE.md` § "the cosign signing pins are CROSS-REPO"), and its own `renovate.json`
  caps `sigstore/cosign-installer` at `<4.0.0`, as the other three do.
- **Renovate:** the repo joins the self-hosted Renovate's repository list in kube-infra. Its own
  `renovate.json` covers:
  - the uv lock;
  - the Dockerfile base image;
  - kubectl, through a regex manager for the version (the checksum is updated by hand in the
    same PR);
  - the GitHub Actions.

  A Renovate lock PR still needs the re-export (`uv export`), and CI's `test` job now catches a
  stale one before merge.

### 4.4 The kube-infra app

The app is `flux-cd/apps/cluster-agent/{base,dev,prd}`, with sms-gateway's layout in every part.

**Namespace and its plumbing**, created by `infra-configs` ahead of the app, as for sms-gateway
and web-tracker, so the Doppler Role exists before the app's Kustomization runs (web-tracker's
namespace file explains the deadlock this avoids):
- `per-cluster/msa2-{dev,prd}/cluster-agent-namespace.yaml`: Pod Security `restricted` on all three
  labels, `prune: disabled`, the `part-of` and `env` labels. `scripts/ci/check-cluster-invariants.py`
  requires `restricted` for a namespace not in `PSA_EXCEPTIONS`.
- The `doppler-operator-secret-writer` Role and RoleBinding in the namespace (the late half in
  `per-cluster/msa2-<env>/doppler-rbac-scoped.yaml`). `scripts/ci/check-bootstrap-order.py`
  checks the pairing.
- A `ghcr-readonly` pull secret in the namespace (`secrets/ghcr-readonly-cluster-agent-<env>.yaml`,
  from shr's `SHARED_GHCR_DOCKERCONFIGJSON`, like web-tracker's). The repo and its GHCR package
  are private.

**Identity.** ServiceAccount `cluster-agent`, shipped in the app. Its token is the
kubelet-projected one, rotated on its own (an hour by default).
- The existing `cluster-agent-readonly` ClusterRole and the `cluster-agent-services-proxy` Role
  (in `monitoring`) are bound to it, in
  `flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml`. Both cluster-scoped and
  cross-namespace RBAC stay in `infra-configs`. A binding may name a ServiceAccount that doesn't
  exist yet, so `infra-configs` applying first is fine.
- ⚠ **Until § 5.4 both bindings name both subjects**: the new `cluster-agent/cluster-agent` and
  the old `flux-system/cluster-agent-readonly`. Re-pointing them outright would cut the NAS agent
  off before it stops, and would remove the rollback of § 5.5.
- At § 5.4 the old subject and the `flux-system/cluster-agent-readonly` ServiceAccount go, and the
  long-lived tokens die with them.

**Deployment** `cluster-agent`:
- 1 replica, `strategy: Recreate` (SQLite has one writer, and the volume is RWO).
- `imagePullSecrets: ghcr-readonly`. `automountServiceAccountToken: true` on this pod, which needs
  its token. sms-gateway's pods turn it off.
- Pod security: `runAsNonRoot`, user and group 10001, `fsGroup` 10001, seccomp `RuntimeDefault`.
  The container has a read-only root filesystem, no privilege escalation and no capabilities.
- Volumes: the PVC at `/data` (`STATE_DB_PATH=/data/state.db`), and an `emptyDir` at `/tmp` for
  kubectl's cache (`KUBECACHEDIR=/tmp/kube-cache`, `HOME=/tmp`).
- Resources: requests 50m CPU and 256Mi; limits 1 CPU and 1Gi, the NAS container's limits.
  Kyverno's `require-resource-limits-vpol` needs the CPU request and the memory limit.
- Probes: readiness and liveness on `GET /health` port 9595. The digest runs in APScheduler's
  background thread, so `/health` keeps answering during a run.
- Env: the settings of § 4.5 in the base, and `CLUSTER_KEY` and `DAILY_DIGEST_MINUTE` in each
  overlay. App Kustomizations have no postBuild substitution, so per-cluster values are overlay
  patches. The secrets come through `envFrom` the two Doppler-managed Secrets (below). The
  Doppler operator's reload annotation restarts the pod when a secret changes.

**PVC** `cluster-agent-data`: 1Gi, local-path, RWO.

**Metrics.** A PodMonitor in the app, as sms-gateway ships, with a relabel that sets
`job="cluster-agent"`. A PodMonitor's default job label is `<namespace>/<name>`, and every alert
selects on `job`, so this relabel is load-bearing (§ 4.7). The agent's PrometheusRule and dashboard
stay in `infra-configs` where they are.

**Network policy**, the sms-gateway split:
- The app ships a namespaced CNP with:
  - default deny;
  - DNS;
  - egress to the kube-apiserver on 6443, for the services proxy and kubectl (monitoring's rule 3
    shape);
  - egress to the world on 443 (the Anthropic and GitHub APIs) and 587 (SES SMTP,
    `email-smtp.eu-north-1.amazonaws.com`);
  - ingress from Prometheus (`monitoring`, `app.kubernetes.io/name: prometheus`) on 9595, the
    shape of sms-gateway's `allow-app-ingress`. Prometheus's egress half already exists:
    monitoring's rule 4, `allow-prometheus-scrape-cluster`, reaches every pod.
- The one cross-namespace flow is a CiliumClusterwideNetworkPolicy pair, egress on the source and
  ingress on the destination: `cluster-agent` → Grafana (`app.kubernetes.io/name: grafana`) on
  `:3000`, the port behind the Service's 80, for annotations. It goes in
  `per-cluster/msa2-{dev,prd}/ciliumclusterwidenp-cluster-agent.yaml`, like
  `ciliumclusterwidenp-sms-gateway.yaml`.
- ⚠ Check Grafana's ingress against monitoring's rule 21 when writing the pair. Its `ingressDeny`
  keeps monitoring pods other than Prometheus off Grafana `:3000`, and a deny always beats an
  allow.
- ⚠ No FQDN rules. `toFQDNs` egress silently drops everything on these clusters (found with
  w1-db's off-site dump, kube-infra#1234), and the GitHub and AWS address ranges are too large and
  changeable for a CIDR list. So "the world on 443 and 587" is the narrowest egress available, the
  trade-off GIKS's namespace already documents (kube-infra `docs/AUDIT-REPORT.md`). The NAS
  container had no egress limits at all.

**Secrets** (§ 3.7). Two DopplerSecrets per cluster in `doppler-operator-system`, each with an
explicit key list (a kube-infra invariant: a DopplerSecret without one syncs its token's whole
config) and `managedSecret` in `cluster-agent`. Registered in `secrets/kustomization.yaml`:
- `cluster-agent-shr`, token `doppler-token-infra-shr`, config `shr`: the API key, the GitHub App
  and SES, renamed by `processors.asName` to the env names the code reads (the `ses-w1` file's
  shape);
- `cluster-agent-<env>`, token `doppler-token-infra-<env>`, config `<env>`: that cluster's Grafana
  token.

**Image verification.** Nothing verifies a new image today unless a policy names it.
`verify-giks-images` covers `giks-*` only, and there is no catch-all for
`ghcr.io/guntars-rakitko/*`. So the move adds sms-gateway's pair:
- `per-cluster/msa2-{dev,prd}/kyverno-policy-verify-cluster-agent.yaml`: Enforce, glob
  `ghcr.io/guntars-rakitko/cluster-agent*`, `verifyDigest: true`, registry credentials from
  `kyverno/ghcr-readonly`, cosign keyless with issuer `token.actions.githubusercontent.com`,
  subject `^https://github\.com/guntars-rakitko/cluster-agent/\.github/workflows/ci\.yml@refs/heads/main$`,
  Rekor `rekor.sigstore.dev`;
- its dev-only Audit `ImageValidatingPolicy` twin.

The legacy ClusterPolicy joins kube-infra#1335's migration map: one more entry in the
`CLAUDE.md` § "Kyverno is held on 1.19.x" table. The repo also becomes the fourth signer under
the cross-repo cosign rule (§ 4.3).

**Flux.** A `cluster-agent` Kustomization in `clusters/msa2-{dev,prd}/apps.yaml`, path
`./flux-cd/apps/cluster-agent/<env>`, `dependsOn: infra-configs`, `wait: true`.

**Image pins.** The dev overlay pins `newTag` (the commit sha) and `digest`. The prd overlay gets
the digest dev already ran, after a dev check and the operator's OK, never a fresh build (the
sms-gateway rule). Tag and digest change together, since the verify policy requires the digest.

### 4.5 Where each Doppler key goes

| Key (Doppler `cluster-agent/prd` today) | After the move |
|---|---|
| `ANTHROPIC_API_KEY` | `infrastructure/shr` as `SHARED_CLUSTER_AGENT_ANTHROPIC_API_KEY`, renamed back by the DopplerSecret |
| `GH_APP_ID`, `GH_APP_INSTALLATION_ID`, `GH_APP_PRIVATE_KEY` | `infrastructure/shr` as `SHARED_CLUSTER_AGENT_GH_APP_*`, renamed to the `CLUSTER_AGENT_GH_APP_*` names `tools/github.py` reads (the compose did that rename until now) |
| `GRAFANA_SA_TOKEN_DEV` / `GRAFANA_SA_TOKEN_PRD` | `infrastructure/dev` / `infrastructure/prd` as `CLUSTER_AGENT_GRAFANA_SA_TOKEN`, renamed to `GRAFANA_SA_TOKEN_<KEY>`; each cluster gets only its own |
| `SES_SMTP_HOST`, `SES_SMTP_PORT`, `SES_SMTP_USERNAME`, `SES_SMTP_PASSWORD`, `SES_FROM_DEFAULT` | **no copy:** read from shr's canonical `SHARED_SES_W1_*`, renamed as `ses-w1` does. The hand-kept mirror ends |
| `LLM_MODEL` (`claude-sonnet-4-6`) | manifest env, base |
| `DAILY_DIGEST_HOUR` (6), `DAILY_DIGEST_WINDOW_HOURS` (24), `DAILY_DIGEST_BUDGET_USD` (0.75) | manifest env, base |
| `DAILY_DIGEST_MINUTE` (0) | manifest env, overlay: dev 0, prd 1 (the cache-sharing offset; no offset arithmetic left in code) |
| `DIGEST_REPO`, `FINDINGS_REPO`, `FINDINGS_MIN_SEVERITY` (empty) | manifest env, base |
| `DIGEST_SUMMARY` (`email,issue`), `DIGEST_SUMMARY_EMAIL_TO` | manifest env, base |
| `ENABLED` (`true`), `DISABLED_MODES` | manifest env, base. `DISABLED_MODES` becomes empty: today's `B,D,E,F,G,H,I,J` lists modes that were never built |
| `CLAUDE_CODE_OAUTH_TOKEN`, `LLM_AUTH_MODE` | **removed** (the dead OAuth path) |
| `KUBECONFIG_DEV`, `KUBECONFIG_PRD` | **removed** (in-cluster ServiceAccount) |
| `MODE_A_CLUSTERS` | **removed** (one cluster per instance: `CLUSTER_KEY`) |
| `SANDBOX_REPO` | **removed** (a fallback unused since the 2026-07-06 graduation) |

### 4.6 Code changes

Everything else stays as it is.

- **One cluster per process.** `CLUSTER_KEY` (`dev` or `prd`) names the cluster this instance
  serves. `main.py` registers one digest job, for that key, at
  `DAILY_DIGEST_HOUR:DAILY_DIGEST_MINUTE`. A tool asked about the other key raises: with one
  cluster's credentials, that call is a bug.
- **In-cluster API access.**
  - `tools/k8s_proxy.py` takes the apiserver address from `KUBERNETES_SERVICE_HOST` /
    `KUBERNETES_SERVICE_PORT` and the CA from
    `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt`. It reads the token from
    `/var/run/secrets/kubernetes.io/serviceaccount/token` **on every call**: the kubelet rotates
    it, and a cached token would bring back the expiry outage hourly.
  - `tools/kubectl.py` runs kubectl without `--kubeconfig`, in its in-cluster mode, which reads
    the same files.
  - The base64 kubeconfig code goes from both.
- **Grafana.** `clusters.py` `GRAFANA_URLS` points both keys at
  `http://kube-prometheus-stack-grafana.monitoring.svc` (the Service's port 80). The docstring
  about kubeconfigs, NodePorts and the NAS's /24 goes. `tools/grafana.py` keeps reading
  `GRAFANA_SA_TOKEN_<KEY>`.
- **The OAuth path goes.** `main.py`'s `LLM_AUTH_MODE` routing and its checks, and `llm.py`'s
  `sk-ant-oat` bearer branch and `CLAUDE_CODE_OAUTH_TOKEN` read. The API key is sent as
  `x-api-key`, and startup fails clearly without it unless `ENABLED=false`.
- **The `SANDBOX_REPO` fallback goes** from `dispatch.py` and `daily_digest.py`.
  `FINDINGS_REPO` and `DIGEST_REPO` are required.
- **⚠ The agent must not mine its own logs.** In a cluster, the agent's stdout reaches Loki
  through Alloy, under `namespace="cluster-agent"`. Its audit lines quote its own LogQL, so they
  contain `panic`, `fatal`, `OOMKilled` and the other tripwire words, and the next digest's
  tripwires would match them. This is the same trap as Loki's own query log, which
  `_RECORD_EXCLUDE` already handles for `loki-0` (truenas-infra `CLAUDE.md`, daily-digest step
  3b). The ratio and tripwire queries exclude the agent's namespace, while each selector keeps a
  positive matcher (Loki rejects a selector of only negative matchers). The agent's own failures
  are covered by its metrics and alerts (§ 4.7).
- **Comments and docstrings** that describe the NAS (compose, `manage.sh`, `docker exec`, the
  Doppler-render path) are rewritten for the cluster: `scheduler.py`, `main.py`, `llm.py`,
  `clusters.py`, the tools.
- **Tests** follow:
  - one job for `CLUSTER_KEY`, and a cross-key tool call raises;
  - the token is re-read per call (a changed token file is used by the next call);
  - the selectors exclude the agent's namespace and keep a positive matcher;
  - the wheel check is for glibc;
  - the OAuth tests go.

### 4.7 Monitoring

- **Alert rules** (kube-infra `prometheus-rules-cluster-agent.yaml`). All four alerts select
  `job="cluster-agent-nas"`: `ClusterAgentDown`, `ClusterAgentNoSuccessfulRun`,
  `ClusterAgentRunsFailing` and `ClusterAgentDispatchErrors`.
  - That becomes `job="cluster-agent"`, in the dev PR. It reaches each cluster when that cluster
    moves, because msa2-dev reconciles kube-infra's `dev` branch and msa2-prd reconciles `main`:
    - dev's rules switch with dev's agent;
    - prd keeps watching the NAS agent, then switches with the promotion that brings its own
      agent.
  - ⚠ The rules must **not** match both job labels. Until § 5.4, dev's Prometheus still scrapes
    the NAS agent, which then runs prd's digest. A selector over both jobs would count those prd
    successes on dev, and hide a failing dev agent.
  - `ClusterAgentDown` gains `or absent(up{job="cluster-agent"})`. A PodMonitor has no target when
    the Deployment is gone (pruned, or scaled to 0), and a rule over a series that doesn't exist
    can't fire. The NAS scrape's static target never had this gap.
  - **The rules assume one process with two runs a day.** Their wording ("2 runs/day, dev 06:00 /
    prd 06:01"; `ClusterAgentRunsFailing`'s "2 = every run failed, 1 = one cluster") and the
    header's "one process scraped by both clusters" become one run a day per process. Each
    expression is re-checked against that before the PR. The mechanisms stay: the counter, not
    the gauge; `OR on() vector(0)`; the 26 h window; the process-uptime clause.
  - Every description's ssh, `midclt` and `curl 10.10.10.10:9595` recipe becomes its `kubectl` and
    Loki equivalent. `ClusterAgentDispatchErrors`'s `grafana-nas` text becomes the in-cluster path.
- **Dashboard** (`grafana-dashboards/cluster-agent.yaml`): the uptime panel's job filter changes
  the same way. The spend thresholds (50/75 for one process covering both clusters) become
  per-cluster ones; the second spec sets them from its measured cost.
- **The NAS scrape** goes at § 5.4:
  - the `cluster-agent-nas` job in `kube-prometheus-stack.yaml`'s `additionalScrapeConfigs`
    (target `${CLUSTER_NAS_DATA_IP}:9595`);
  - the `CLUSTER_NAS_DATA_IP` cluster-env key on both clusters (`clusters/msa2-{dev,prd}/infrastructure.yaml`),
    whose only consumer is that scrape;
  - monitoring's rule 18 `allow-prometheus-egress-to-cluster-agent` (Prometheus → world `:9595`).
- **Logs:** the agent's logs and its audit lines are in Loki as `{namespace="cluster-agent"}`.
  Until now they existed only in the NAS container's log.

## 5. Cutover

### 5.1 Build

1. Create the repo, run the history move (§ 4.1), add the Dockerfile, `ci.yml`, `renovate.json`,
   `CLAUDE.md` and the § 4.6 code changes in one PR. CI is green.
2. Merge. The first image is built, signed and verified; record its tag and digest.

### 5.2 dev

3. Doppler (main session): add the § 4.5 keys to `infrastructure/shr`
   (`SHARED_CLUSTER_AGENT_*`) and to `infrastructure/dev` (`CLUSTER_AGENT_GRAFANA_SA_TOKEN`).
   Copy the values from `cluster-agent/prd` without printing them: pipe each value on stdin
   (`doppler secrets get … --plain | doppler secrets set … --silent`). `doppler secrets set` drops
   one trailing newline from stdin, which matters for the PEM key, so compare the sha256 of source
   and copy rather than trusting the write.
4. A kube-infra PR to `dev`, with everything below for msa2-dev only (msa2-prd's twins come with
   § 5.3):
   - the namespace file, its Doppler RBAC and its pull secret;
   - the new subject added to both bindings, beside the old one (§ 4.4);
   - both DopplerSecrets;
   - the Kyverno pair;
   - the CCNP pairs;
   - the app (namespaced CNP, PodMonitor, dev's image pin);
   - the rules and the dashboard on `job="cluster-agent"`, which reach prd only with its
     promotion (§ 4.7).
5. At the merge, take dev off the NAS: `MODE_A_CLUSTERS=prd` in Doppler and
   `./manage.sh phase apps --only cluster-agent --apply`, so dev is not digested twice. Confirm
   that `process_start_time_seconds` changed (a `/health` reply alone proves nothing).
6. Check on msa2-dev:
   - the pod is Ready (Kyverno admitted the signed image);
   - `up{job="cluster-agent"} == 1` in dev's Prometheus;
   - a digest run files its summary in `cluster-agent-digest` labelled `msa2-dev`. Either the
     06:00 run, or a manual fire from the main session
     (`kubectl -n cluster-agent exec deploy/cluster-agent -- python -c …`, which is a cluster
     write and is the main session's alone);
   - the run's audit lines are in Loki under `namespace="cluster-agent"` (positive control: they
     contain tripwire words), and the next digest's log patterns include none of them;
   - an apiserver-proxy call succeeds after more than an hour of pod uptime, so it ran on a
     rotated token (the second day's run, or a second manual fire).

### 5.3 prd

7. Add msa2-prd's twins on `dev` (its namespace, Doppler RBAC, pull secret, DopplerSecrets,
   Kyverno pair, CCNP pairs and the `infrastructure/prd` Grafana key), with the digest dev ran in
   prd's overlay. Then promote to `main`.
8. At the merge, the NAS stops digesting prd too: `MODE_A_CLUSTERS` set empty and applied, as in
   step 5. The NAS app then runs nothing until § 5.4 deletes it.
9. The same checks on msa2-prd.

### 5.4 Retire the NAS side (the same day)

10. **The NAS, live (main session):**
    - Delete the TrueNAS app with `midclt call app.delete cluster-agent`. Removing its
      `config/apps.yaml` entry would not do it: `ensure_custom_app` creates and updates, and
      never deletes.
    - ⚠ Confirm `/mnt/.ix-apps/app_configs/cluster-agent/` is gone after the delete. It holds the
      rendered compose with **every secret** substituted in.
    - Delete `/mnt/tank/system/apps-config/cluster-agent/{code,data,venv}`. The files are
      root-owned and TrueNAS has no file-delete API, so use the API-plus-root-cron recipe
      (truenas-infra `CLAUDE.md`), or the operator's `sudo`.
    - Remove the orphan `cluster-agent` bucket on both MinIO instances: confirm it is empty, then
      `mc rb`.
11. **The truenas-infra PR** removes:
    - `apps/cluster-agent/` and the moved documents;
    - the `config/apps.yaml` entry;
    - in `src/truenas_infra/modules/apps.py`: the `_DOPPLER_KEYS_PER_APP` and
      `_DOPPLER_PROJECT_PER_APP` entries, the `CLUSTER_AGENT_*` constants, the upload dispatch,
      `_requirement_pins`, `_verify_cluster_agent_requirements_export`,
      `_ensure_cluster_agent_config_via_ctx` and `_git_ls_files` (only the upload used them);
    - their tests in `tests/test_apps.py`. Three of them read the real tree and the real `uv`, so
      they fail once the directory is gone;
    - `tests/test_verify.py`'s `cluster-agent` app in its mock call sequences;
    - `scripts/render-cluster-agent-kubeconfigs.sh` and `tests/test_render_cluster_agent_kubeconfigs.py`;
    - the `pip_requirements` rule for `apps/cluster-agent/requirements.lock.txt` in `renovate.json`
      (the rule moves to the new repo);
    - the `cluster-agent` bucket from `setup-minio-{buckets,encryption,lifecycle}.sh`, added to
      `tests/test_minio_setup_scripts.py`'s `RETIRED_BUCKETS`.

    Also close the four open Renovate branches that edit `apps/cluster-agent/*`
    (`renovate/{prometheus_client-0.x,pydantic-2.x,tenacity-9.x,python-3.x}`). Renovate opens them
    again in the new repo.
12. **Operator, in the TrueNAS UI:** remove the NOPASSWD sudo lines
    `/usr/bin/docker restart cluster-agent` and `/usr/bin/docker exec cluster-agent *`.
13. **The kube-infra PR:**
    - Delete the `flux-system/cluster-agent-readonly` ServiceAccount and its subject in both
      bindings.
    - Delete the `grafana-nas` NodePort and monitoring's rule 18d `allow-grafana-ingress-from-nas`.
    - Delete the NAS scrape, the `CLUSTER_NAS_DATA_IP` keys and monitoring's rule 18 (§ 4.7).
      Rule 18a stays (§ 3.5).
    - Prove the old tokens are dead: a read with one of the NAS kubeconfigs answers 401.
14. **Doppler:** delete the `cluster-agent` project. Its last useful keys now live in
    `infrastructure` (§ 4.5).
15. **Memory:** the token-rotation reminder (due 2027-09-04) retires.

### 5.5 Rollback

- **Before § 5.4:** scale the cluster's Deployment to 0 (or suspend its Flux Kustomization), and
  give that cluster back to the NAS through `MODE_A_CLUSTERS`.
- **After § 5.4:** revert the truenas-infra retirement PR and redeploy the NAS app, then mint new
  tokens with `render-cluster-agent-kubeconfigs.sh` from git history (its `--apply` writes Doppler
  and redeploys). This is the price of § 3.4.

## 6. Touchpoints

What the move changes outside the agent's own code. These are from a read-only sweep of each
repo's default branch on 2026-10-17: kube-infra `dev` e5007c47, truenas-infra `main` 624e7487,
wiki `main` 31eb0d03, dotfiles `main` bc7d412e. Mentions that are only history (old plans and
specs, issue references) stay as they are and are not listed.

**kube-infra**
- **`flux-cd/infrastructure/configs/base/`**
  - `cluster-agent-rbac.yaml` (§ 4.4):
    - The ServiceAccount `flux-system/cluster-agent-readonly`, with `automountServiceAccountToken:
      false` because the agent ran off-cluster. It goes.
    - The ClusterRole stays. Its ClusterRoleBinding and the `monitoring` Role/RoleBinding for
      `services/proxy` move to the new subject.
    - The header (Doppler `KUBECONFIG_*`, the render script) is rewritten.
  - `prometheus-rules-cluster-agent.yaml` and `grafana-dashboards/cluster-agent.yaml`: § 4.7.
  - `grafana-nas-nodeport.yaml`: deleted (§ 3.6), with its `kustomization.yaml` entry.
  - `ciliumnetworkpolicy-monitoring.yaml`:
    - rules 18 and 18d go (§ 4.7, § 5.4); rule 18a stays (§ 3.5);
    - rule 18c keeps its other consumers, and its prose loses the NAS;
    - the header and rule 21's comment stop naming the NAS as a Grafana client.
  - `kustomization.yaml`: its comment at the RBAC entry links the moved design spec.
- **`flux-cd/infrastructure/helmreleases/kube-prometheus-stack.yaml`:** the `cluster-agent-nas`
  scrape job goes.
- **`flux-cd/clusters/msa2-{dev,prd}/`**
  - `infrastructure.yaml`: `CLUSTER_NAS_DATA_IP` goes.
  - `apps.yaml`: the new Kustomization.
- **`flux-cd/infrastructure/configs/per-cluster/msa2-{dev,prd}/`**, new files: the namespace,
  `ciliumclusterwidenp-cluster-agent.yaml` (the Grafana pair), `kyverno-policy-verify-cluster-agent.yaml` (and dev's
  Audit twin), `secrets/cluster-agent-{shr,<env>}.yaml` and
  `secrets/ghcr-readonly-cluster-agent-<env>.yaml`. Changed: the kustomization entries,
  `doppler-rbac-scoped.yaml`, and `secrets/kustomization.yaml`. Prose only:
  `msa2-prd/ciliumnetworkpolicy-giks-v1-box.yaml`'s comment names `CLUSTER_NAS_DATA_IP`.
- **`flux-cd/apps/cluster-agent/`:** new (§ 4.4).
- **Renovate's repository list:** the new repo.
- **`CLAUDE.md`:**
  - § SSO architecture, "The cluster-agent's path, and who reaches Grafana :3000": the client
    list.
  - § Post-bootstrap operator tasks: the Grafana token re-mint recipe. It writes
    `cluster-agent/prd` `GRAFANA_SA_TOKEN_*`, names the token `cluster-agent-nas-<ts>` and says the
    port admits only 10.10.5.10; it now writes `infrastructure/<env>` and names the in-cluster
    path.
  - § Kyverno is held on 1.19.x: the per-policy map gains one entry.
  - The cross-repo cosign rule gains its fourth repo.
  - Related Repositories, § Monitoring's dashboard line, the MinIO bucket list and the NAS
    container list.
- **`docs/ARCHITECTURE-SNAPSHOT.md`:** the agent's placement and its scrape.

**truenas-infra:** everything in § 5.4 step 11, plus:
- `CLAUDE.md`:
  - § cluster-agent ops, about 700 lines, becomes a pointer;
  - Related Repositories, § Network's `:9595` note, § Planned Services (the row and the surviving
    apps), § API Configuration Approach's upload list, the bucket and ILM rows;
  - § SSH + sudo on NAS: the two NOPASSWD lines and the recipes that use them;
  - § File Structure.
- `README.md`'s phase-9 app list, and `manage.sh`'s "uv required for cluster-agent" comment.
- `scripts/setup-talos-shutdown-orchestrator.sh`: prose comparisons to the agent.

**wiki**
- Rewritten:
  - `docs/runbooks/cluster-agent-runbook.md`: its NAS start and stop, the switches,
    troubleshooting ("Agent logs are NOT in Loki"), token rotation, the observability RBAC, the
    deployment gotchas and the Grafana annotation path;
  - `docs/cluster-agent/index.md`: "Where it runs".
- Re-pointed:
  - `docs/cluster-agent/policy.md`: its truenas-infra links;
  - `docs/runbooks/msa2-rebuild-from-zero.md`: the Grafana re-mint through `manage.sh`;
  - `docs/runbooks/ses-setup.md`: the SES mirror, which ends;
  - `docs/runbooks/credentials-inventory.md`: the `cluster-agent/prd` row.
- Prose:
  - `docs/architecture/{ip-plan,network-policy-and-psa,secrets-flow,compute-estates}.md`;
  - `docs/runbooks/index.md` and `docs/runbooks/alerts/{index,minio-endpoint-down,loki-request-latency}.md`;
  - `docs/reference/links.md`.
- `sync-map.yaml` gains the new repo's `CLAUDE.md` → `docs/projects/cluster-agent.md`, as
  `sms-gateway/CLAUDE.md` is mapped. A mapping fails the sync when the local clone is missing, so
  clone first.
- `docs/cluster-agent/phase-history.md` gets a new entry.

**Links to the moved design spec.** These point at
`truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md` and are re-pointed to the
new repo in the same PRs:
- kube-infra's `cluster-agent-rbac.yaml`, `configs/base/kustomization.yaml` and the dashboard header;
- the wiki's runbook and `policy.md`.

**dotfiles** (`claude/workspace-CLAUDE.md`): the Applications table gains the repo. § "Stronger
enforcement, available but not built" names the `cluster-agent-readonly` ClusterRole, which keeps
its name, so it stays true.

**mikrotik-infra** (`CLAUDE.md`): two prose lines say the NAS hosts the agent.

## 7. Docs (in the same PRs)

- **New repo `CLAUDE.md`** (current state):
  - what the agent is and how it runs;
  - deploying a change: merge, then pin the digest in kube-infra dev, then promote the same digest;
  - where its logs, metrics and alerts are;
  - the switches, and settings versus secrets;
  - a manual fire through `kubectl exec`, the main session's alone;
  - reading `state.db` with `python -c "import sqlite3 …"`.

  Also a `README.md`, and the moved design history in `docs/`.
- **kube-infra `CLAUDE.md`:** the app's entry and a cluster-agent section (namespace, roles,
  network-policy pairs, alerts, pins), plus the sections § 6 lists. The NAS-era token warning
  goes.
- **truenas-infra `CLAUDE.md`:**
  - § cluster-agent ops becomes a short pointer to the new repo, with one line of history;
  - the Planned Services row goes;
  - the NOPASSWD list loses its two lines;
  - the `cluster-agent` bucket row goes;
  - the Related Repositories table names the new repo.
- **wiki:**
  - `docs/runbooks/cluster-agent-runbook.md` rewritten for the cluster;
  - the `docs/cluster-agent/` pages updated;
  - `sync-map.yaml` maps the new repo's `CLAUDE.md`, as it does for the other repos;
  - the architecture pages the inventory names are updated. Deploy with
    `./tools/deploy.sh --verify`.
- **dotfiles** `claude/workspace-CLAUDE.md`: the new repo in the Applications table.
- **mikrotik-infra** `CLAUDE.md`: its two lines about the NAS hosting the agent.
- **The 2026-05-23 design:** a note under § 3.1, § 3.2 and § 3.6 pointing here.

Each cross-repo PR links the others, as the workspace's documentation rule asks.

## 8. Testing

- **CI**, every PR: the existing suite plus the § 4.6 tests, the export check (CI now catches a
  stale `requirements.lock.txt` before merge), and a nonzero test count.
- **Image**, every merge: the smoke test of § 4.3, the cosign signature verified.
- **kube-infra's own CI** on each kube-infra PR: `validate` green, including
  `check-cluster-invariants.py` (Pod Security, DopplerSecret key lists) and
  `check-bootstrap-order.py` (the Doppler RBAC pairing).
- **In the cluster**, § 5.2 step 6 and § 5.3 step 9: health, scrape, a real digest, the logs in
  Loki without self-matches, and a call on a rotated token.
- **Alerts:** after the first run, `ClusterAgentNoSuccessfulRun` and `ClusterAgentRunsFailing`
  evaluate against the new job. Positive control: the `cluster_agent_run_total` series exists with
  `job="cluster-agent"`; a rule over a series that doesn't exist can't fire.

## 9. Risks

- **A node that is down at 06:00 misses that day's digest.** APScheduler's in-memory store doesn't
  know a fire was missed while the process was down. The dead-man's switch covers the outage, and
  the next digest covers its own 24 h. (Reading from the last successful run instead of a fixed
  window was declined on 2026-10-17: the log-ratio maths assumes 24 h.)
- **The dev cluster now holds the agent's secrets.** It also runs CI runners and Renovate. The
  Secret is namespaced and no other workload's ServiceAccount can read it. The GitHub App key can
  write issues, contents and pull requests on every repo the App is installed on (its declared
  permissions, `gh api /apps/cluster-agent`). The agent itself writes only issues and comments, in
  `kube-infra` and `cluster-agent-digest` (`tools/github.py`'s write allowlist). Narrowing the App
  to what the agent uses is an operator step in the second spec.
- **Egress is "the world on 443 and 587"**, not named hosts (no working FQDN policy here). It is
  still narrower than the NAS container, which had no egress policy.
- **The image is verified only because § 4.4 adds a policy.** Without it, an unsigned image would
  be admitted. The new legacy ClusterPolicy also adds one entry to kube-infra#1335's migration
  ahead of Kyverno 1.21. § 5.2 step 6 proves admission on dev before prd.
- **A wrong job label silences the agent's alerts.** A PodMonitor's default job is
  `cluster-agent/<name>`, not `cluster-agent`. § 4.4's relabel and § 4.7's per-cluster switch
  cover it, and § 8's positive control checks the series exists. So does an agent with no target
  at all, which `ClusterAgentDown`'s `absent()` clause covers.
- **The agent's secrets join shr** (§ 3.7). What the shr token can read, the agent's API key and
  GitHub App key now are too.
- **The new repo is a fourth cosign signer.** A cosign bump in any one of the four repos breaks
  that repo's images (the cross-repo rule, § 4.3).

## 10. What comes next

The second spec, on this platform:
- Claude Opus 5.5 through the Claude Agent SDK, with structured output and refusal handling;
- an alert on `aborted_budget`;
- a verify-before-filing stage, which re-checks each new finding with read-only tools and drops
  what it disproves, listing every drop in the summary.

Its decisions so far (2026-10-17):
- a finding that is disproved, explained or inconclusive is dropped and listed;
- it goes live from day one, with a `VERIFY_MODE` off switch;
- only new findings are checked;
- the verifier may read kube-infra's merged PRs and commits;
- a check that cannot run files the finding as "⚠ Not verified".
