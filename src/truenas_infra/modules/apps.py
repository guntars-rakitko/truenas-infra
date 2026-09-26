"""Phase: apps — Custom App registration from committed compose YAML.

Live apps (authoritative list is config/apps.yaml, NOT this docstring):
  * minio-prd     → 10.10.10.10 (S3 for Velero/Longhorn/backups, prd)
  * minio-dev     → 10.10.15.10 (S3 for Velero/Longhorn/backups, dev)
  * traefik       → 10.10.5.20  (mgmt-VLAN ingress for the NAS admin UIs)
  * wiki          → nginx serving the MkDocs site
  * cluster-agent → LLM-driven SRE assistant (both data VLANs)

Declared but `enabled: false`: plex, qbittorrent.

⚠ Six apps were RETIRED with the 2026-09-23 pool rebuild — pxe, meshcentral,
amtctl, homepage, stress-dashboard and iperf3 — and their ~930 lines of
upload/cronjob machinery were deleted here in the same pass. meshcentral and
amtctl are not merely unused: they existed only to KVM into the Q170S1 nodes
over Intel AMT, and the MS-A2 replacement has no IPMI/BMC/vPro/AMT at all.
Do not resurrect any of them from git history without re-reading
docs/superpowers/plans/2026-09-23-nas-pool-rebuild.md.
"""

from __future__ import annotations

import base64
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Secrets rendering ───────────────────────────────────────────────────────


# Per-app Doppler key map: { app_name: { compose_var: doppler_key } }.
#
# Default source: Doppler `infrastructure/ops`. Per-app overrides are
# declared in _DOPPLER_PROJECT_PER_APP below (currently only cluster-agent,
# which migrated to its own dedicated project in Task 22.5).
#
# The compose-var → doppler-key indirection lets the compose file keep its
# conventional env-var names (e.g. `MINIO_ROOT_USER` per the MinIO
# container's expected env), while Doppler stores the per-cluster-suffixed
# canonical names (`MINIO_ROOT_USER_PRD`).
#
# Adding a new app with secrets: append a new entry here. The compose
# file uses `${VAR}` placeholders for the keys on the LEFT of each entry.
_DOPPLER_KEYS_PER_APP: dict[str, dict[str, str]] = {
    "cluster-agent": {
        # Secrets source: cluster-agent/prd (see _DOPPLER_PROJECT_PER_APP).
        # Env var names match Doppler key names exactly — project is the
        # namespace, no CLUSTER_AGENT_ prefix needed.
        #
        # LLM auth — active path. Agent SDK reads CLAUDE_CODE_OAUTH_TOKEN
        # (sk-ant-oat01-* from `claude setup-token`) natively. Bills against
        # Max subscription; pool-shares with interactive Claude Code until
        # 2026-06-15 when it moves to a separate $100/mo bucket.
        # ANTHROPIC_API_KEY stays registered as documented fallback — NOT
        # passed into the container env (see docker-compose.yaml header).
        # Operator swaps compose line if API key needed (silent-shadow footgun
        # means both must never be set simultaneously — main.py fail-fast).
        #
        # (Previously had a file-based path via Compose configs: + a
        # CLAUDE_OAUTH_CREDENTIALS_B64DECODED entry — dropped 2026-05-25
        # after empirical research showed CLAUDE_CODE_OAUTH_TOKEN env var
        # is the canonical headless mechanism.)
        # LLM auth — both tokens are always passed; LLM_AUTH_MODE
        # ("oauth" or "api_key") decides which one main.py keeps in
        # os.environ at startup. Flip with `doppler secrets set
        # LLM_AUTH_MODE=oauth` + container restart.
        "LLM_AUTH_MODE":                 "LLM_AUTH_MODE",
        "CLAUDE_CODE_OAUTH_TOKEN":       "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY":             "ANTHROPIC_API_KEY",
        "GH_APP_ID":                     "GH_APP_ID",
        "GH_APP_PRIVATE_KEY":            "GH_APP_PRIVATE_KEY",
        "GH_APP_INSTALLATION_ID":        "GH_APP_INSTALLATION_ID",
        "KUBECONFIG_DEV":                "KUBECONFIG_DEV",
        "KUBECONFIG_PRD":                "KUBECONFIG_PRD",
        # Loki/Prom/AM/Grafana all use apiserver proxy via kubeconfig SA
        # token — no separate annotation-auth keys needed. Grafana itself
        # is in auth.proxy mode + auto-creates the cluster-agent user
        # from X-WEBAUTH-USER. (Old GRAFANA_API_TOKEN_{DEV,PRD} entries
        # removed 2026-05-26 with cluster-agent PR #42.)
        "ENABLED":                       "ENABLED",
        "DISABLED_MODES":                "DISABLED_MODES",
        # Removed 2026-05-27 (post-P3 wrap):
        #   - AUTOMERGE_DISABLED_REPOS  (Mode J reservation, never spec'd)
        #   - MODE_A_BUDGET_USD          (P1 5-min legacy, replaced by
        #                                 DAILY_DIGEST_BUDGET_USD)
        #   - MINIO_NAS_KEY_ID/SECRET_KEY (Mode G reservation, paused)
        #   - B2_KEY_ID/APP_KEY           (Mode G off-site, paused)
        #   - KUBECONFIG_TEST_RESTORE_DEV (Mode G test-restore SA, paused)
        # The cluster-agent `cluster-agent-tests` ns + Role on dev was
        # also removed in kube-infra at the same time. Re-provision via
        # P0 Task 2 + 4 + truenas-infra/scripts/setup-minio-users.sh
        # if/when Mode G is revived.
        # Mode A (P2 daily-digest)
        "SANDBOX_REPO":                  "SANDBOX_REPO",
        # Findings/summary routing (2026-07-06 graduation spec).
        # FINDINGS_REPO → ops repo (kube-infra); DIGEST_REPO → renamed
        # digest repo; FINDINGS_MIN_SEVERITY = inert floor (empty = every
        # finding files). All fall back to SANDBOX_REPO in code.
        "DIGEST_REPO":                   "DIGEST_REPO",
        "FINDINGS_REPO":                 "FINDINGS_REPO",
        "FINDINGS_MIN_SEVERITY":         "FINDINGS_MIN_SEVERITY",
        "LLM_MODEL":                     "LLM_MODEL",
        "MODE_A_CLUSTERS":               "MODE_A_CLUSTERS",
        # Daily-digest (P2 — 2026-05-26 pivot from 5-min polling)
        "DAILY_DIGEST_HOUR":             "DAILY_DIGEST_HOUR",     # default "6"
        "DAILY_DIGEST_MINUTE":           "DAILY_DIGEST_MINUTE",   # default "0"
        "DAILY_DIGEST_WINDOW_HOURS":     "DAILY_DIGEST_WINDOW_HOURS",   # default "24"
        "DAILY_DIGEST_BUDGET_USD":       "DAILY_DIGEST_BUDGET_USD",     # default "0.50"
        # Summary delivery (P3+ — 2026-05-27). CSV destinations:
        # "" disabled, "issue", "email", or "email,issue".
        "DIGEST_SUMMARY":                "DIGEST_SUMMARY",
        "DIGEST_SUMMARY_EMAIL_TO":       "DIGEST_SUMMARY_EMAIL_TO",
        # SES SMTP credentials (mirrored from infrastructure/shr
        # SHARED_SES_W1_* — same creds Alertmanager + everything else
        # uses for w1.lv outbound). Rotate in lockstep with the
        # canonical copy in infrastructure/shr.
        "SES_SMTP_HOST":                 "SES_SMTP_HOST",
        "SES_SMTP_PORT":                 "SES_SMTP_PORT",
        "SES_SMTP_USERNAME":             "SES_SMTP_USERNAME",
        "SES_SMTP_PASSWORD":             "SES_SMTP_PASSWORD",
        "SES_FROM_DEFAULT":              "SES_FROM_DEFAULT",
    },
    # MINIO_AISTOR_LICENSE is a single shared Doppler key (no _PRD/_DEV
    # suffix) — the AIStor Free license is org-scoped, the same token
    # works for both single-node instances.
    "minio-dev": {
        "MINIO_ROOT_USER":       "MINIO_ROOT_USER_DEV",
        "MINIO_ROOT_PASSWORD":   "MINIO_ROOT_PASSWORD_DEV",
        "MINIO_KMS_SECRET_KEY":  "MINIO_KMS_SECRET_KEY_DEV",
        "MINIO_AISTOR_LICENSE":  "MINIO_AISTOR_LICENSE",
    },
    "minio-prd": {
        "MINIO_ROOT_USER":       "MINIO_ROOT_USER_PRD",
        "MINIO_ROOT_PASSWORD":   "MINIO_ROOT_PASSWORD_PRD",
        "MINIO_KMS_SECRET_KEY":  "MINIO_KMS_SECRET_KEY_PRD",
        "MINIO_AISTOR_LICENSE":  "MINIO_AISTOR_LICENSE",
    },
}

# Per-app Doppler project overrides: { app_name: (project, config) }.
# Apps NOT listed here default to ("infrastructure", "ops").
# cluster-agent was migrated to its own dedicated Doppler project in Task 22.5
# to isolate its secrets from the broader infrastructure service token.
_DOPPLER_PROJECT_PER_APP: dict[str, tuple[str, str]] = {
    "cluster-agent": ("cluster-agent", "prd"),
}


def _load_doppler_for_app(app_name: str) -> dict[str, str]:
    """Fetch the secrets needed for `app_name` from Doppler.

    The Doppler project/config defaults to infrastructure/ops; apps in
    _DOPPLER_PROJECT_PER_APP use their own project instead.

    Returns a `{compose_var: value}` dict ready for `_render_compose`
    substitution. Empty dict if the app isn't in `_DOPPLER_KEYS_PER_APP`
    (i.e., the app declares no secrets).

    Raises `RuntimeError` if any required Doppler key fetch fails — we
    fail loud rather than render the compose with an empty value.
    """
    mapping = _DOPPLER_KEYS_PER_APP.get(app_name)
    if not mapping:
        return {}
    project, config = _DOPPLER_PROJECT_PER_APP.get(app_name, ("infrastructure", "ops"))
    out: dict[str, str] = {}
    for compose_var, doppler_key in mapping.items():
        result = subprocess.run(
            ["doppler", "secrets", "get", doppler_key,
             "--project", project, "--config", config,
             "--plain", "--silent"],
            check=False, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to fetch Doppler key '{doppler_key}' for app "
                f"'{app_name}': {result.stderr.strip()}"
            )
        # `--plain` outputs the value followed by a newline; strip the trailing
        # newline only (preserve any internal newlines for multi-line values).
        raw = result.stdout.rstrip("\n")
        # Convention: a compose-var whose name ends in _B64DECODED means the
        # Doppler value is base64-encoded and must be decoded before injection.
        # No current consumer (was used by CLAUDE_OAUTH_CREDENTIALS_B64DECODED
        # before that file-based path was dropped 2026-05-25 in favor of
        # CLAUDE_CODE_OAUTH_TOKEN). Kept as a general-purpose hook for future
        # apps that need binary credentials surfaced via Compose configs:.
        if compose_var.endswith("_B64DECODED"):
            try:
                raw = base64.b64decode(raw).decode("utf-8")
            except Exception:
                # Placeholder value (not yet a real base64 blob) — inject
                # as-is; downstream consumer must handle the placeholder.
                pass
        out[compose_var] = raw
    return out


_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _render_compose(compose_path: Path, app_name: str = "") -> str:
    """Read compose YAML, substitute ${VAR} with values fetched from Doppler.

    `app_name` selects the per-app key map (`_DOPPLER_KEYS_PER_APP`); apps
    declaring no secrets there pass through unchanged.

    Missing secrets leave the placeholder intact — TrueNAS will reject
    unsubstituted variables, making the issue visible.
    """
    compose = compose_path.read_text(encoding="utf-8")
    values = _load_doppler_for_app(app_name) if app_name else {}
    if not values:
        return compose

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return _VAR_RE.sub(_sub, compose)


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AppSpec:
    name: str
    compose_path: Path
    bind_ip: str = ""
    description: str = ""


@dataclass(frozen=True)
class AppsConfig:
    apps: tuple[AppSpec, ...] = ()


def load_apps_config(path: Path) -> AppsConfig:
    """Parse config/apps.yaml — only ENABLED apps are returned."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    apps: list[AppSpec] = []
    for a in raw.get("apps") or []:
        if not a.get("enabled", True):
            continue
        apps.append(
            AppSpec(
                name=a["name"],
                compose_path=Path(a["compose"]),
                bind_ip=a.get("bind_ip", ""),
                description=a.get("description", ""),
            )
        )
    return AppsConfig(apps=tuple(apps))


# ─── ensure_docker_pool ──────────────────────────────────────────────────────


def _wait_for_docker_running(
    cli: Any, *, pool_name: str, wait_s: float, after_update: bool,
) -> None:
    """Poll `docker.status` until RUNNING. Raise `RuntimeError` if it never is.

    ⚠ The `else` is load-bearing. Until 2026-09-23 this loop had none, so a
    daemon that never came up fell straight through to a SUCCESS Diff — and
    run() went on to app.create, which failed with 'No pool configured for
    Docker', the exact error this wait exists to prevent. The operator saw a
    green `docker_pool_ensured` immediately followed by an unexplained
    app-create failure. Same shape as commit_network_changes() in network.py:
    a bounded wait must say so when it gives up.

    Errors DURING the wait stay swallowed on purpose — the daemon is genuinely
    restarting and will refuse connections for a while. But the last one is
    kept rather than discarded, because on timeout it is the only thing that
    distinguishes "slow daemon" from "the API has been failing for 60s".

    `after_update` only shapes the message: whether we just set the pool, or
    found it already set and are checking a daemon nobody here disturbed.
    """
    deadline = time.monotonic() + wait_s
    last_status: str | None = None
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = cli.call("docker.status")
            last_error = None
            last_status = (status or {}).get("status")
            if last_status == "RUNNING":
                return
        except Exception as exc:  # noqa: BLE001 — transient during restart
            last_error = exc
        time.sleep(2)

    seen = (
        f"last reported status {last_status!r}" if last_status
        else "the daemon never returned a status"
    )
    why = f"; last error: {last_error!r}" if last_error is not None else ""
    what = (
        f"of setting pool {pool_name!r}" if after_update
        else f"— pool {pool_name!r} was already configured, so nothing here "
             f"disturbed it; the daemon is down on its own"
    )
    raise RuntimeError(
        f"Docker daemon did not reach RUNNING within {wait_s}s {what} "
        f"({seen}{why}). Refusing to continue — app.create would fail with "
        "'No pool configured for Docker'."
    )


def ensure_docker_pool(
    cli: Any, *, pool_name: str, apply: bool, wait_s: float = 60,
) -> Diff:
    """Ensure TrueNAS Docker/Apps is configured to use `pool_name` as storage.

    On apply, also wait for the docker daemon to reach RUNNING — otherwise
    subsequent `app.create` calls fail with 'No pool configured for Docker'.

    Raises `RuntimeError` if the daemon never reaches RUNNING within `wait_s`.
    """
    live = cli.call("docker.config")

    if live.get("pool") == pool_name:
        # ⚠ The pool being set does NOT mean the daemon is up, and this path is
        # how you find that out the hard way. `docker.update` persists the pool
        # BEFORE the wait below, so a run that set the pool and then timed out
        # leaves the config correct and the daemon dead — and every later run
        # lands here. Until 2026-09-23 this returned an unconditional green
        # noop, so the operator's instinctive re-run reported success while
        # app.create kept failing. Verifying here is what makes a re-run
        # meaningful rather than reassuring.
        #
        # Dry-run is deliberately exempt: `ensure_custom_app` never calls
        # app.create without `apply`, so the daemon is not a precondition for
        # anything a dry-run does — and an inspection command should not block
        # for wait_s or hard-fail on state it was only asked to report.
        if apply:
            _wait_for_docker_running(
                cli, pool_name=pool_name, wait_s=wait_s, after_update=False,
            )
        return Diff.noop(live)

    if not apply:
        return Diff.update(before=live, after={**live, "pool": pool_name})

    updated = cli.call("docker.update", {"pool": pool_name})
    _wait_for_docker_running(
        cli, pool_name=pool_name, wait_s=wait_s, after_update=True,
    )
    return Diff.update(before=live, after=updated)


# ─── ensure_custom_app ───────────────────────────────────────────────────────


def ensure_custom_app(cli: Any, *, spec: AppSpec, apply: bool) -> Diff:
    """Register or update a Custom App from the committed compose YAML.

    Three paths:

      1. App doesn't exist → `app.create` (first-time install).
      2. App exists, stored compose == local compose → noop.
      3. App exists, compose drift → `app.update` with the new
         `custom_compose_config_string`; TrueNAS rewrites the compose
         AND redeploys the container in one job (observed as
         "Updating docker resources" in job progress).

    Drift detection: `app.config <name>` returns the stored compose as
    a parsed dict. We parse the local YAML the same way and deep-equal
    the two. This is robust to whitespace + comment differences because
    both sides go through the YAML parser. Structural changes (e.g.
    `network_mode: bridge` → `host`, `ports:` added/removed, a new
    volume) show up immediately.

    Note on build-context files (Dockerfile, entrypoint.sh, nginx.conf):
    those live under /mnt/tank/system/apps-config/<app>/build/ and are
    refreshed by `pxe_build_context_ensured` / equivalent BEFORE this
    function runs. When `app.update` re-pushes the compose, Docker's
    `build:` step re-evaluates the context and rebuilds only layers
    whose inputs changed — typical turnaround is <5s when just the
    final-stage COPY files changed.
    """
    existing = cli.call("app.query", [["name", "=", spec.name]])
    compose_yaml = _render_compose(spec.compose_path, app_name=spec.name)

    # First-time create.
    if not existing:
        payload: dict[str, Any] = {
            "app_name": spec.name,
            "custom_app": True,
            "custom_compose_config_string": compose_yaml,
        }
        if apply:
            created = cli.call("app.create", payload, job=True)
            return Diff.create(created)
        return Diff.create(payload)

    # Exists: drift-check via parsed compose deep-equal.
    live_compose = cli.call("app.config", spec.name)
    desired_compose = yaml.safe_load(compose_yaml)

    if live_compose == desired_compose:
        return Diff.noop(existing[0])

    if not apply:
        return Diff.update(before=live_compose, after=desired_compose)

    # Job-returning method — pass `job=True` so the client blocks and
    # raises on FAILED. Completion (including container redeploy) is
    # synchronous from our POV.
    cli.call(
        "app.update",
        spec.name,
        {"custom_compose_config_string": compose_yaml},
        job=True,
    )
    return Diff.update(before=live_compose, after=desired_compose)


# ─── ensure_cronjob ──────────────────────────────────────────────────────────


def ensure_cronjob(
    cli: Any,
    *,
    description: str,
    command: str,
    schedule: dict[str, str],
    user: str = "root",
    apply: bool,
) -> Diff:
    """Ensure a TrueNAS cronjob exists identified by its description.

    We use `description` as the idempotency key since TrueNAS doesn't expose
    a stable human-readable name. If a job with this description exists
    but has drifted (command/schedule/user/enabled differs from desired),
    it's updated in-place.
    """
    existing = cli.call("cronjob.query", [["description", "=", description]])
    desired = {
        "enabled": True,
        "description": description,
        "command": command,
        "user": user,
        "schedule": schedule,
    }
    if existing:
        current = existing[0]
        # Compare only the fields we own — TrueNAS adds id/origin/etc we don't care about.
        drifted = any(current.get(k) != desired[k] for k in ("command", "schedule", "user", "enabled"))
        if not drifted:
            return Diff.noop(current)
        if apply:
            updated = cli.call("cronjob.update", current["id"], desired)
            return Diff.update(before=current, after=updated)
        return Diff.update(before=current, after=desired)

    if apply:
        created = cli.call("cronjob.create", desired)
        return Diff.create(created)
    return Diff.create(desired)


# ─── File upload to NAS (filesystem.put wrapper) ─────────────────────────────


CONTENT_VERIFY_MAX_BYTES = 1 * 1024 * 1024
"""Pull-back ceiling for content verification in `ensure_file_on_nas`.

1 MiB covers every Dockerfile / script / conf we ship while never touching
the multi-hundred-MB hw-validation artefacts that use the same helper.
"""


def _remote_content_matches(
    read_fn: Any,
    *,
    local_path: Path,
    remote_path: str,
    local_size: int,
) -> tuple[bool, bool]:
    """Return (verified, same) for a file whose SIZE already matches.

    `verified` says whether we actually compared bytes; `same` is the
    idempotency answer. When we cannot verify (no read_fn, file too big,
    or the read failed) we return (False, True) — i.e. preserve the old
    size-only behaviour rather than churning uploads — but the caller
    surfaces `content_verified=False` so a silent assumption is at least
    a visible one.
    """
    if read_fn is None or local_size > CONTENT_VERIFY_MAX_BYTES:
        return (False, True)
    try:
        remote_bytes = read_fn(remote_path)
    except Exception:  # noqa: BLE001 — degrade to size-only, never fail the phase
        return (False, True)
    import hashlib
    same = (hashlib.sha256(remote_bytes).digest()
            == hashlib.sha256(local_path.read_bytes()).digest())
    return (True, same)


def ensure_file_on_nas(
    cli: Any,
    upload_fn: Any,
    *,
    local_path: Path,
    remote_path: str,
    mode: int,
    apply: bool,
    read_fn: Any = None,
) -> Diff:
    """Upload `local_path` to the NAS at `remote_path` via `upload_fn`.

    Idempotency: size first (cheap), then CONTENT HASH when the sizes
    match and the file is small enough to pull back.

    ⚠ SIZE ALONE IS NOT CONTENT — this function used to stop at the size
    check, with the docstring claiming "any content edit larger than a
    whitespace tweak changes size". That is FALSE and it cost us a silent
    deploy failure (2026-09-13): `apps/pxe/build/Dockerfile` was bumped
    `FROM alpine:3.20` -> `3.23` in git, both spellings are the SAME
    BYTE LENGTH, so the check reported `noop changed=False` forever and
    the NAS kept building PXE on 3.20 while the repo claimed 3.23. The
    dry-run agreed it was clean — a textbook false clean. Any version
    bump, flag flip (`true`->`fals`… no; but `:3.20`->`:3.23`, `=0`->`=1`,
    `WARN`->`INFO`) is equal-length and was invisible.

    `read_fn` is an optional callable `read_fn(remote_path) -> bytes`. When
    supplied and the file is <= CONTENT_VERIFY_MAX_BYTES, a size match is
    confirmed by comparing sha256. When it is absent, or the file is too
    large, or the read fails, we fall back to the old size-only behaviour
    but mark the result `content_verified=False` so the log line says so
    rather than implying a real match.

    ⚠ The size cap exists because this function used to ship hw-validation's
    192 MB modloop; pulling that back every dry-run would have been absurd.
    Every surviving caller uploads small files, so the cap no longer binds —
    but leave it: the next large artifact should not silently re-download.

    `upload_fn` is a callable `upload_fn(*, local_path, remote_path, mode)`
    that actually performs the upload. Injected so tests can mock it
    without touching HTTP.
    """
    local_size = local_path.stat().st_size
    desired = {"path": remote_path, "size": local_size, "mode": mode}

    try:
        remote = cli.call("filesystem.stat", remote_path)
    except Exception:  # noqa: BLE001 — any error ⇒ assume missing
        remote = None

    if remote is not None and remote.get("size") == local_size:
        verified, same = _remote_content_matches(
            read_fn, local_path=local_path, remote_path=remote_path,
            local_size=local_size,
        )
        if same:
            return Diff.noop({**desired, "content_verified": verified})
        # sizes agree but CONTENT differs — the case the old check missed.

    if not apply:
        if remote is None:
            return Diff.create(desired)
        return Diff.update(before={"path": remote_path, "size": remote.get("size")}, after=desired)

    upload_fn(local_path=local_path, remote_path=remote_path, mode=mode)
    if remote is None:
        return Diff.create(desired)
    return Diff.update(
        before={"path": remote_path, "size": remote.get("size")},
        after=desired,
    )


# ─── Talos updater: upload script + register short cronjob ───────────────────


# The cronjob command — a short one-liner that just invokes the on-disk
# script and tees its output to a log file (useful for debugging; TrueNAS
# doesn't retain cronjob stdout/stderr in an easily-queryable way).
# Well under TrueNAS's 1024-char cronjob.command cap.
#
# NOTE on the /bin/bash -c wrap: TrueNAS's `cronjob.run` path exec's the
# command without first running it through a shell, so a top-level `>>`
# or `2>&1` is passed to argv as a literal token and the redirect
# silently doesn't happen (confirmed empirically — `date > /tmp/x` via
# cronjob.run creates /tmp/x at 0 bytes). Wrapping in `/bin/bash -c
# "..."` forces bash to parse the redirect.

def _tls_rotate_cronjob_command(script_path: str) -> str:
    """Same /bin/bash -c wrap as the talos-updater cronjob.

    Runs tls-rotate.sh hourly; it internally calls tls-export.sh (which
    diffs SHA-256 and copies on change) and `app.redeploy`s cert-consuming
    apps when the cert file actually changed.
    """
    log_path = str(Path(script_path).parent / "tls-rotate.log")
    return f'/bin/bash -c "/bin/bash {script_path} >> {log_path} 2>&1"'


def ensure_tls_rotate(
    cli: Any,
    upload_fn: Any,
    *,
    export_path: Path,
    rotate_path: Path,
    remote_dir: str,
    apply: bool,
) -> tuple[Diff, ...]:
    """Deploy the cert export + rotation scripts and register the hourly
    cronjob that drives them.

    Three artifacts:
    - `tls-export.sh`: diff /etc/certificates/w1-wildcard vs pool, exit 10
      on change.
    - `tls-rotate.sh`: wraps export, app.redeploy on change.
    - Cronjob: `0 * * * *` runs tls-rotate.sh.

    Reuses `ensure_file_on_nas` (size-based idempotency) and
    `ensure_cronjob` (update-when-differs).
    """
    remote_export = f"{remote_dir.rstrip('/')}/{export_path.name}"
    remote_rotate = f"{remote_dir.rstrip('/')}/{rotate_path.name}"

    export_diff = ensure_file_on_nas(
        cli, upload_fn,
        local_path=export_path, remote_path=remote_export,
        mode=0o755, apply=apply,
    )
    rotate_diff = ensure_file_on_nas(
        cli, upload_fn,
        local_path=rotate_path, remote_path=remote_rotate,
        mode=0o755, apply=apply,
    )
    cron_diff = ensure_cronjob(
        cli,
        description="tls-rotate",
        command=_tls_rotate_cronjob_command(remote_rotate),
        schedule={"minute": "0", "hour": "*", "dom": "*", "month": "*", "dow": "*"},
        user="root",
        apply=apply,
    )
    return (export_diff, rotate_diff, cron_diff)


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/apps.yaml")

# TLS export + rotate scripts (phase apps ships them to the pool; the
# hourly cronjob runs them when TrueNAS auto-renews the wildcard cert).
TLS_EXPORT_SCRIPT_PATH = Path("apps/tls/tls-export.sh")
TLS_ROTATE_SCRIPT_PATH = Path("apps/tls/tls-rotate.sh")
TLS_REMOTE_DIR = "/mnt/tank/system/tls"

# Traefik dynamic config — routers + services + TLS block. Committed in
# the repo; uploaded to the container's file-provider directory so
# Traefik hot-reloads on change.
TRAEFIK_ROUTES_PATH = Path("apps/traefik/routes.yaml")
TRAEFIK_CONFIG_REMOTE_DIR = "/mnt/tank/system/apps-config/traefik"

# Wiki nginx server config — static, mounted read-only into the `wiki`
# Custom App. The site/ content itself is pushed by wiki/tools/deploy.sh
# from the operator laptop (rsync over SSH), not by this phase.
WIKI_NGINX_CONF_PATH = Path("apps/wiki/nginx.conf")
WIKI_CONFIG_REMOTE_DIR = "/mnt/tank/system/apps-config/wiki"

# cluster-agent — LLM-driven SRE assistant. Stock python:3.14-alpine base
# image, app code on the pool, bind-mounted into /app. We upload src/ +
# prompts/ only; data/ (SQLite state) and venv/ (self-healing,
# Python-version-tied) are excluded deliberately.
# ⚠ This comment used to say "same deploy pattern as amtctl / stress-dashboard".
# Both were retired 2026-09-23, so cluster-agent is now the ONLY app using the
# code-on-pool bind-mount pattern — there is no sibling left to copy from.
CLUSTER_AGENT_LOCAL_DIR = Path("apps/cluster-agent")
CLUSTER_AGENT_SRC_LOCAL_DIR = CLUSTER_AGENT_LOCAL_DIR / "src"
CLUSTER_AGENT_PROMPTS_LOCAL_DIR = CLUSTER_AGENT_LOCAL_DIR / "prompts"
CLUSTER_AGENT_MAIN_LOCAL_FILE = CLUSTER_AGENT_LOCAL_DIR / "main.py"
CLUSTER_AGENT_CODE_REMOTE_DIR = "/mnt/tank/system/apps-config/cluster-agent/code"


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
    pool_name: str = "tank",
) -> int:
    """Phase 9: apps — Custom App deployment + TLS rotation + Traefik routes."""
    log = ctx.log.bind(phase="apps")

    # 1. Docker/Apps storage pool must be configured first.
    diff = ensure_docker_pool(cli, pool_name=pool_name, apply=ctx.apply)
    log.info("docker_pool_ensured", pool=pool_name,
             action=diff.action, changed=diff.changed)

    # 2. Load app config (only enabled apps).
    cfg = load_apps_config(config_path or DEFAULT_CONFIG_PATH)

    # 2a. PRE-APP config file uploads. Any app that bind-mounts a named file
    # (not just a directory) needs the source file present BEFORE the
    # container starts — otherwise Docker bind-mount creates an empty dir
    # in place of the file and the container crashes.
    #
    # `wiki` bind-mounts a specific nginx.conf file. Apps like traefik
    # (directory mount with graceful empty handling) and minio-{prd,dev}
    # (uses env vars) don't need pre-ordering.
    # ⚠ FIXED 2026-09-23 — these used to gate on `only` ALONE, never on whether
    # the app is actually enabled in apps.yaml. `cfg` holds only ENABLED apps,
    # but the dispatch ignored it, so a retired app kept having its config
    # uploaded forever.
    #
    # ⚠ THIS WAS NOT THEORETICAL. After the pool rebuild removed six apps from
    # apps.yaml, the very next `phase apps --apply` RECREATED all four of
    # amtctl/homepage/meshcentral/stress-dashboard's config directories, plus
    # the pxe and talos-updater script trees — on a pool that had just been
    # rebuilt specifically to be rid of them. The cronjobs pointing at those
    # trees survived in TrueNAS config on boot-pool and would have fired:
    # talos-updater nightly at 03:00, and pxe-download on Sunday at 02:30,
    # re-downloading the 15 GB of ISOs the rebuild had deliberately deleted.
    # The dead code for all six was deleted in the same pass; `_want()` is what
    # keeps the remaining five honest.
    #
    # Gating on `_want()` makes the module self-correcting: remove an app from
    # apps.yaml and its config stops being uploaded, with no code change.
    _enabled = {a.name for a in cfg.apps}

    def _want(name: str) -> bool:
        return only in (None, name) and name in _enabled

    if _want("wiki"):
        _ensure_wiki_config_via_ctx(cli, ctx, log)
    if _want("cluster-agent"):
        _ensure_cluster_agent_config_via_ctx(cli, ctx, log)
    for spec in cfg.apps:
        if only and spec.name != only:
            continue
        diff = ensure_custom_app(cli, spec=spec, apply=ctx.apply)
        log.info(
            "app_ensured",
            name=spec.name, bind_ip=spec.bind_ip,
            compose=str(spec.compose_path),
            action=diff.action, changed=diff.changed,
        )

    # 3. TLS cert export + rotation scripts. Ships to /mnt/tank/system/tls/
    # and registers the hourly cronjob. Depends on phase tls having already
    # issued the wildcard cert (the scripts assume /etc/certificates/
    # w1-wildcard.{crt,key} exist).
    # ⚠ DELIBERATELY NOT GATED ON `_want()`. There is no "tls" app in
    # apps.yaml, so `_want("tls")` would ALWAYS be False and cert rotation
    # would silently stop — the wildcard cert would expire with no signal.
    # This is infrastructure, not an app. Leave it on `only`.
    if only in (None, "tls"):
        _ensure_tls_rotate_via_ctx(cli, ctx, log)

    # 4. Traefik routes.yaml — uploaded to the container's file-provider
    # directory. Traefik file-watches and hot-reloads on change, no app
    # redeploy needed for route edits.
    if _want("traefik"):
        _ensure_traefik_routes_via_ctx(cli, ctx, log)

    # (Wiki nginx.conf was already uploaded in step 2a, before the apps
    # loop, so the container bind-mount finds the file on first start.)

    return 0


def _ensure_traefik_routes_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    if not TRAEFIK_ROUTES_PATH.exists():
        log.warning("traefik_routes_skipped",
                    reason="source_missing", path=str(TRAEFIK_ROUTES_PATH))
        return

    from truenas_infra.client import upload_file

    host = ctx.config.truenas_host
    api_key = ctx.config.truenas_api_key
    verify_ssl = ctx.config.truenas_verify_ssl

    def _upload(*, local_path: Path, remote_path: str, mode: int) -> None:
        upload_file(
            cli, host=host, api_key=api_key, verify_ssl=verify_ssl,
            local_path=local_path, remote_path=remote_path, mode=mode,
        )

    remote = f"{TRAEFIK_CONFIG_REMOTE_DIR}/routes.yaml"
    diff = ensure_file_on_nas(
        cli, _upload,
        local_path=TRAEFIK_ROUTES_PATH, remote_path=remote,
        mode=0o644, apply=ctx.apply,
    )
    log.info("traefik_routes_ensured", path=remote,
             action=diff.action, changed=diff.changed)


def _ensure_wiki_config_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload apps/wiki/nginx.conf to the pool so the `wiki` Custom App can
    bind-mount it read-only. Site content (site/) is pushed separately from
    the wiki repo via rsync and is NOT this phase's concern."""
    if not WIKI_NGINX_CONF_PATH.exists():
        log.warning("wiki_config_skipped",
                    reason="source_missing", path=str(WIKI_NGINX_CONF_PATH))
        return

    from truenas_infra.client import upload_file

    host = ctx.config.truenas_host
    api_key = ctx.config.truenas_api_key
    verify_ssl = ctx.config.truenas_verify_ssl

    def _upload(*, local_path: Path, remote_path: str, mode: int) -> None:
        upload_file(
            cli, host=host, api_key=api_key, verify_ssl=verify_ssl,
            local_path=local_path, remote_path=remote_path, mode=mode,
        )

    remote = f"{WIKI_CONFIG_REMOTE_DIR}/nginx.conf"
    diff = ensure_file_on_nas(
        cli, _upload,
        local_path=WIKI_NGINX_CONF_PATH, remote_path=remote,
        mode=0o644, apply=ctx.apply,
    )
    log.info("wiki_config_ensured", path=remote,
             action=diff.action, changed=diff.changed)





def _ensure_cluster_agent_config_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload the cluster-agent app source to the pool.

    Layout on the pool:
        .../apps-config/cluster-agent/code/   — bind-mounted /app (ro)
            ├── main.py                        — FastAPI entrypoint (Task 18)
            ├── src/                           — Python package (Tasks 9-19)
            └── prompts/                       — Jinja2 prompt templates (Task 14)

    Files NOT uploaded:
      - docker-compose.yaml   (owned by ensure_custom_app)
      - data/                 (SQLite state.db — must persist across redeploys)
      - venv/                 (self-healing; tied to Python minor version)

    Source dirs (src/ and prompts/) and main.py are created by Tasks 9-19.
    This helper runs in the phase-apps pre-upload block so the operator can
    validate the registration with `manage.sh phase apps` before those tasks
    are done. It skips gracefully if the files/dirs don't exist yet rather
    than failing — the real code upload happens automatically once they appear.

    Deploy pattern: stock python base image, code on the pool, bind-mounted
    into /app in the container (the only app left using it — see the module
    constants above).
    docker-compose.yaml's command block invokes `uvicorn main:app` which
    resolves to /app/main.py — so main.py must be at the code/ root.
    """
    src_dir = CLUSTER_AGENT_SRC_LOCAL_DIR
    prompts_dir = CLUSTER_AGENT_PROMPTS_LOCAL_DIR
    main_file = CLUSTER_AGENT_MAIN_LOCAL_FILE

    any_present = src_dir.is_dir() or prompts_dir.is_dir() or main_file.is_file()
    if not any_present:
        log.info(
            "cluster_agent_config_skipped",
            reason="source_dirs_missing",
            src=str(src_dir),
            prompts=str(prompts_dir),
            main=str(main_file),
            note="build them in Tasks 9-19",
        )
        return

    from truenas_infra.client import upload_file

    host = ctx.config.truenas_host
    api_key = ctx.config.truenas_api_key
    verify_ssl = ctx.config.truenas_verify_ssl

    def _upload(*, local_path: Path, remote_path: str, mode: int) -> None:
        upload_file(
            cli, host=host, api_key=api_key, verify_ssl=verify_ssl,
            local_path=local_path, remote_path=remote_path, mode=mode,
        )

    # Upload main.py at the top level of code/ (uvicorn `main:app` entrypoint).
    if main_file.is_file():
        remote_main = f"{CLUSTER_AGENT_CODE_REMOTE_DIR}/main.py"
        diff = ensure_file_on_nas(
            cli, _upload,
            local_path=main_file, remote_path=remote_main,
            mode=0o644, apply=ctx.apply,
        )
        log.info("cluster_agent_file_ensured", path=remote_main,
                 action=diff.action, changed=diff.changed)
    else:
        log.info(
            "cluster_agent_main_skipped",
            reason="not_present_yet",
            path=str(main_file),
        )

    # Upload src/ and prompts/ subtrees, preserving relative paths under code/.
    for subdir in (src_dir, prompts_dir):
        if not subdir.is_dir():
            log.info(
                "cluster_agent_subdir_skipped",
                reason="not_present_yet",
                path=str(subdir),
            )
            continue
        for local in sorted(subdir.rglob("*")):
            if not local.is_file():
                continue
            # Preserve the subdir name (src/ or prompts/) in the remote path.
            rel = local.relative_to(CLUSTER_AGENT_LOCAL_DIR).as_posix()
            remote = f"{CLUSTER_AGENT_CODE_REMOTE_DIR}/{rel}"
            diff = ensure_file_on_nas(
                cli, _upload,
                local_path=local, remote_path=remote,
                mode=0o644, apply=ctx.apply,
            )
            log.info("cluster_agent_file_ensured", path=remote,
                     action=diff.action, changed=diff.changed)



def _ensure_tls_rotate_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    if not TLS_EXPORT_SCRIPT_PATH.exists() or not TLS_ROTATE_SCRIPT_PATH.exists():
        log.warning("tls_rotate_skipped",
                    export_exists=TLS_EXPORT_SCRIPT_PATH.exists(),
                    rotate_exists=TLS_ROTATE_SCRIPT_PATH.exists())
        return

    from truenas_infra.client import upload_file

    host = ctx.config.truenas_host
    api_key = ctx.config.truenas_api_key
    verify_ssl = ctx.config.truenas_verify_ssl

    def _upload(*, local_path: Path, remote_path: str, mode: int) -> None:
        upload_file(
            cli, host=host, api_key=api_key, verify_ssl=verify_ssl,
            local_path=local_path, remote_path=remote_path, mode=mode,
        )

    export_diff, rotate_diff, cron_diff = ensure_tls_rotate(
        cli, _upload,
        export_path=TLS_EXPORT_SCRIPT_PATH,
        rotate_path=TLS_ROTATE_SCRIPT_PATH,
        remote_dir=TLS_REMOTE_DIR,
        apply=ctx.apply,
    )
    log.info("tls_export_script_ensured",
             path=f"{TLS_REMOTE_DIR}/tls-export.sh",
             action=export_diff.action, changed=export_diff.changed)
    log.info("tls_rotate_script_ensured",
             path=f"{TLS_REMOTE_DIR}/tls-rotate.sh",
             action=rotate_diff.action, changed=rotate_diff.changed)
    log.info("tls_rotate_cronjob_ensured",
             action=cron_diff.action, changed=cron_diff.changed)




