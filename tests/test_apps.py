"""Tests for modules/apps.py — phase 9 (Custom App deployment from compose)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── ensure_docker_pool ──────────────────────────────────────────────────────


def test_ensure_docker_pool_sets_when_unset() -> None:
    from truenas_infra.modules.apps import ensure_docker_pool

    live = {"id": 1, "pool": None, "dataset": None}
    cli = _mk_cli([live, {**live, "pool": "tank"}])

    diff = ensure_docker_pool(cli, pool_name="tank", apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "docker.update")
    assert update.args[1]["pool"] == "tank"


def test_ensure_docker_pool_noop_when_match() -> None:
    from truenas_infra.modules.apps import ensure_docker_pool

    live = {"id": 1, "pool": "tank", "dataset": "tank/.ix-apps"}
    cli = _mk_cli([live])
    diff = ensure_docker_pool(cli, pool_name="tank", apply=True)
    assert diff.changed is False


# ─── load_apps_config ────────────────────────────────────────────────────────


def test_load_apps_config_parses_enabled_apps(tmp_path: Path) -> None:
    from truenas_infra.modules.apps import load_apps_config

    yaml_file = tmp_path / "apps.yaml"
    yaml_file.write_text(
        textwrap.dedent(
            """
            apps:
              - name: netboot-xyz
                enabled: true
                compose: apps/netboot-xyz/docker-compose.yaml
                secrets: null
                bind_ip: 10.10.5.10
              - name: minio-prd
                enabled: true
                compose: apps/minio-prd/docker-compose.yaml
                secrets: apps/minio-prd/secrets.sops.yaml
                bind_ip: 10.10.10.10
              - name: plex
                enabled: false
                compose: apps/plex/docker-compose.yaml
                bind_ip: 10.10.20.10
            """
        ).strip()
    )

    cfg = load_apps_config(yaml_file)

    # Disabled apps should NOT be returned.
    assert len(cfg.apps) == 2
    names = [a.name for a in cfg.apps]
    assert "netboot-xyz" in names
    assert "minio-prd" in names
    assert "plex" not in names


# ─── ensure_custom_app ───────────────────────────────────────────────────────


def test_ensure_custom_app_creates_when_missing(tmp_path: Path) -> None:
    from truenas_infra.modules.apps import AppSpec, ensure_custom_app

    compose_path = tmp_path / "docker-compose.yaml"
    compose_path.write_text("services:\n  foo:\n    image: hello-world\n")

    cli = _mk_cli([
        [],                                       # app.query
        {"id": "netboot-xyz", "state": "RUNNING"},
    ])

    spec = AppSpec(name="netboot-xyz", compose_path=compose_path, secrets_path=None)
    diff = ensure_custom_app(cli, spec=spec, apply=True)

    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "app.create")
    payload = create.args[1]
    assert payload["app_name"] == "netboot-xyz"
    assert payload["custom_app"] is True
    assert "services:" in payload["custom_compose_config_string"]


def test_ensure_custom_app_noop_when_compose_matches(tmp_path: Path) -> None:
    """Note: TrueNAS doesn't let us read back the custom compose directly,
    so we hash + compare. If there's a matching app already, treat as noop
    for now; a `--force` update path can come later.
    """
    from truenas_infra.modules.apps import AppSpec, ensure_custom_app

    compose_path = tmp_path / "docker-compose.yaml"
    compose_path.write_text("services:\n  foo:\n    image: hello-world\n")

    existing = {"id": "netboot-xyz", "name": "netboot-xyz", "state": "RUNNING", "custom_app": True}
    cli = _mk_cli([[existing]])

    spec = AppSpec(name="netboot-xyz", compose_path=compose_path, secrets_path=None)
    diff = ensure_custom_app(cli, spec=spec, apply=True)

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "app.create" not in names


# ─── run() orchestration ─────────────────────────────────────────────────────


class _CfgStub:
    truenas_host = "10.10.5.10"
    truenas_api_key = "test-key"
    truenas_verify_ssl = False


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        self.config = _CfgStub()
        import structlog
        self.log = structlog.get_logger("test")


def test_render_compose_substitutes_vars_from_secrets(tmp_path: Path, monkeypatch) -> None:
    """${VAR} references in compose are replaced by values loaded from SOPS."""
    from truenas_infra.modules import apps as apps_module

    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        "services:\n  minio:\n    environment:\n"
        "      - MINIO_ROOT_USER=${MINIO_ROOT_USER}\n"
        "      - MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}\n"
    )

    # Monkey-patch the SOPS loader to avoid actually running sops in tests.
    monkeypatch.setattr(
        apps_module, "_load_sops_dotenv",
        lambda _p: {"MINIO_ROOT_USER": "admin", "MINIO_ROOT_PASSWORD": "s3cret"},
    )

    rendered = apps_module._render_compose(compose, tmp_path / "secrets.sops.yaml")

    assert "MINIO_ROOT_USER=admin" in rendered
    assert "MINIO_ROOT_PASSWORD=s3cret" in rendered
    # No un-substituted placeholders left.
    assert "${" not in rendered


def test_render_compose_passes_through_when_no_secrets(tmp_path: Path) -> None:
    from truenas_infra.modules import apps as apps_module

    compose = tmp_path / "docker-compose.yaml"
    compose.write_text("services:\n  foo:\n    image: hello-world\n")

    rendered = apps_module._render_compose(compose, None)
    assert rendered == compose.read_text()


def test_ensure_cronjob_creates_when_missing() -> None:
    from truenas_infra.modules.apps import ensure_cronjob

    cli = _mk_cli([[], {"id": 1}])
    diff = ensure_cronjob(
        cli,
        description="talos-updater",
        command="/bin/sh /path/to/updater.sh",
        schedule={"minute": "0", "hour": "3", "dom": "*", "month": "*", "dow": "*"},
        apply=True,
    )
    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "cronjob.create")
    payload = create.args[1]
    assert payload["description"] == "talos-updater"
    assert payload["command"] == "/bin/sh /path/to/updater.sh"
    assert payload["enabled"] is True
    assert payload["schedule"]["hour"] == "3"


def test_ensure_cronjob_noop_when_exists_with_same_description_and_command() -> None:
    from truenas_infra.modules.apps import ensure_cronjob

    cmd = "/bin/sh /path/to/updater.sh >> /tmp/log 2>&1"
    sched = {"minute": "0", "hour": "3", "dom": "*", "month": "*", "dow": "*"}
    existing = [{
        "id": 5, "description": "talos-updater", "enabled": True,
        "command": cmd, "user": "root", "schedule": sched,
    }]
    cli = _mk_cli([existing])
    diff = ensure_cronjob(
        cli, description="talos-updater", command=cmd, schedule=sched,
        apply=True,
    )
    assert diff.changed is False


def test_ensure_cronjob_updates_when_command_differs() -> None:
    """Idempotency: if the cronjob exists but the command has drifted,
    update it in-place via cronjob.update."""
    from truenas_infra.modules.apps import ensure_cronjob

    sched = {"minute": "0", "hour": "3", "dom": "*", "month": "*", "dow": "*"}
    existing = [{
        "id": 7, "description": "talos-updater", "enabled": True,
        "command": "/old/command", "user": "root", "schedule": sched,
    }]
    cli = _mk_cli([existing, {"id": 7, "command": "/new/command"}])

    diff = ensure_cronjob(
        cli, description="talos-updater", command="/new/command",
        schedule=sched, apply=True,
    )

    assert diff.changed is True
    assert diff.action == "update"
    update = next(c for c in cli.call.call_args_list if c.args[0] == "cronjob.update")
    assert update.args[1] == 7  # id
    assert update.args[2]["command"] == "/new/command"


def test_run_configures_docker_pool_and_apps(tmp_path: Path) -> None:
    from truenas_infra.modules.apps import run

    # Minimal compose
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "netboot-xyz").mkdir()
    compose = tmp_path / "apps" / "netboot-xyz" / "docker-compose.yaml"
    compose.write_text("services:\n  netboot:\n    image: ghcr.io/netbootxyz/netbootxyz:latest\n")

    cfg_path = tmp_path / "apps.yaml"
    cfg_path.write_text(
        textwrap.dedent(
            f"""
            apps:
              - name: netboot-xyz
                enabled: true
                compose: {compose}
                bind_ip: 10.10.5.10
            """
        ).strip()
    )

    # Script + schematic already match (we provide matching sizes), so no
    # filesystem.put upload is triggered — test doesn't need a live NAS.
    from pathlib import Path as _P
    script_size = _P("apps/netboot-xyz/talos-updater.sh").stat().st_size
    schematic_size = _P("apps/netboot-xyz/schematic.yaml").stat().st_size

    # Pre-compute the command ensure_cronjob expects so the mock returns an
    # existing cronjob with NO drift (otherwise cronjob.update is called and
    # we'd need another yielded response).
    from truenas_infra.modules.apps import _talos_updater_cronjob_command
    expected_cmd = _talos_updater_cronjob_command(
        "/mnt/tank/system/apps-config/talos-updater/talos-updater.sh"
    )
    expected_schedule = {"minute": "0", "hour": "3", "dom": "*", "month": "*", "dow": "*"}

    # Menu file sizes so the boot.cfg + custom.ipxe stat calls appear as
    # already-uploaded (avoids a live upload attempt during the test).
    boot_cfg_size = _P("apps/netboot-xyz/boot.cfg").stat().st_size
    custom_ipxe_size = _P("apps/netboot-xyz/custom.ipxe").stat().st_size
    tls_export_size = _P("apps/tls/tls-export.sh").stat().st_size
    tls_rotate_size = _P("apps/tls/tls-rotate.sh").stat().st_size

    # Cronjob.query for tls-rotate — pre-shaped to match expected command
    # so ensure_cronjob reports noop without needing a cronjob.update call.
    from truenas_infra.modules.apps import _tls_rotate_cronjob_command
    expected_tls_cmd = _tls_rotate_cronjob_command(
        "/mnt/tank/system/tls/tls-rotate.sh"
    )
    expected_hourly = {"minute": "0", "hour": "*", "dom": "*", "month": "*", "dow": "*"}

    cli = _mk_cli([
        {"id": 1, "pool": "tank", "dataset": "tank/.ix-apps"},  # docker.config (noop)
        [],                                                      # app.query
        {"id": "netboot-xyz"},                                   # app.create
        {"size": script_size, "mode": 0o100755},                # filesystem.stat script
        {"size": schematic_size, "mode": 0o100644},             # filesystem.stat schematic
        [{                                                       # cronjob.query — no drift
            "id": 1, "description": "talos-updater", "enabled": True,
            "command": expected_cmd, "user": "root", "schedule": expected_schedule,
        }],
        {"size": boot_cfg_size, "mode": 0o100644},              # filesystem.stat boot.cfg
        {"size": custom_ipxe_size, "mode": 0o100644},           # filesystem.stat custom.ipxe
        {"size": tls_export_size, "mode": 0o100755},            # filesystem.stat tls-export
        {"size": tls_rotate_size, "mode": 0o100755},            # filesystem.stat tls-rotate
        [{                                                       # cronjob.query tls-rotate
            "id": 2, "description": "tls-rotate", "enabled": True,
            "command": expected_tls_cmd, "user": "root", "schedule": expected_hourly,
        }],
        {"size": _P("apps/traefik/routes.yaml").stat().st_size,
         "mode": 0o100644},                                      # filesystem.stat traefik routes
    ])

    rc = run(
        cli, _Ctx(apply=True), only=None,
        config_path=cfg_path, pool_name="tank",
    )

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "docker.config" in names
    assert "app.create" in names
    assert "filesystem.stat" in names
    assert "cronjob.query" in names


# ─── ensure_file_on_nas ──────────────────────────────────────────────────────


def test_ensure_file_on_nas_uploads_when_missing(tmp_path: Path) -> None:
    """File not on NAS → upload_fn called with expected args."""
    from truenas_api_client.exc import ClientException
    from truenas_infra.modules.apps import ensure_file_on_nas

    local = tmp_path / "talos-updater.sh"
    local.write_bytes(b"#!/bin/sh\necho hi\n")

    cli = MagicMock()
    # filesystem.stat raises when file doesn't exist
    cli.call.side_effect = ClientException("does not exist")

    uploads: list[dict] = []
    def fake_upload(*, local_path, remote_path, mode):
        uploads.append({"local_path": local_path, "remote_path": remote_path, "mode": mode})

    diff = ensure_file_on_nas(
        cli, fake_upload,
        local_path=local,
        remote_path="/mnt/tank/system/apps-config/talos-updater/talos-updater.sh",
        mode=0o755,
        apply=True,
    )

    assert diff.changed is True
    assert diff.action == "create"
    assert len(uploads) == 1
    assert uploads[0]["local_path"] == local
    assert uploads[0]["remote_path"] == "/mnt/tank/system/apps-config/talos-updater/talos-updater.sh"
    assert uploads[0]["mode"] == 0o755


def test_ensure_file_on_nas_noop_when_size_matches(tmp_path: Path) -> None:
    """File on NAS with same size → no upload."""
    from truenas_infra.modules.apps import ensure_file_on_nas

    local = tmp_path / "talos-updater.sh"
    content = b"#!/bin/sh\necho hi\n"
    local.write_bytes(content)

    cli = MagicMock()
    cli.call.return_value = {"size": len(content), "mode": 0o100755}

    uploads: list = []
    def fake_upload(**_kw):
        uploads.append(_kw)

    diff = ensure_file_on_nas(
        cli, fake_upload,
        local_path=local,
        remote_path="/mnt/tank/x.sh",
        mode=0o755,
        apply=True,
    )

    assert diff.changed is False
    assert diff.action == "noop"
    assert uploads == []


def test_ensure_file_on_nas_reuploads_when_size_differs(tmp_path: Path) -> None:
    """File on NAS with different size → re-upload."""
    from truenas_infra.modules.apps import ensure_file_on_nas

    local = tmp_path / "talos-updater.sh"
    local.write_bytes(b"#!/bin/sh\nnew content\n")

    cli = MagicMock()
    cli.call.return_value = {"size": 8, "mode": 0o100755}  # stale, wrong size

    uploads: list = []
    def fake_upload(**kw):
        uploads.append(kw)

    diff = ensure_file_on_nas(
        cli, fake_upload,
        local_path=local,
        remote_path="/mnt/tank/x.sh",
        mode=0o755,
        apply=True,
    )

    assert diff.changed is True
    assert diff.action == "update"
    assert len(uploads) == 1


def test_ensure_file_on_nas_dry_run_does_not_upload(tmp_path: Path) -> None:
    """apply=False → never calls upload_fn."""
    from truenas_api_client.exc import ClientException
    from truenas_infra.modules.apps import ensure_file_on_nas

    local = tmp_path / "x.sh"
    local.write_bytes(b"abc\n")

    cli = MagicMock()
    cli.call.side_effect = ClientException("missing")

    uploads: list = []
    def fake_upload(**kw):
        uploads.append(kw)

    diff = ensure_file_on_nas(
        cli, fake_upload,
        local_path=local, remote_path="/mnt/tank/x.sh",
        mode=0o755, apply=False,
    )

    assert diff.changed is True
    assert uploads == []


# ─── ensure_talos_updater (automated via filesystem.put) ─────────────────────


def test_ensure_talos_updater_uploads_script_and_registers_short_cronjob(tmp_path: Path) -> None:
    """End-to-end: schematic + script uploaded, short cronjob registered.

    The cronjob command MUST fit in TrueNAS's 1024-char limit; this is the
    whole reason we pivoted away from the inline-script approach.
    """
    from truenas_infra.modules.apps import ensure_talos_updater

    script = tmp_path / "talos-updater.sh"
    script.write_bytes(b"#!/bin/sh\necho talos\n")
    schematic = tmp_path / "schematic.yaml"
    schematic.write_bytes(b"customization: {}\n")

    from truenas_api_client.exc import ClientException

    def _cli_sequence():
        # filesystem.stat for script → missing
        yield ClientException("missing")
        # filesystem.stat for schematic → missing
        yield ClientException("missing")
        # cronjob.query → none
        yield []
        # cronjob.create → new
        yield {"id": 42, "description": "talos-updater"}

    seq = _cli_sequence()
    cli = MagicMock()
    def _call(*a, **k):
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v
    cli.call.side_effect = _call

    uploads: list = []
    def fake_upload(**kw):
        uploads.append(kw)

    diffs = ensure_talos_updater(
        cli, fake_upload,
        script_path=script,
        schematic_path=schematic,
        remote_dir="/mnt/tank/system/apps-config/talos-updater",
        apply=True,
    )

    # Two uploads happened: script (0o755) and schematic (0o644).
    assert len(uploads) == 2
    modes = {u["remote_path"]: u["mode"] for u in uploads}
    assert modes["/mnt/tank/system/apps-config/talos-updater/talos-updater.sh"] == 0o755
    assert modes["/mnt/tank/system/apps-config/talos-updater/schematic.yaml"] == 0o644

    # A cronjob was created with a SHORT command that just invokes the script.
    create = next(c for c in cli.call.call_args_list if c.args[0] == "cronjob.create")
    payload = create.args[1]
    assert payload["description"] == "talos-updater"
    assert len(payload["command"]) <= 1024, "cronjob.command must fit TrueNAS's limit"
    assert "/mnt/tank/system/apps-config/talos-updater/talos-updater.sh" in payload["command"]
    # Output must be captured to a log file (TrueNAS doesn't retain cron
    # stdout/stderr in a queryable way; a log next to the script is how
    # the operator debugs failures).
    assert "talos-updater.log" in payload["command"]
    assert "2>&1" in payload["command"]
    # The command MUST be wrapped in `/bin/bash -c "..."`. TrueNAS's
    # `cronjob.run` path doesn't invoke a shell before exec'ing the
    # command, so top-level `>>` / `2>&1` tokens are treated as literal
    # argv entries and the redirect silently doesn't happen. Wrapping in
    # `/bin/bash -c` makes bash interpret the redirect.
    assert payload["command"].startswith("/bin/bash -c "), (
        f"command must start with /bin/bash -c, got: {payload['command']!r}"
    )
    assert payload["schedule"]["hour"] == "3"

    # diffs is a list/tuple with per-artifact results for logging.
    assert len(diffs) == 3  # script, schematic, cronjob
    assert all(d.changed for d in diffs)


# ─── TLS rotate cronjob ──────────────────────────────────────────────────────


def test_tls_rotate_cronjob_command_wraps_in_bash() -> None:
    """Same cronjob.run gotcha as talos-updater: must wrap in /bin/bash -c
    so `>>` and `2>&1` actually redirect."""
    from truenas_infra.modules.apps import _tls_rotate_cronjob_command

    cmd = _tls_rotate_cronjob_command("/mnt/tank/system/tls/tls-rotate.sh")
    assert cmd.startswith("/bin/bash -c "), cmd
    assert "/mnt/tank/system/tls/tls-rotate.sh" in cmd
    assert "tls-rotate.log" in cmd
    assert "2>&1" in cmd
    assert len(cmd) <= 1024, f"must fit TrueNAS cronjob.command cap, got {len(cmd)}"


def test_ensure_tls_rotate_uploads_both_scripts_and_registers_hourly_cronjob(
    tmp_path: Path,
) -> None:
    """phase apps uploads tls-export.sh + tls-rotate.sh to /mnt/tank/system/tls/
    and registers an hourly cronjob pointing at tls-rotate.sh."""
    from truenas_api_client.exc import ClientException
    from truenas_infra.modules.apps import ensure_tls_rotate

    export = tmp_path / "tls-export.sh"
    export.write_bytes(b"#!/bin/sh\n# export\n")
    rotate = tmp_path / "tls-rotate.sh"
    rotate.write_bytes(b"#!/bin/sh\n# rotate\n")

    # Sequenced mock: filesystem.stat raises (file missing → upload),
    # cronjob.query returns [], cronjob.create returns the created job.
    calls = iter([
        ClientException("missing"),                         # filesystem.stat export
        ClientException("missing"),                         # filesystem.stat rotate
        [],                                                 # cronjob.query
        {"id": 99, "description": "tls-rotate"},            # cronjob.create
    ])
    def _side_effect(*a, **kw):
        v = next(calls)
        if isinstance(v, Exception):
            raise v
        return v
    cli = MagicMock()
    cli.call.side_effect = _side_effect

    uploads: list = []
    def fake_upload(**kw):
        uploads.append(kw)

    diffs = ensure_tls_rotate(
        cli, fake_upload,
        export_path=export,
        rotate_path=rotate,
        remote_dir="/mnt/tank/system/tls",
        apply=True,
    )

    # Two uploads, both 0755.
    assert len(uploads) == 2
    modes = {u["remote_path"]: u["mode"] for u in uploads}
    assert modes["/mnt/tank/system/tls/tls-export.sh"] == 0o755
    assert modes["/mnt/tank/system/tls/tls-rotate.sh"] == 0o755

    # Hourly cronjob (minute=0, * hour/day/month/dow).
    create = next(c for c in cli.call.call_args_list if c.args[0] == "cronjob.create")
    payload = create.args[1]
    assert payload["description"] == "tls-rotate"
    assert payload["schedule"]["minute"] == "0"
    assert payload["schedule"]["hour"] == "*"
    assert "/mnt/tank/system/tls/tls-rotate.sh" in payload["command"]
    assert len(diffs) == 3  # export + rotate + cronjob


def test_ensure_netboot_menu_files_uploads_boot_cfg_and_custom_ipxe(tmp_path: Path) -> None:
    """phase apps uploads boot.cfg to /config/menus/ and custom.ipxe to /assets/."""
    from truenas_api_client.exc import ClientException
    from truenas_infra.modules.apps import ensure_netboot_menu_files

    boot_cfg = tmp_path / "boot.cfg"
    boot_cfg.write_bytes(b"#!ipxe\nset custom_url http://10.10.5.10:8080\n")
    custom_ipxe = tmp_path / "custom.ipxe"
    custom_ipxe.write_bytes(b"#!ipxe\n:start\nmenu Homelab\nexit\n")

    cli = MagicMock()
    cli.call.side_effect = ClientException("missing")

    uploads: list = []
    def fake_upload(**kw):
        uploads.append(kw)

    diffs = ensure_netboot_menu_files(
        cli, fake_upload,
        boot_cfg_path=boot_cfg,
        custom_ipxe_path=custom_ipxe,
        config_menus_dir="/mnt/tank/system/pxe/config/menus",
        assets_dir="/mnt/tank/system/pxe/assets",
        apply=True,
    )

    # Two uploads expected, both 0o644.
    assert len(uploads) == 2
    paths = {u["remote_path"]: u["mode"] for u in uploads}
    assert paths["/mnt/tank/system/pxe/config/menus/boot.cfg"] == 0o644
    assert paths["/mnt/tank/system/pxe/assets/custom.ipxe"] == 0o644
    assert all(d.changed for d in diffs)


def test_ensure_talos_updater_all_noop_when_state_matches(tmp_path: Path) -> None:
    """Re-running: all three artifacts already match → all noop."""
    from truenas_infra.modules.apps import (
        _talos_updater_cronjob_command,
        ensure_talos_updater,
    )

    script = tmp_path / "talos-updater.sh"
    script.write_bytes(b"#!/bin/sh\necho talos\n")
    schematic = tmp_path / "schematic.yaml"
    schematic.write_bytes(b"customization: {}\n")

    remote_dir = "/mnt/tank/system/apps-config/talos-updater"
    # Construct the existing cronjob with the EXACT fields ensure_cronjob
    # compares against — otherwise it'll think the job has drifted and try
    # to call cronjob.update.
    expected_cmd = _talos_updater_cronjob_command(f"{remote_dir}/{script.name}")
    expected_schedule = {"minute": "0", "hour": "3", "dom": "*", "month": "*", "dow": "*"}
    existing_cronjob = {
        "id": 42, "description": "talos-updater", "enabled": True,
        "command": expected_cmd, "user": "root", "schedule": expected_schedule,
    }

    def _cli_sequence():
        # filesystem.stat for script → matches size
        yield {"size": script.stat().st_size, "mode": 0o100755}
        # filesystem.stat for schematic → matches size
        yield {"size": schematic.stat().st_size, "mode": 0o100644}
        # cronjob.query → existing, no drift
        yield [existing_cronjob]

    seq = _cli_sequence()
    cli = MagicMock()
    cli.call.side_effect = lambda *a, **k: next(seq)

    uploads: list = []
    def fake_upload(**kw):
        uploads.append(kw)

    diffs = ensure_talos_updater(
        cli, fake_upload,
        script_path=script,
        schematic_path=schematic,
        remote_dir=remote_dir,
        apply=True,
    )

    assert uploads == []
    assert all(not d.changed for d in diffs)
    # Should NOT have called cronjob.create or cronjob.update.
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "cronjob.create" not in names
    assert "cronjob.update" not in names
