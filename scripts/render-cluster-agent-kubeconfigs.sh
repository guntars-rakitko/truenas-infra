#!/usr/bin/env bash
# render-cluster-agent-kubeconfigs.sh — mint fresh ServiceAccount tokens
# for the cluster-agent and render them into kubeconfigs for Doppler.
#
# ## Why this exists
#
# The wiki runbook `cluster-agent-runbook.md` § Token rotation has
# referenced this exact path since it was written, but the script was
# never committed. Rotation was therefore six manual steps with no
# verification, and on 2026-08-21 the tokens expired unnoticed: every
# Mode A run failed for 14 days while `/health` kept returning `ok`,
# because nothing checked the credential and nothing alerted on the
# agent's own error counter. (Alerting was added the same day in
# kube-infra `prometheus-rules-cluster-agent.yaml`.)
#
# ## What it does
#
#   1. Mints a token per cluster via the TokenRequest API.
#   2. VERIFIES THE ACTUAL EXPIRY of what the apiserver handed back,
#      rather than assuming the requested duration was granted.
#   3. Proves the token works with a real read against each cluster.
#   4. Renders kubeconfigs matching the shape the agent expects.
#   5. With --apply: writes both to Doppler, re-renders the app config via
#      manage.sh (NOT app.redeploy — see the apply section), waits for the
#      new process, then re-verifies the deployed credential.
#
# Step 2 is the point. `--service-account-max-token-expiration` is NOT
# set on either apiserver, so the granted lifetime is whatever the
# server decides — never assume you got what you asked for. Trusting the
# request is how a "90-day" token turned into a silent 14-day outage.
#
# ## Usage
#
#   ./scripts/render-cluster-agent-kubeconfigs.sh              # render + verify only
#   ./scripts/render-cluster-agent-kubeconfigs.sh --apply      # + Doppler + redeploy
#   ./scripts/render-cluster-agent-kubeconfigs.sh --duration 8760h
#
#   # MS-A2 cutover: point ONE cluster at its new admin kubeconfig
#   ./scripts/render-cluster-agent-kubeconfigs.sh \
#     --prd-kubeconfig ~/github/kube-infra/talos-os/kubeconfig-msa2-prd
#
# ## Which cluster each key points at
#
# `dev` / `prd` here are the agent's two Doppler keys (KUBECONFIG_DEV /
# KUBECONFIG_PRD), not a statement about hardware. Each is minted from its
# own admin kubeconfig:
#
#   --dev-kubeconfig PATH   default $KUBECONFIG_DIR/kubeconfig-dev (kub-dev)
#   --prd-kubeconfig PATH   default $KUBECONFIG_DIR/kubeconfig-prd (kub-prd)
#
# The defaults are the Q170S1 clusters. At the MS-A2 cutover (kube-infra
# plan docs/superpowers/plans/2026-09-23-msa2-phase-2-build.md § Cutover
# inventory row 16) each key moves to kubeconfig-msa2-<env> one cluster at a
# time; in the mixed period prd = msa2-prd while dev is still kub-dev, so
# pass only --prd-kubeconfig. Re-minting the unchanged cluster as well is
# harmless (a fresh token for the same cluster).
#
# ## Pre-flight (runs before ANY token is minted)
#
# For each key: the kubeconfig exists, its server is printed, and so are the
# nodes it reaches (a read), so you see which cluster each key will point
# at. Then it REFUSES:
#
#   - a server on an MS-A2 BUILD address (10.10.5.17 prd / .18 dev). Talos
#     makes the cluster endpoint the service-account token ISSUER, so the
#     re-address to the final address (.11 / .12) invalidates every token
#     issued before it (plan D12, row 16(a)). A token minted at the build
#     address works until that re-address and then fails every run — the
#     exact silent blindness this script exists to end. Re-mint only after
#     the box has moved. --allow-build-address overrides, for a deliberate
#     short-lived test.
#   - dev and prd resolving to the SAME server: the agent would digest one
#     cluster twice under two names and watch nothing on the other.
#
# After minting, the token's own `iss` claim is checked against the build
# addresses too. The issuer is the real invariant; the kubeconfig's server
# is only a proxy for it (a hostname server would slip past the first check).
#
# ## Prereqs
#
#   - kubectl with admin kubeconfigs at kube-infra/talos-os/kubeconfig-{dev,prd}
#     (override the directory with KUBECONFIG_DIR, or one cluster with
#     --dev-kubeconfig / --prd-kubeconfig)
#   - doppler CLI authenticated (only for --apply)
#   - ssh to the NAS as truenas_admin, and a working truenas-infra
#     manage.sh (Doppler infrastructure/ops access) — only for --apply
#
# ## Verification after running with --apply
#
#   curl -s http://10.10.10.10:9595/health
#   # then wait for the next 06:00/06:01 Europe/Riga run, and confirm a
#   # success series now exists:
#   curl -s http://10.10.10.10:9595/metrics | grep cluster_agent_run_total

set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────────
KUBECONFIG_DIR="${KUBECONFIG_DIR:-$HOME/github/kube-infra/talos-os}"
SA_NAME="cluster-agent-readonly"
SA_NAMESPACE="flux-system"
DOPPLER_PROJECT="cluster-agent"
DOPPLER_CONFIG="prd"
NAS_SSH="${NAS_SSH:-truenas_admin@nas.w1.lv}"
NAS_APP="cluster-agent"
# Repo root — manage.sh lives here and is the ONLY thing that pushes new
# Doppler values into the app config (see the apply section).
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 8760h = 1 year. The previous 2160h (90 days) expired unnoticed; a
# longer lifetime does not fix the silence (the alerts do) but it does
# reduce how often this ceremony has to happen. The apiserver may grant
# less — step 2 reports what was ACTUALLY granted, which is the number
# that matters.
DURATION="8760h"
APPLY=0
ALLOW_BUILD_ADDRESS=0

# Per-key source kubeconfig — see "Which cluster each key points at" above.
declare -A SRC=(
  [dev]="$KUBECONFIG_DIR/kubeconfig-dev"
  [prd]="$KUBECONFIG_DIR/kubeconfig-prd"
)

# MS-A2 BUILD addresses (kube-infra msa2 plan § Cutover inventory row 3:
# prd .17 -> .11, dev .18 -> .12). Delete this list once both boxes sit at
# their final addresses; until then a token minted here is a token that dies.
BUILD_ADDRESSES=(10.10.5.17 10.10.5.18)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)                APPLY=1; shift ;;
    --duration)             DURATION="$2"; shift 2 ;;
    --dev-kubeconfig)       SRC[dev]="$2"; shift 2 ;;
    --prd-kubeconfig)       SRC[prd]="$2"; shift 2 ;;
    --allow-build-address)  ALLOW_BUILD_ADDRESS=1; shift ;;
    # The whole leading comment block, so the help cannot drift from it.
    -h|--help)  awk 'NR == 1 { next } /^#/ { print; next } { exit }' "$0"; exit 0 ;;
    *)          echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# ─── Helpers ─────────────────────────────────────────────────────────────────

# Host part of a URL: https://10.10.5.18:6443/x -> 10.10.5.18. (IPv4 or a
# hostname; nothing here uses an IPv6 literal.)
url_host() {
  local h="${1#*://}"
  h="${h%%/*}"
  printf '%s' "${h%:*}"
}

# True when the URL's host is an MS-A2 build address. Exact match, so a
# near miss like 10.10.5.1 or 10.10.5.170 is not a hit.
is_build_address() {
  local host a
  host="$(url_host "$1")"
  for a in "${BUILD_ADDRESSES[@]}"; do
    [[ "$host" == "$a" ]] && return 0
  done
  return 1
}

# Decode a JWT's payload and print the `exp` claim as a human date plus
# the remaining lifetime, then the subject and issuer. Reads what the
# SERVER issued, not what we asked.
jwt_expiry() {
  python3 - "$1" <<'PY'
import base64, datetime, json, sys
payload = sys.argv[1].split('.')[1]
payload += '=' * (-len(payload) % 4)
claims = json.loads(base64.urlsafe_b64decode(payload))
exp = datetime.datetime.fromtimestamp(claims['exp'], datetime.UTC)
now = datetime.datetime.now(datetime.UTC)
# Format the delta by hand. `str(timedelta)` renders a negative span as
# e.g. "-15 days, 23:58:02" (= -14d 0:02), which is easy to misread as
# still-valid at a glance — the exact mistake this script exists to stop.
delta = exp - now
secs = int(abs(delta.total_seconds()))
span = f"{secs // 86400}d {secs % 86400 // 3600}h {secs % 3600 // 60}m"
human = f"{span} remaining" if delta.total_seconds() > 0 else f"*** ALREADY EXPIRED {span} ago ***"
print(f"{exp.isoformat(timespec='seconds')}|{human}|{claims.get('sub','')}|{claims.get('iss','')}")
PY
}

# ─── 0. Pre-flight — nothing is minted until every key passes ───────────────
# Server + CA come out of the admin kubeconfig so the rendered file always
# matches the live cluster (Q170S1: the VIP, 10.10.5.2 prd / .3 dev; MS-A2:
# the node's own address, no VIP).
declare -A SERVER CA_DATA

for CLUSTER in dev prd; do
  KCFG="${SRC[$CLUSTER]}"
  [[ -f "$KCFG" ]] || { echo "FATAL: $CLUSTER admin kubeconfig not found: $KCFG" >&2; exit 1; }

  SERVER[$CLUSTER]="$(KUBECONFIG="$KCFG" kubectl config view --minify \
                        -o jsonpath='{.clusters[0].cluster.server}')"
  CA_DATA[$CLUSTER]="$(KUBECONFIG="$KCFG" kubectl config view --raw --minify \
                         -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')"
  [[ -n "${SERVER[$CLUSTER]}" && -n "${CA_DATA[$CLUSTER]}" ]] \
    || { echo "FATAL: could not read server/CA for $CLUSTER from $KCFG" >&2; exit 1; }

  echo "==> $CLUSTER: $KCFG -> ${SERVER[$CLUSTER]}"

  if is_build_address "${SERVER[$CLUSTER]}"; then
    if [[ "$ALLOW_BUILD_ADDRESS" -eq 1 ]]; then
      echo "    WARNING : MS-A2 BUILD address — this token dies at the re-address (--allow-build-address)" >&2
    else
      echo "FATAL: $CLUSTER server ${SERVER[$CLUSTER]} is an MS-A2 BUILD address." >&2
      echo "       The endpoint is the token issuer, so the re-address to the final" >&2
      echo "       address invalidates this token (kube-infra msa2 plan D12, § Cutover" >&2
      echo "       inventory row 16(a)). Re-mint after the box has moved." >&2
      exit 1
    fi
  fi

  NODES="$(KUBECONFIG="$KCFG" kubectl get nodes -o name --request-timeout=15s \
             | sed 's|^node/||' | paste -sd, -)" \
    || { echo "FATAL: cannot list nodes on $CLUSTER (${SERVER[$CLUSTER]}) — nothing minted" >&2; exit 1; }
  [[ -n "$NODES" ]] || { echo "FATAL: $CLUSTER reports no nodes — nothing minted" >&2; exit 1; }
  echo "    nodes   : $NODES"
done

if [[ "${SERVER[dev]}" == "${SERVER[prd]}" ]]; then
  echo "FATAL: dev and prd both point at ${SERVER[dev]} — the agent would watch one" >&2
  echo "       cluster twice and the other not at all. Check --dev/--prd-kubeconfig." >&2
  exit 1
fi

# ─── 1-4. Mint, verify, render — per cluster ─────────────────────────────────
declare -A RENDERED

for CLUSTER in dev prd; do
  KCFG="${SRC[$CLUSTER]}"

  echo "==> $CLUSTER: minting token for $SA_NAMESPACE/$SA_NAME (requested $DURATION)"
  TOKEN="$(KUBECONFIG="$KCFG" kubectl -n "$SA_NAMESPACE" create token "$SA_NAME" \
             --duration="$DURATION")"

  # ── Step 2: what did we ACTUALLY get? ──
  IFS='|' read -r EXP_AT REMAINING SUBJECT ISSUER <<<"$(jwt_expiry "$TOKEN")"
  echo "    subject : $SUBJECT"
  echo "    issuer  : $ISSUER"
  echo "    expires : $EXP_AT  ($REMAINING)"

  if [[ "$SUBJECT" != "system:serviceaccount:$SA_NAMESPACE:$SA_NAME" ]]; then
    echo "FATAL: token subject is not the expected SA — refusing to continue" >&2
    exit 1
  fi

  # The pre-flight judged the kubeconfig's server; this judges the token's
  # own issuer, which is what the re-address actually invalidates.
  if is_build_address "$ISSUER" && [[ "$ALLOW_BUILD_ADDRESS" -ne 1 ]]; then
    echo "FATAL: $CLUSTER token issuer $ISSUER is an MS-A2 BUILD address — it dies at" >&2
    echo "       the re-address (plan D12). Nothing was published." >&2
    exit 1
  fi

  OUT="$WORKDIR/cluster-agent-$CLUSTER.kubeconfig"
  cat > "$OUT" <<YAML
apiVersion: v1
kind: Config
clusters:
  - name: $CLUSTER
    cluster:
      server: ${SERVER[$CLUSTER]}
      certificate-authority-data: ${CA_DATA[$CLUSTER]}
users:
  - name: $SA_NAME
    user:
      token: $TOKEN
contexts:
  - name: $CLUSTER
    context:
      cluster: $CLUSTER
      user: $SA_NAME
current-context: $CLUSTER
YAML

  # ── Step 3: prove the rendered kubeconfig actually works ──
  # Two reads: a plain API read, and the services/proxy path the agent
  # depends on for Loki/Prometheus/Alertmanager. The second is the one
  # that broke — a token can authenticate and still lack the proxy Role.
  echo -n "    verify  : API read ... "
  KUBECONFIG="$OUT" kubectl get ns flux-system -o name >/dev/null
  echo -n "ok / services-proxy ... "
  KUBECONFIG="$OUT" kubectl -n monitoring get --raw \
    '/api/v1/namespaces/monitoring/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/status' \
    >/dev/null
  echo "ok"

  RENDERED[$CLUSTER]="$OUT"
done

# ─── 5. Apply ────────────────────────────────────────────────────────────────
if [[ "$APPLY" -eq 0 ]]; then
  # WORKDIR is wiped by the EXIT trap, so copy the rendered files somewhere
  # the printed commands can actually reach. 0600 — these carry live
  # (read-only) cluster credentials; the reminder to delete them is below.
  for CLUSTER in dev prd; do
    install -m 600 "${RENDERED[$CLUSTER]}" "/tmp/cluster-agent-$CLUSTER.kubeconfig"
  done

  # Unquoted heredoc: $VARS interpolate, but backslashes pass through, so
  # the `tr -d '\n'` below stays literal for copy-paste. \$( is escaped so
  # the command substitution is printed, not executed.
  cat <<EOF

Render + verification passed. Nothing was published (no --apply).
Kubeconfigs written to /tmp/cluster-agent-{dev,prd}.kubeconfig (mode 0600).

To publish, re-run with --apply, or do it by hand:

  doppler secrets set \\
    KUBECONFIG_DEV="\$(base64 < /tmp/cluster-agent-dev.kubeconfig | tr -d '\n')" \\
    KUBECONFIG_PRD="\$(base64 < /tmp/cluster-agent-prd.kubeconfig | tr -d '\n')" \\
    --project $DOPPLER_PROJECT --config $DOPPLER_CONFIG
  cd $REPO_ROOT && ./manage.sh phase apps --only $NAS_APP --apply

  ^ NOT 'midclt call app.redeploy' — that recreates the container from the
    STORED rendered config and would redeploy the OLD token. Only the
    manage.sh apply re-renders from current Doppler.

Then shred the copies — they hold live tokens:
  rm -f /tmp/cluster-agent-{dev,prd}.kubeconfig
EOF
  exit 0
fi

# Record the running process's start time BEFORE anything changes, so the
# wait loop below can tell a genuinely new process from the old one still
# answering /health.
AGENT_START_BEFORE="$(curl -s --max-time 5 http://10.10.10.10:9595/metrics 2>/dev/null \
  | awk '/^process_start_time_seconds/ {print $2}')"

echo "==> writing kubeconfigs to Doppler $DOPPLER_PROJECT/$DOPPLER_CONFIG"
# Doppler stores these base64-encoded; the compose layer decodes them.
# `base64` wraps lines on some platforms, so strip newlines — the agent
# decodes the whole value in one go.
doppler secrets set \
  KUBECONFIG_DEV="$(base64 < "${RENDERED[dev]}" | tr -d '\n')" \
  KUBECONFIG_PRD="$(base64 < "${RENDERED[prd]}" | tr -d '\n')" \
  --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --silent

echo "==> re-rendering the app config so the container picks up the new values"
# ⚠ `midclt call app.redeploy` is NOT enough, and this cost a wasted cycle
# on 2026-09-05. Doppler is not read at container start: manage.sh RENDERS
# the compose with the secret values substituted and stores that rendered
# config in TrueNAS (/mnt/.ix-apps/app_configs/cluster-agent/...).
# `app.redeploy` recreates the container from the STORED config, so it
# faithfully redeploys the OLD token. Proof: after a Doppler write plus a
# redeploy, `manage.sh phase apps --only cluster-agent` (dry-run) still
# reported `app_ensured action=update changed=True`.
#
# The apply below re-renders from current Doppler AND recreates the
# container. Verified: `changed=True` before it, `changed=False` after.
(cd "$REPO_ROOT" && ./manage.sh phase apps --only "$NAS_APP" --apply) \
  | grep -vE "cluster_agent_file_ensured" || true

echo "==> waiting for the NEW process to come up"
# ⚠ Do NOT just poll /health. The old container keeps serving 200 while the
# redeploy is still queued, so a naive poll returns "healthy" instantly and
# proves nothing (observed 2026-09-05: /health reported ok with
# uptime_seconds=1061654 — the 12-day-old process — right after an apply).
# Wait for process_start_time_seconds to actually CHANGE.
agent_start() {
  curl -s --max-time 5 http://10.10.10.10:9595/metrics 2>/dev/null \
    | awk '/^process_start_time_seconds/ {print $2}'
}
BEFORE="${AGENT_START_BEFORE:-}"
for _ in $(seq 1 60); do
  NOW="$(agent_start || true)"
  if [[ -n "$NOW" && "$NOW" != "$BEFORE" ]]; then
    echo "    new process up (process_start_time_seconds $BEFORE -> $NOW)"
    break
  fi
  sleep 5
done

curl -s --max-time 10 http://10.10.10.10:9595/health || true
echo
echo

echo "==> verifying the deployed credential can do what Mode A step 1 needs"
# The container's env is now byte-identical to what we just wrote, so
# testing the Doppler value tests what the agent holds.
for CLUSTER in dev prd; do
  V="$WORKDIR/verify-$CLUSTER.kubeconfig"
  doppler secrets get "KUBECONFIG_$(echo "$CLUSTER" | tr '[:lower:]' '[:upper:]')" \
    --project "$DOPPLER_PROJECT" --config "$DOPPLER_CONFIG" --plain 2>/dev/null \
    | tr -d '\n\r ' | base64 -d > "$V"
  printf "    %s: alertmanager " "$CLUSTER"
  KUBECONFIG="$V" kubectl -n monitoring get --raw \
    '/api/v1/namespaces/monitoring/services/kube-prometheus-stack-alertmanager:9093/proxy/api/v2/status' \
    >/dev/null 2>&1 && printf "ok / prometheus " || printf "FAIL / prometheus "
  KUBECONFIG="$V" kubectl -n monitoring get --raw \
    '/api/v1/namespaces/monitoring/services/kube-prometheus-stack-prometheus:9090/proxy/api/v1/query?query=up' \
    >/dev/null 2>&1 && printf "ok / loki " || printf "FAIL / loki "
  KUBECONFIG="$V" kubectl -n monitoring get --raw \
    '/api/v1/namespaces/monitoring/services/loki:3100/proxy/loki/api/v1/labels' \
    >/dev/null 2>&1 && echo "ok" || echo "FAIL"
done
echo
echo "Done. The next scheduled digest is 06:00 (dev) / 06:01 (prd) Europe/Riga."
echo "Confirm a SUCCESS series appears after it runs — the absence of one is"
echo "exactly what went unnoticed for 14 days:"
echo "  curl -s http://10.10.10.10:9595/metrics | grep cluster_agent_run_total"
echo
echo "Next rotation is due before the expiry printed above. If it lapses,"
echo "ClusterAgentNoSuccessfulRun + ClusterAgentRunsFailing will page within a day."
