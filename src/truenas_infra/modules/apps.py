"""Phase: apps — Custom App registration from committed compose YAML.

See docs/plans/zesty-drifting-castle.md §Phase 9.

Planned apps in initial scope:
  * pxe          → 10.10.5.10  (TFTP/iPXE + HTTP asset cache; our own
                                alpine+dnsmasq+nginx, no netboot.xyz)
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


def ensure_pxe_menu_files(
    cli: Any,
    upload_fn: Any,
    *,
    boot_cfg_path: Path,
    menu_ipxe_path: Path,
    submenus_dir: Path,
    tftp_dir: str,
    apply: bool,
) -> tuple[Diff, ...]:
    """Upload the Homelab PXE menu tree to the pxe container's TFTP
    root (bind-mounted from /mnt/tank/system/pxe/tftp):

    - boot.cfg → <tftp_dir>/boot.cfg — loaded first by menu.ipxe for
      runtime vars (site_name, cache_url, sigs_enabled).
    - menu.ipxe → <tftp_dir>/menu.ipxe — Homelab top-level menu
      (chained by the iPXE binary's embedded script).
    - <submenus_dir>/*.ipxe → <tftp_dir>/*.ipxe (flat) — our own
      sub-menus (talos.ipxe, bios.ipxe, utils.ipxe, linux.ipxe,
      live.ipxe). Flat, not nested, because iPXE chains them with
      relative paths from menu.ipxe and the TFTP CWD is the root.

    All files uploaded at 0o644 AND chowned to uid=1000, gid=1000.
    The chown is mandatory: the pxe container's dnsmasq runs as user
    `nbxyz` (UID 1000) with `--tftp-secure`, which refuses to serve
    files not readable by its own UID. Files that already existed in
    this dataset preserve their nbxyz ownership across filesystem.put
    updates, but newly created files land as root:root and are
    invisible to TFTP until chowned.

    Idempotent via size+mode check in ensure_file_on_nas. If submenus_dir
    does not exist, skip the sub-menu loop — phase apps stays working
    for bare-clone CI.
    """
    # UID/GID of the `nbxyz` user baked into the pxe container
    # (created in the Dockerfile, matches what the container's dnsmasq
    # and nginx drop to).
    NBXYZ_UID, NBXYZ_GID = 1000, 1000

    def upload_and_chown(local_path: Path, remote_path: str) -> Diff:
        diff = ensure_file_on_nas(
            cli, upload_fn,
            local_path=local_path, remote_path=remote_path,
            mode=0o644, apply=apply,
        )
        # Chown only when we actually wrote (create/update). No-op diffs
        # mean the file already exists with matching content; ownership
        # is presumed correct from a prior run.
        if apply and diff.changed:
            cli.call("filesystem.chown", {
                "path": remote_path, "uid": NBXYZ_UID, "gid": NBXYZ_GID,
            })
        return diff

    diffs: list[Diff] = []

    diffs.append(upload_and_chown(
        boot_cfg_path,
        f"{tftp_dir.rstrip('/')}/{boot_cfg_path.name}",
    ))
    diffs.append(upload_and_chown(
        menu_ipxe_path,
        f"{tftp_dir.rstrip('/')}/{menu_ipxe_path.name}",
    ))

    if submenus_dir.is_dir():
        for sub in sorted(submenus_dir.glob("*.ipxe")):
            diffs.append(upload_and_chown(
                sub,
                f"{tftp_dir.rstrip('/')}/{sub.name}",
            ))

    return tuple(diffs)


def ensure_pxe_build_context(
    cli: Any,
    upload_fn: Any,
    *,
    local_dir: Path,
    remote_dir: str,
    apply: bool,
) -> tuple[tuple[str, Diff], ...]:
    """Upload every file in apps/pxe/build/ to the NAS at remote_dir.

    The pxe container's docker-compose build.context points at this
    remote directory, so the Dockerfile + embed.ipxe + local-*.h +
    entrypoint.sh + nginx.conf must all exist there before TrueNAS
    runs `docker compose up`.

    Uploaded files preserve their executable bits: .sh files go as
    0755, everything else as 0644. Returns a list of (filename, diff)
    tuples so the caller can log per-file.

    Idempotent via size+mode check. Safe to re-run.
    """
    if not local_dir.is_dir():
        return ()

    diffs: list[tuple[str, Diff]] = []
    for f in sorted(local_dir.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(local_dir)
        mode = 0o755 if f.name.endswith(".sh") else 0o644
        remote = f"{remote_dir.rstrip('/')}/{rel}"
        diffs.append((str(rel), ensure_file_on_nas(
            cli, upload_fn,
            local_path=f, remote_path=remote,
            mode=mode, apply=apply,
        )))
    return tuple(diffs)


# ensure_pxe_cache removed 2026-04-22.
#
# Replaced by nginx reverse-proxy lazy cache (see
# apps/pxe/build/nginx.conf, proxy_cache_path + proxy_pass blocks).
# Instead of pre-downloading a curated list of assets via a weekly
# script, nginx downloads + caches on first client request and
# serves cached copies from that point on. Entries evict after 30d
# of non-use; hard cap 80 GB.
#
# Benefits over the old pre-cache model:
#   * Only fetches what's actually used — zero disk cost for menu
#     items nobody boots
#   * Self-maintaining: no URL-list to keep in sync with upstream
#     releases, nginx just caches whatever the client asks for
#   * Adding a new distro to the menu is a one-line menus/*.ipxe
#     edit — no cache-script changes needed


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
TALOS_UPDATER_SCRIPT_PATH = Path("apps/pxe/talos-updater.sh")
TALOS_UPDATER_SCHEMATIC_PATH = Path("apps/pxe/schematic.yaml")
TALOS_UPDATER_CONFIG_PATH = Path("config/talos.yaml")

# Homelab PXE menu tree — committed in this repo, uploaded to the
# pxe container's TFTP root (bind-mounted from /mnt/tank/system/pxe/tftp)
# on every `phase apps --apply`.
PXE_BOOT_CFG_PATH = Path("apps/pxe/boot.cfg")
PXE_MENU_IPXE_PATH = Path("apps/pxe/menu.ipxe")
PXE_SUBMENUS_DIR = Path("apps/pxe/menus")
PXE_TFTP_DIR = "/mnt/tank/system/pxe/tftp"
PXE_HTTP_DIR = "/mnt/tank/system/pxe/http"

# Container build context — Dockerfile, embed.ipxe, iPXE build inputs,
# entrypoint, nginx config. Uploaded to NAS so docker-compose's
# build.context directive finds them locally during image build.
PXE_BUILD_CONTEXT_DIR = Path("apps/pxe/build")
PXE_BUILD_CONTEXT_REMOTE_DIR = "/mnt/tank/system/apps-config/pxe/build"

# Local-asset scripts — run on the NAS host to populate
# /mnt/tank/system/pxe/http/extras/ with ISOs + auxiliary files and
# regenerate the dynamic menu listings. No proxying, no caching
# magic — just curl → local file → nginx static-serves.
PXE_DOWNLOAD_SCRIPT_PATH = Path("apps/pxe/pxe-download.sh")
PXE_GENMENU_SCRIPT_PATH  = Path("apps/pxe/pxe-genmenu.sh")
PXE_SCRIPTS_REMOTE_DIR   = "/mnt/tank/system/apps-config/pxe"

# bios-config PXE bios-apply image — built in the sibling `bios-config`
# repo by `./tools/build-bios-apply-img.sh`; uploaded here so
# `sanboot http://10.10.5.10:8080/bios-config/bios-apply.img` from
# custom.ipxe (the bios-apply menu entry) serves a valid image.
#
# Operator flow:
#   1. cd ~/Documents/github/bios-config && ./tools/build-bios-apply-img.sh
#   2. cd ~/Documents/github/truenas-infra && ./manage.sh phase apps --apply
# The phase is a no-op if the local image hasn't changed (size-based
# idempotency in ensure_file_on_nas). If the sibling checkout isn't
# present, the upload is skipped silently (phase apps keeps working
# for operators who don't have bios-config checked out locally).
BIOS_APPLY_LOCAL_PATH = Path("../bios-config/build/bios-apply.img")

# hw-validation PXE live image — built in the sibling `hw-validation`
# repo by `./tools/build-image.sh`. Produces a 4-file set under
# build/<alpine-version>-r0/ and a `latest` symlink pointing to it:
#   vmlinuz-lts      ~11 MB  (stock Alpine kernel)
#   initramfs-lts    ~24 MB  (stock Alpine init)
#   modloop-lts     ~183 MB  (squashfs of kernel modules, fetched at boot)
#   overlay.cpio.gz  (grows with test scripts; Phase A ~8 KB)
#
# Uploaded to /mnt/tank/system/pxe/http/hw-validation/latest/ so the
# hw-validation.ipxe sub-menu's `kernel` + `initrd` URLs resolve.
# No-op if sibling repo isn't checked out or hasn't been built --
# mirrors the bios-apply.img pattern.
HW_VALIDATION_LOCAL_DIR = Path("../hw-validation/build/latest")
HW_VALIDATION_REMOTE_DIR = "/mnt/tank/system/pxe/http/hw-validation/latest"
# Files the hw-validation.ipxe menu references. If the build produces
# more files later (e.g. memtest86+ sister payload), append here.
HW_VALIDATION_PAYLOAD_FILES = (
    "vmlinuz-lts",
    "initramfs-lts",
    "modloop-lts",
    # apkovl.tar.gz: our scripts + /etc/apk/world + /etc/apk/repositories.
    # Fetched by the Alpine netboot init over HTTP (apkovl=<URL> cmdline),
    # NOT loaded as a second initrd — the netboot init untars it after
    # fetching modloop, then runs `apk add` to install /sbin/init.
    "apkovl.tar.gz",
    "version.txt",
)
BIOS_APPLY_REMOTE_PATH = "/mnt/tank/system/pxe/http/bios-config/bios-apply.img"

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

# stress-dashboard — FastAPI + Jinja2 app that renders hw-validation JSON
# reports from /mnt/tank/system/stress-results/ (mounted RW so the UI can
# delete individual reports). Same deploy pattern as amtctl: the container
# is a stock python:3.13-alpine image, and we upload main.py + templates/
# to the pool where they get bind-mounted into /app. No image to build.
STRESS_DASHBOARD_LOCAL_DIR = Path("apps/stress-dashboard")
STRESS_DASHBOARD_CODE_REMOTE_DIR = "/mnt/tank/system/apps-config/stress-dashboard/code"


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
    if only in (None, "stress-dashboard"):
        _ensure_stress_dashboard_config_via_ctx(cli, ctx, log)

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

    # 4. Homelab PXE — independent TFTP/HTTP stack (apps/pxe/). Uploads
    # the container build context (Dockerfile + iPXE build inputs),
    # the TFTP-served menu tree (menu.ipxe, boot.cfg, menus/*.ipxe),
    # and installs the pxe-cache cronjob (weekly mirror of distro /
    # utility assets from upstream origins).
    if only in (None, "pxe"):
        _ensure_pxe_build_context_via_ctx(cli, ctx, log)
        _ensure_pxe_menu_files_via_ctx(cli, ctx, log)
        _ensure_pxe_scripts_via_ctx(cli, ctx, log)
        _ensure_pxe_bios_apply_img_via_ctx(cli, ctx, log)
        _ensure_pxe_hw_validation_via_ctx(cli, ctx, log)

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


def _ensure_stress_dashboard_config_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload stress-dashboard app source (main.py + templates/) to the pool.

    Layout on the pool:
        .../apps-config/stress-dashboard/code/   — bind-mounted /app
            ├── main.py
            └── templates/*.html

    Reads from /mnt/tank/system/stress-results/ at runtime (bind-mounted /data,
    RW so the delete button works). That dataset is provisioned in
    config/storage.yaml + shares.yaml — not in scope here.

    Files NOT uploaded: docker-compose.yaml (owned by ensure_custom_app).
    """
    if not STRESS_DASHBOARD_LOCAL_DIR.is_dir():
        log.warning("stress_dashboard_config_skipped",
                    reason="source_missing", path=str(STRESS_DASHBOARD_LOCAL_DIR))
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

    SKIP = {"docker-compose.yaml"}
    for local in sorted(STRESS_DASHBOARD_LOCAL_DIR.rglob("*")):
        if not local.is_file():
            continue
        if local.name in SKIP:
            continue
        rel = local.relative_to(STRESS_DASHBOARD_LOCAL_DIR).as_posix()
        remote = f"{STRESS_DASHBOARD_CODE_REMOTE_DIR}/{rel}"
        diff = ensure_file_on_nas(
            cli, _upload,
            local_path=local, remote_path=remote,
            mode=0o644, apply=ctx.apply,
        )
        log.info("stress_dashboard_file_ensured", path=remote,
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


def _pxe_upload_helper(cli: Any, ctx: Any) -> Any:
    """Build the upload callable the ensure_pxe_* helpers expect.

    Extracted from the three via_ctx functions below so they all share
    the same filesystem.put wrapper without re-stating the boilerplate.
    """
    from truenas_infra.client import upload_file
    host = ctx.config.truenas_host
    api_key = ctx.config.truenas_api_key
    verify_ssl = ctx.config.truenas_verify_ssl

    def _upload(*, local_path: Path, remote_path: str, mode: int) -> None:
        upload_file(
            cli, host=host, api_key=api_key, verify_ssl=verify_ssl,
            local_path=local_path, remote_path=remote_path, mode=mode,
        )
    return _upload


def _ensure_pxe_build_context_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload apps/pxe/build/** to the NAS so docker-compose's
    build.context directive can read them at image-build time."""
    if not PXE_BUILD_CONTEXT_DIR.is_dir():
        log.warning("pxe_build_context_skipped",
                    local_dir_exists=False,
                    local_dir=str(PXE_BUILD_CONTEXT_DIR))
        return

    diffs = ensure_pxe_build_context(
        cli, _pxe_upload_helper(cli, ctx),
        local_dir=PXE_BUILD_CONTEXT_DIR,
        remote_dir=PXE_BUILD_CONTEXT_REMOTE_DIR,
        apply=ctx.apply,
    )
    for name, diff in diffs:
        log.info("pxe_build_context_ensured",
                 path=f"{PXE_BUILD_CONTEXT_REMOTE_DIR}/{name}",
                 action=diff.action, changed=diff.changed)


def _ensure_pxe_menu_files_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    if not PXE_BOOT_CFG_PATH.exists() or not PXE_MENU_IPXE_PATH.exists():
        log.warning("pxe_menu_skipped",
                    boot_cfg_exists=PXE_BOOT_CFG_PATH.exists(),
                    menu_ipxe_exists=PXE_MENU_IPXE_PATH.exists())
        return

    diffs = ensure_pxe_menu_files(
        cli, _pxe_upload_helper(cli, ctx),
        boot_cfg_path=PXE_BOOT_CFG_PATH,
        menu_ipxe_path=PXE_MENU_IPXE_PATH,
        submenus_dir=PXE_SUBMENUS_DIR,
        tftp_dir=PXE_TFTP_DIR,
        apply=ctx.apply,
    )
    # Reconstruct the upload order so we can log per-file. Matches the
    # exact sequence inside ensure_pxe_menu_files: boot.cfg, then
    # menu.ipxe, then sorted submenus/*.ipxe.
    names: list[str] = [PXE_BOOT_CFG_PATH.name, PXE_MENU_IPXE_PATH.name]
    if PXE_SUBMENUS_DIR.is_dir():
        names.extend(p.name for p in sorted(PXE_SUBMENUS_DIR.glob("*.ipxe")))
    for name, diff in zip(names, diffs):
        log.info("pxe_menu_ensured",
                 path=f"{PXE_TFTP_DIR}/{name}",
                 action=diff.action, changed=diff.changed)


def _ensure_pxe_scripts_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload pxe-download.sh + pxe-genmenu.sh to the NAS and register
    a cronjob that triggers the downloader. Script runs curl on the
    NAS to pull each curated asset into
    /mnt/tank/system/pxe/http/extras/, then calls pxe-genmenu.sh to
    rewrite the auto-generated utils/distros/live menus based on
    whatever ISOs it finds.

    Cronjob schedule: weekly Sunday 02:30. Operator can trigger
    on-demand via `midclt call cronjob.run <id>` (e.g. after editing
    the script to add a new ISO URL)."""
    upload = _pxe_upload_helper(cli, ctx)
    diffs: list[tuple[str, Diff]] = []
    for p in (PXE_DOWNLOAD_SCRIPT_PATH, PXE_GENMENU_SCRIPT_PATH):
        if not p.exists():
            continue
        remote = f"{PXE_SCRIPTS_REMOTE_DIR}/{p.name}"
        d = ensure_file_on_nas(
            cli, upload,
            local_path=p, remote_path=remote,
            mode=0o755, apply=ctx.apply,
        )
        diffs.append((p.name, d))
        log.info("pxe_script_ensured", path=remote,
                 action=d.action, changed=d.changed)

    # Weekly cronjob for the downloader (which also calls genmenu
    # at the end). Sunday 02:30 because the talos-updater runs at
    # 03:00 and we don't want them overlapping on the ZFS pool.
    remote_dl = f"{PXE_SCRIPTS_REMOTE_DIR}/{PXE_DOWNLOAD_SCRIPT_PATH.name}"
    cron_cmd = (
        f'/bin/bash -c "/bin/bash {remote_dl} '
        f'>> {PXE_SCRIPTS_REMOTE_DIR}/pxe-download.log 2>&1"'
    )
    cron = ensure_cronjob(
        cli,
        description="pxe-download",
        command=cron_cmd,
        schedule={"minute": "30", "hour": "2", "dom": "*", "month": "*", "dow": "0"},
        user="root",
        apply=ctx.apply,
    )
    log.info("pxe_download_cronjob_ensured",
             action=cron.action, changed=cron.changed)


def _ensure_pxe_bios_apply_img_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload bios-config's bios-apply.img if the sibling repo has built it.

    Used to live inside the old _ensure_pxe_cache_via_ctx (since the
    cache script and the img upload ran in the same code path). With
    the cache script retired (→ nginx lazy cache), the img upload
    now stands alone.
    """
    # bios-config bios-apply.img (optional; sibling repo build artefact).
    # No-op when the sibling repo isn't checked out at ../bios-config —
    # keeps phase apps working for operators who don't use bios-config.
    #
    # NOTE ON IDEMPOTENCY: the default `ensure_file_on_nas` compares sizes
    # only, which is useless here — the FAT image is fixed at 16 MB even
    # when contents change. Since the image is small (~16 MB) and rebuilds
    # are rare (operator-driven, not cronjob), we just always re-upload
    # and log noop vs update via local mtime comparison. Safer than
    # trying to compute a remote hash.
    bios_img = BIOS_APPLY_LOCAL_PATH.resolve()
    if not bios_img.is_file():
        log.info("bios_apply_img_skipped",
                 reason="sibling build artefact not present",
                 expected_local=str(bios_img))
    elif not ctx.apply:
        log.info("bios_apply_img_ensured",
                 path=BIOS_APPLY_REMOTE_PATH,
                 action="would_upload", changed=True,
                 local_size=bios_img.stat().st_size,
                 note="dry-run — size-based idempotency unreliable for this artefact")
    else:
        import hashlib
        local_sha = hashlib.sha256(bios_img.read_bytes()).hexdigest()[:16]
        _pxe_upload_helper(cli, ctx)(
            local_path=bios_img, remote_path=BIOS_APPLY_REMOTE_PATH,
            mode=0o644,
        )
        log.info("bios_apply_img_ensured",
                 path=BIOS_APPLY_REMOTE_PATH,
                 action="uploaded", changed=True,
                 local_size=bios_img.stat().st_size,
                 local_sha256=local_sha)


def _ensure_pxe_hw_validation_via_ctx(cli: Any, ctx: Any, log: Any) -> None:
    """Upload hw-validation's Alpine live-image artefacts if the sibling
    repo has built them.

    No-op when the sibling repo isn't checked out at ../hw-validation
    or `./tools/build-image.sh` hasn't run yet (in which case
    build/latest/ is missing). Keeps `phase apps` working for operators
    who don't use hw-validation.

    Size-based idempotency via ensure_file_on_nas is safe here: each
    file's size is determined by content (kernel + Alpine version),
    not by a fixed padding like bios-apply.img. A rebuild of the same
    Alpine version produces byte-identical vmlinuz / initramfs /
    modloop; only overlay.cpio.gz + version.txt change with our own
    edits, and their sizes do change.
    """
    local_dir = HW_VALIDATION_LOCAL_DIR.resolve()
    if not local_dir.is_dir():
        log.info("hw_validation_skipped",
                 reason="sibling build artefact not present",
                 expected_local=str(local_dir))
        return

    # Resolve any symlink (`build/latest -> 3.21.0-r0/`) so stat + mtime
    # reflect the real target, not the link.
    try:
        real_local_dir = local_dir.resolve()
    except OSError as e:
        log.warning("hw_validation_skipped",
                    reason=f"resolve failed: {e}",
                    expected_local=str(local_dir))
        return

    upload = _pxe_upload_helper(cli, ctx)
    for name in HW_VALIDATION_PAYLOAD_FILES:
        local = real_local_dir / name
        if not local.is_file():
            log.info("hw_validation_file_skipped",
                     reason="expected payload file missing",
                     expected_local=str(local))
            continue
        remote = f"{HW_VALIDATION_REMOTE_DIR}/{name}"
        diff = ensure_file_on_nas(
            cli, upload,
            local_path=local, remote_path=remote,
            mode=0o644, apply=ctx.apply,
        )
        log.info("hw_validation_file_ensured",
                 path=remote, action=diff.action, changed=diff.changed,
                 local_size=local.stat().st_size)


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
