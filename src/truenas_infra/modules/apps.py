"""Phase: apps — Custom App registration from committed compose YAML.

See docs/plans/zesty-drifting-castle.md §Phase 9.

Planned apps in initial scope:
  * netboot-xyz  → 10.10.5.10  (PXE/TFTP + HTTP + UI)
  * minio-prd    → 10.10.10.10 (S3 for Velero prd)
  * minio-dev    → 10.10.15.10 (S3 for Velero dev)

Deferred (separate pass): plex, qbittorrent.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Secrets rendering ───────────────────────────────────────────────────────


def _load_sops_dotenv(path: Path) -> dict[str, str]:
    """Decrypt a SOPS-encrypted YAML/dotenv secrets file into a dict.

    The file shape is either a YAML mapping (`KEY: value`) or dotenv
    (`KEY=value`). We try YAML first, fall back to dotenv parsing.
    """
    result = subprocess.run(
        ["sops", "decrypt", str(path)],
        check=True, capture_output=True, text=True,
    )
    text = result.stdout
    # Try YAML mapping first.
    try:
        parsed = yaml.safe_load(text)
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
    except yaml.YAMLError:
        pass
    # Fall back to dotenv.
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _render_compose(compose_path: Path, secrets_path: Path | None) -> str:
    """Read compose YAML, substitute ${VAR} with values from decrypted secrets.

    Missing secrets leave the placeholder intact — TrueNAS will reject
    unsubstituted variables, making the issue visible.
    """
    compose = compose_path.read_text(encoding="utf-8")
    if secrets_path is None:
        return compose
    values = _load_sops_dotenv(secrets_path)

    def _sub(match: re.Match) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return _VAR_RE.sub(_sub, compose)


# ─── Config types ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AppSpec:
    name: str
    compose_path: Path
    secrets_path: Path | None = None
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
                secrets_path=Path(a["secrets"]) if a.get("secrets") else None,
                bind_ip=a.get("bind_ip", ""),
                description=a.get("description", ""),
            )
        )
    return AppsConfig(apps=tuple(apps))


# ─── ensure_docker_pool ──────────────────────────────────────────────────────


def ensure_docker_pool(
    cli: Any, *, pool_name: str, apply: bool, wait_s: float = 60,
) -> Diff:
    """Ensure TrueNAS Docker/Apps is configured to use `pool_name` as storage.

    If we change the pool, also wait for the docker daemon to reach RUNNING
    state — otherwise subsequent `app.create` calls fail with
    'No pool configured for Docker'.
    """
    live = cli.call("docker.config")
    if live.get("pool") == pool_name:
        return Diff.noop(live)
    if not apply:
        return Diff.update(before=live, after={**live, "pool": pool_name})

    updated = cli.call("docker.update", {"pool": pool_name})

    # Wait for the daemon to be RUNNING.
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        try:
            status = cli.call("docker.status")
            if status.get("status") == "RUNNING":
                break
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)

    return Diff.update(before=live, after=updated)


# ─── ensure_custom_app ───────────────────────────────────────────────────────


def ensure_custom_app(cli: Any, *, spec: AppSpec, apply: bool) -> Diff:
    """Register (or update) a Custom App using the compose file content.

    TrueNAS Custom Apps accept a compose YAML string via
    `custom_compose_config_string`. Updates to an existing app currently
    no-op (TODO: add --force path to redeploy with new compose).
    """
    existing = cli.call("app.query", [["name", "=", spec.name]])
    if existing:
        # TrueNAS doesn't expose the stored compose for diffing. Treat as noop;
        # operator uses `app.redeploy` / `app.delete` + re-create to update.
        return Diff.noop(existing[0])

    compose_yaml = _render_compose(spec.compose_path, spec.secrets_path)
    payload: dict[str, Any] = {
        "app_name": spec.name,
        "custom_app": True,
        "custom_compose_config_string": compose_yaml,
    }

    if apply:
        created = cli.call("app.create", payload)
        return Diff.create(created)
    return Diff.create(payload)


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


def ensure_file_on_nas(
    cli: Any,
    upload_fn: Any,
    *,
    local_path: Path,
    remote_path: str,
    mode: int,
    apply: bool,
) -> Diff:
    """Upload `local_path` to the NAS at `remote_path` via `upload_fn`.

    Idempotency: compare local file size against `filesystem.stat` on the
    remote. If sizes match, no upload. This is good enough for the tiny
    script files we're shipping (changes ~= months apart, and any content
    edit larger than a whitespace tweak changes size).

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
        return Diff.noop(desired)

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
def _talos_updater_cronjob_command(
    script_path: str,
    *,
    version: str = "latest",
    retention: int = 5,
    arch: str = "amd64",
    platform: str = "metal",
) -> str:
    log_path = str(Path(script_path).parent / "talos-updater.log")
    env = (
        f"TALOS_VERSION={version} RETENTION={retention} "
        f"ARCH={arch} PLATFORM={platform}"
    )
    return f'/bin/bash -c "{env} /bin/sh {script_path} >> {log_path} 2>&1"'


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


# ─── Talos updater config ────────────────────────────────────────────────────


@dataclass(frozen=True)
class TalosUpdaterConfig:
    version: str = "latest"
    retention: int = 5
    architecture: str = "amd64"
    platform: str = "metal"


def load_talos_config(path: Path) -> TalosUpdaterConfig:
    """Parse config/talos.yaml. Missing file → sensible defaults."""
    if not path.exists():
        return TalosUpdaterConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return TalosUpdaterConfig(
        version=str(raw.get("version", "latest")),
        retention=int(raw.get("retention", 5)),
        architecture=str(raw.get("architecture", "amd64")),
        platform=str(raw.get("platform", "metal")),
    )


def ensure_netboot_menu_files(
    cli: Any,
    upload_fn: Any,
    *,
    boot_cfg_path: Path,
    custom_ipxe_path: Path,
    config_menus_dir: str,
    assets_dir: str,
    apply: bool,
) -> tuple[Diff, ...]:
    """Upload the homelab netboot.xyz overrides:

    - boot.cfg → <config_menus_dir>/boot.cfg (0644): chain-loaded by menu.ipxe
      before the menu renders. Sets `custom_url` so the main-menu "Custom
      URL Menu" item appears.
    - custom.ipxe → <assets_dir>/custom.ipxe (0644): served over HTTP :8080
      by the netboot.xyz container. Chained when the user clicks "Custom
      URL Menu" (netboot.xyz appends /custom.ipxe to the URL).
    """
    boot_cfg_remote = f"{config_menus_dir.rstrip('/')}/{boot_cfg_path.name}"
    custom_ipxe_remote = f"{assets_dir.rstrip('/')}/{custom_ipxe_path.name}"
    d1 = ensure_file_on_nas(
        cli, upload_fn,
        local_path=boot_cfg_path, remote_path=boot_cfg_remote,
        mode=0o644, apply=apply,
    )
    d2 = ensure_file_on_nas(
        cli, upload_fn,
        local_path=custom_ipxe_path, remote_path=custom_ipxe_remote,
        mode=0o644, apply=apply,
    )
    return (d1, d2)


def ensure_talos_updater(
    cli: Any,
    upload_fn: Any,
    *,
    script_path: Path,
    schematic_path: Path,
    remote_dir: str,
    apply: bool,
    config: TalosUpdaterConfig | None = None,
) -> tuple[Diff, ...]:
    """Set up the Talos PXE auto-updater end-to-end:

    1. Upload talos-updater.sh (mode 0755) to remote_dir.
    2. Upload schematic.yaml (mode 0644) to remote_dir.
    3. Register a daily cronjob that runs the uploaded script with the
       version/retention/arch/platform env vars from config/talos.yaml.

    Idempotent — safe to re-run. Returns one Diff per artifact for logging.
    """
    cfg = config or TalosUpdaterConfig()
    remote_script = f"{remote_dir.rstrip('/')}/{script_path.name}"
    remote_schematic = f"{remote_dir.rstrip('/')}/{schematic_path.name}"

    script_diff = ensure_file_on_nas(
        cli, upload_fn,
        local_path=script_path, remote_path=remote_script,
        mode=0o755, apply=apply,
    )
    schematic_diff = ensure_file_on_nas(
        cli, upload_fn,
        local_path=schematic_path, remote_path=remote_schematic,
        mode=0o644, apply=apply,
    )
    cron_diff = ensure_cronjob(
        cli,
        description="talos-updater",
        command=_talos_updater_cronjob_command(
            remote_script,
            version=cfg.version, retention=cfg.retention,
            arch=cfg.architecture, platform=cfg.platform,
        ),
        schedule={"minute": "0", "hour": "3", "dom": "*", "month": "*", "dow": "*"},
        user="root",
        apply=apply,
    )

    return (script_diff, schematic_diff, cron_diff)


# ─── Phase entry point ───────────────────────────────────────────────────────


DEFAULT_CONFIG_PATH = Path("config/apps.yaml")

# Location on the NAS where the Talos updater script + schematic live.
# Backed by the `tank/system/apps-config/talos-updater` dataset (created
# by phase datasets).
TALOS_UPDATER_REMOTE_DIR = "/mnt/tank/system/apps-config/talos-updater"

# Local source paths, committed in this repo.
TALOS_UPDATER_SCRIPT_PATH = Path("apps/netboot-xyz/talos-updater.sh")
TALOS_UPDATER_SCHEMATIC_PATH = Path("apps/netboot-xyz/schematic.yaml")
TALOS_UPDATER_CONFIG_PATH = Path("config/talos.yaml")

# Homelab netboot.xyz menu overrides — committed in this repo, uploaded
# to the netboot.xyz container's /config and /assets volumes on every
# `phase apps --apply`.
NETBOOT_BOOT_CFG_PATH = Path("apps/netboot-xyz/boot.cfg")
NETBOOT_CUSTOM_IPXE_PATH = Path("apps/netboot-xyz/custom.ipxe")
NETBOOT_CONFIG_MENUS_DIR = "/mnt/tank/system/pxe/config/menus"
NETBOOT_ASSETS_DIR = "/mnt/tank/system/pxe/assets"

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

# Homepage dashboard configs (services, bookmarks, widgets, settings,
# docker, kubernetes). Mounted read-only into /app/config of the
# `homepage` Custom App. All *.yaml files from apps/homepage/ are
# uploaded EXCEPT docker-compose.yaml (ensure_custom_app owns that) and
# *.sops.yaml (encrypted secrets stay on the laptop).
HOMEPAGE_CONFIG_LOCAL_DIR = Path("apps/homepage")
HOMEPAGE_CONFIG_REMOTE_DIR = "/mnt/tank/system/apps-config/homepage"

# MeshCentral config.json — our canonical overrides for TlsOffload,
# RedirPort, Cert etc. On first-run MC writes its own config; this file
# overwrites it. MC reads config.json ONLY on fresh container start —
# operator must `app.stop` + `app.start` after changes here (NOT
# `app.redeploy`, which keeps the process alive).
MESHCENTRAL_CONFIG_PATH = Path("apps/meshcentral/config.json")
MESHCENTRAL_CONFIG_REMOTE_DIR = "/mnt/tank/system/apps-config/meshcentral/data"

# amtctl sidecar — FastAPI app that proxies Intel AMT WS-MAN for the 6
# K8s nodes + serves a power-control HTML UI. The container is a plain
# python:3.13-alpine image; our code (amt.py, main.py, web/) and config
# (nodes.yaml) live on the pool and bind-mount into the container. So
# "deploying" amtctl means uploading those files — there's no image to
# build or push. The persistent /venv mount (bootstrapped by the
# container's entrypoint) caches pip-installed deps across restarts.
AMTCTL_LOCAL_DIR = Path("apps/amtctl")
AMTCTL_CODE_REMOTE_DIR = "/mnt/tank/system/apps-config/amtctl/code"
AMTCTL_CONFIG_REMOTE_DIR = "/mnt/tank/system/apps-config/amtctl/config"


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
    pool_name: str = "tank",
) -> int:
    """Phase 9: apps — Custom App deployment + Talos PXE auto-updater."""
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
    # `wiki` bind-mounts a specific nginx.conf file. `homepage` mounts a
    # directory but Homepage refuses to start if services.yaml etc are
    # missing, so we pre-upload those too. Apps like traefik (directory
    # mount with graceful empty handling), meshcentral (self-mkdir on first
    # run), minio-{prd,dev} (uses env vars) don't need pre-ordering.
    if only in (None, "wiki"):
        _ensure_wiki_config_via_ctx(cli, ctx, log)
    if only in (None, "homepage"):
        _ensure_homepage_config_via_ctx(cli, ctx, log)
    if only in (None, "meshcentral"):
        _ensure_meshcentral_config_via_ctx(cli, ctx, log)
    if only in (None, "amtctl"):
        _ensure_amtctl_config_via_ctx(cli, ctx, log)

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

    # 3. Talos PXE auto-updater: upload script + schematic to the NAS via
    # filesystem.put, then register a short cronjob that invokes the
    # on-disk script (well under TrueNAS's 1024-char cronjob.command cap).
    if only in (None, "talos-updater"):
        _ensure_talos_updater_via_ctx(cli, ctx, log)

    # 4. Homelab netboot.xyz overrides — boot.cfg sets custom_url so the
    # "Custom URL Menu" item appears in the main menu; clicking it chains
    # our custom.ipxe (with local Talos boot entry).
    if only in (None, "netboot-xyz"):
        _ensure_netboot_menu_files_via_ctx(cli, ctx, log)

    # 5. TLS cert export + rotation scripts. Ships to /mnt/tank/system/tls/
    # and registers the hourly cronjob. Depends on phase tls having already
    # issued the wildcard cert (the scripts assume /etc/certificates/
    # w1-wildcard.{crt,key} exist).
    if only in (None, "tls"):
        _ensure_tls_rotate_via_ctx(cli, ctx, log)

    # 6. Traefik routes.yaml — uploaded to the container's file-provider
    # directory. Traefik file-watches and hot-reloads on change, no app
    # redeploy needed for route edits.
    if only in (None, "traefik"):
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


def _ensure_meshcentral_config_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload apps/meshcentral/config.json so MC uses our canonical
    TlsOffload / RedirPort / Cert settings (rather than whatever MC
    self-generated on first run).

    IMPORTANT: MC only reads config.json on FRESH container start. After
    this helper changes the file, operator must:

        midclt call app.stop meshcentral
        midclt call app.start meshcentral

    `app.redeploy` is NOT enough — the process stays alive across it
    and keeps using in-memory config. See apps/meshcentral/config.json
    header comment for the rationale on each setting.
    """
    if not MESHCENTRAL_CONFIG_PATH.exists():
        log.warning("meshcentral_config_skipped",
                    reason="source_missing", path=str(MESHCENTRAL_CONFIG_PATH))
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

    remote = f"{MESHCENTRAL_CONFIG_REMOTE_DIR}/config.json"
    diff = ensure_file_on_nas(
        cli, _upload,
        local_path=MESHCENTRAL_CONFIG_PATH, remote_path=remote,
        mode=0o644, apply=ctx.apply,
    )
    log.info("meshcentral_config_ensured", path=remote,
             action=diff.action, changed=diff.changed)
    if diff.changed:
        log.warning(
            "meshcentral_config_needs_restart",
            msg="MC only reads config.json on full container start; "
                "run `midclt call app.stop meshcentral && midclt call "
                "app.start meshcentral` to pick up the new values.",
        )


def _ensure_amtctl_config_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload the amtctl app source + node inventory to the pool.

    Layout on the pool:
        .../apps-config/amtctl/code/   — Python + static UI (bind-mounted /app)
            ├── amt.py
            ├── main.py
            └── web/index.html
        .../apps-config/amtctl/config/ — runtime config (bind-mounted /config)
            └── nodes.yaml

    Globs apps/amtctl/ so new files (e.g. additional UI assets, a
    requirements.txt) show up on the next phase-apps run without code
    changes here.

    Files NOT uploaded:
      - docker-compose.yaml  (owned by ensure_custom_app)
      - secrets.sops.yaml    (rendered into compose env by _render_compose)
      - Dockerfile            (unused — runtime install approach; keeping
                               the file for documentation + a possible
                               future switch to registry-hosted image)
    """
    if not AMTCTL_LOCAL_DIR.is_dir():
        log.warning("amtctl_config_skipped",
                    reason="source_missing", path=str(AMTCTL_LOCAL_DIR))
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

    # App code: walk apps/amtctl/ and mirror structure under code/,
    # skipping the "meta" files that belong to other concerns.
    SKIP = {"docker-compose.yaml", "secrets.sops.yaml", "Dockerfile"}
    for local in sorted(AMTCTL_LOCAL_DIR.rglob("*")):
        if not local.is_file():
            continue
        if local.name in SKIP:
            continue
        rel = local.relative_to(AMTCTL_LOCAL_DIR).as_posix()
        if rel == "nodes.yaml":
            # nodes.yaml goes to the config dir (bind-mounted /config)
            remote = f"{AMTCTL_CONFIG_REMOTE_DIR}/nodes.yaml"
        else:
            # Everything else → code dir (bind-mounted /app)
            remote = f"{AMTCTL_CODE_REMOTE_DIR}/{rel}"
        diff = ensure_file_on_nas(
            cli, _upload,
            local_path=local, remote_path=remote,
            mode=0o644, apply=ctx.apply,
        )
        log.info("amtctl_file_ensured", path=remote,
                 action=diff.action, changed=diff.changed)


def _ensure_homepage_config_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload every declarative YAML from apps/homepage/ to the pool so the
    `homepage` Custom App can bind-mount /app/config read-only.

    Skips:
      - docker-compose.yaml  (owned by ensure_custom_app)
      - *.sops.yaml          (encrypted secrets stay on the laptop; rendered
                              into compose env vars by _render_compose if
                              the app is configured with a secrets file)

    Globs the directory so adding a new YAML file (e.g. a future
    `proxmox.yaml` widget group) requires no code change here.
    """
    if not HOMEPAGE_CONFIG_LOCAL_DIR.is_dir():
        log.warning("homepage_config_skipped",
                    reason="source_missing", path=str(HOMEPAGE_CONFIG_LOCAL_DIR))
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

    for local in sorted(HOMEPAGE_CONFIG_LOCAL_DIR.glob("*.yaml")):
        # Skip compose (apps loop owns it) and sops-encrypted secrets.
        if local.name == "docker-compose.yaml":
            continue
        if local.name.endswith(".sops.yaml"):
            continue
        remote = f"{HOMEPAGE_CONFIG_REMOTE_DIR}/{local.name}"
        diff = ensure_file_on_nas(
            cli, _upload,
            local_path=local, remote_path=remote,
            mode=0o644, apply=ctx.apply,
        )
        log.info("homepage_config_ensured", path=remote,
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


def _ensure_netboot_menu_files_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    if not NETBOOT_BOOT_CFG_PATH.exists() or not NETBOOT_CUSTOM_IPXE_PATH.exists():
        log.warning("netboot_menu_skipped",
                    boot_cfg_exists=NETBOOT_BOOT_CFG_PATH.exists(),
                    custom_ipxe_exists=NETBOOT_CUSTOM_IPXE_PATH.exists())
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

    boot_diff, custom_diff = ensure_netboot_menu_files(
        cli, _upload,
        boot_cfg_path=NETBOOT_BOOT_CFG_PATH,
        custom_ipxe_path=NETBOOT_CUSTOM_IPXE_PATH,
        config_menus_dir=NETBOOT_CONFIG_MENUS_DIR,
        assets_dir=NETBOOT_ASSETS_DIR,
        apply=ctx.apply,
    )
    log.info("netboot_boot_cfg_ensured",
             path=f"{NETBOOT_CONFIG_MENUS_DIR}/boot.cfg",
             action=boot_diff.action, changed=boot_diff.changed)
    log.info("netboot_custom_ipxe_ensured",
             path=f"{NETBOOT_ASSETS_DIR}/custom.ipxe",
             action=custom_diff.action, changed=custom_diff.changed)


def _ensure_talos_updater_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Build a real upload closure from ctx.config and invoke ensure_talos_updater.

    Separated so tests of `run()` don't need a live /_upload endpoint; the
    standalone `ensure_talos_updater` is tested directly with a fake
    upload function.
    """
    # Skip if the source artifacts are missing (dev checkout oddity).
    if not TALOS_UPDATER_SCRIPT_PATH.exists():
        log.warning("talos_updater_skipped",
                    reason="script_missing", path=str(TALOS_UPDATER_SCRIPT_PATH))
        return
    if not TALOS_UPDATER_SCHEMATIC_PATH.exists():
        log.warning("talos_updater_skipped",
                    reason="schematic_missing", path=str(TALOS_UPDATER_SCHEMATIC_PATH))
        return

    from truenas_infra.client import upload_file

    host = ctx.config.truenas_host
    api_key = ctx.config.truenas_api_key
    verify_ssl = ctx.config.truenas_verify_ssl

    def _upload(*, local_path: Path, remote_path: str, mode: int) -> None:
        upload_file(
            cli,
            host=host, api_key=api_key, verify_ssl=verify_ssl,
            local_path=local_path, remote_path=remote_path, mode=mode,
        )

    cfg = load_talos_config(TALOS_UPDATER_CONFIG_PATH)
    log.info("talos_updater_config_loaded",
             version=cfg.version, retention=cfg.retention,
             arch=cfg.architecture, platform=cfg.platform)

    script_diff, schematic_diff, cron_diff = ensure_talos_updater(
        cli, _upload,
        script_path=TALOS_UPDATER_SCRIPT_PATH,
        schematic_path=TALOS_UPDATER_SCHEMATIC_PATH,
        remote_dir=TALOS_UPDATER_REMOTE_DIR,
        apply=ctx.apply,
        config=cfg,
    )
    log.info("talos_updater_script_ensured",
             path=TALOS_UPDATER_REMOTE_DIR + "/talos-updater.sh",
             action=script_diff.action, changed=script_diff.changed)
    log.info("talos_updater_schematic_ensured",
             path=TALOS_UPDATER_REMOTE_DIR + "/schematic.yaml",
             action=schematic_diff.action, changed=schematic_diff.changed)
    log.info("talos_updater_cronjob_ensured",
             action=cron_diff.action, changed=cron_diff.changed)
