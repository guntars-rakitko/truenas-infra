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
    # ⚠ The third response is the daemon poll, and it is NOT optional. On
    # apply, ensure_docker_pool polls `docker.status` until RUNNING, and its
    # loop swallows every exception — including the StopIteration a short
    # side-effect list raises. Without a RUNNING response the loop spun the
    # full wait_s at time.sleep(2): this one test cost 60s of every suite run
    # and asserted nothing about the wait. Feeding it exercises the real path.
    cli = _mk_cli([live, {**live, "pool": "tank"}, {"status": "RUNNING"}])

    diff = ensure_docker_pool(cli, pool_name="tank", apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "docker.update")
    assert update.args[1]["pool"] == "tank"
    assert [c.args[0] for c in cli.call.call_args_list] == [
        "docker.config", "docker.update", "docker.status",
    ], "must poll the daemon before returning — app.create fails without it"


@pytest.fixture
def fake_clock(monkeypatch):
    """Drive ensure_docker_pool's wait loop off a fake clock.

    `time.sleep` advances the clock instead of blocking, so a 60s wait costs
    nothing and the poll COUNT is exact rather than wall-clock dependent.
    ⚠ Do not swap this for a tiny real `wait_s`: with sleep stubbed to a no-op
    the loop spins as fast as the CPU allows, so the number of polls — and
    therefore how many mock responses it eats — varies per machine.
    """
    import truenas_infra.modules.apps as m
    t = {"now": 0.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"])
    monkeypatch.setattr(m.time, "sleep", lambda s: t.__setitem__("now", t["now"] + s))
    return t


def _pool_cli(status_fn):
    """cli double whose docker.status behaviour is supplied by `status_fn`.

    A callable, not a list: the wait loop polls an unbounded number of times,
    and a short list would raise StopIteration INSIDE the try — which the loop
    treats as a transient error, silently turning a status test into an error
    test.
    """
    live = {"id": 1, "pool": None, "dataset": None}
    calls: list[str] = []

    def _respond(method, *args, **kwargs):
        calls.append(method)
        if method == "docker.config":
            return live
        if method == "docker.update":
            return {**live, "pool": "tank"}
        if method == "docker.status":
            return status_fn()
        raise AssertionError(f"unexpected call {method!r}")

    cli = MagicMock()
    cli.call.side_effect = _respond
    return cli, calls


def test_ensure_docker_pool_raises_when_daemon_never_starts(fake_clock) -> None:
    """A wait that gives up must FAIL, not return a success Diff.

    The regression: this loop had no `else`, so a daemon that never came up
    fell through to `Diff.update(...)` — run() then called app.create, which
    failed with 'No pool configured for Docker', the exact error the wait
    exists to prevent. The operator saw a green `docker_pool_ensured` followed
    by an unexplained app-create failure.
    """
    from truenas_infra.modules.apps import ensure_docker_pool

    cli, calls = _pool_cli(lambda: {"status": "PENDING"})

    with pytest.raises(RuntimeError) as exc:
        ensure_docker_pool(cli, pool_name="tank", apply=True, wait_s=60)

    msg = str(exc.value)
    assert "did not reach RUNNING within 60" in msg
    assert "'PENDING'" in msg, "must name the status it actually saw"
    assert "re-run" in msg, (
        "must warn that the pool is already persisted, so a re-run takes the "
        "noop path and never waits again"
    )
    assert calls.count("docker.status") == 30, "60s at sleep(2) = 30 polls"


def test_ensure_docker_pool_timeout_surfaces_last_error(fake_clock) -> None:
    """Errors during the wait are swallowed, but the LAST one must survive.

    A persistently failing docker.status and a merely slow daemon are
    indistinguishable once the exception is discarded — which is what
    `except Exception: pass` did. Only the timeout message can tell them apart.
    """
    from truenas_infra.modules.apps import ensure_docker_pool

    def _boom():
        raise ConnectionRefusedError("daemon socket not up")

    cli, _ = _pool_cli(_boom)

    with pytest.raises(RuntimeError) as exc:
        ensure_docker_pool(cli, pool_name="tank", apply=True, wait_s=10)

    msg = str(exc.value)
    assert "never returned a status" in msg
    assert "daemon socket not up" in msg, "the last error must reach the operator"


def test_ensure_docker_pool_tolerates_transient_errors_then_succeeds(fake_clock) -> None:
    """Swallowing errors DURING the wait is correct — the daemon is restarting.

    Proves the fix did not make a normal restart fatal: two refused connections
    followed by RUNNING is a success, and no stale error leaks into anything.
    """
    from truenas_infra.modules.apps import ensure_docker_pool

    seq = iter([ConnectionRefusedError("no socket"),
                ConnectionRefusedError("no socket"),
                {"status": "RUNNING"}])

    def _flaky():
        v = next(seq)
        if isinstance(v, Exception):
            raise v
        return v

    cli, calls = _pool_cli(_flaky)

    diff = ensure_docker_pool(cli, pool_name="tank", apply=True, wait_s=60)

    assert diff.changed is True
    assert calls.count("docker.status") == 3


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
              - name: pxe
                enabled: true
                compose: apps/pxe/docker-compose.yaml
                bind_ip: 10.10.5.10
              - name: minio-prd
                enabled: true
                compose: apps/minio-prd/docker-compose.yaml
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
    assert "pxe" in names
    assert "minio-prd" in names
    assert "plex" not in names


# ─── ensure_custom_app ───────────────────────────────────────────────────────


def test_ensure_custom_app_creates_when_missing(tmp_path: Path) -> None:
    from truenas_infra.modules.apps import AppSpec, ensure_custom_app

    compose_path = tmp_path / "docker-compose.yaml"
    compose_path.write_text("services:\n  foo:\n    image: hello-world\n")

    cli = _mk_cli([
        [],                                       # app.query
        {"id": "pxe", "state": "RUNNING"},        # app.create (job=True → result)
    ])

    spec = AppSpec(name="pxe", compose_path=compose_path)
    diff = ensure_custom_app(cli, spec=spec, apply=True)

    assert diff.changed is True
    create = next(c for c in cli.call.call_args_list if c.args[0] == "app.create")
    payload = create.args[1]
    assert payload["app_name"] == "pxe"
    assert payload["custom_app"] is True
    assert "services:" in payload["custom_compose_config_string"]
    # Should be invoked with job=True so the client blocks until redeploy
    # finishes and surfaces FAILED as an exception.
    assert create.kwargs.get("job") is True


def test_ensure_custom_app_noop_when_compose_matches(tmp_path: Path) -> None:
    """Drift-check compares the local compose (parsed) against `app.config`
    (TrueNAS returns the stored compose as a dict). Deep-equal → noop, no
    `app.update` call, no redeploy.
    """
    from truenas_infra.modules.apps import AppSpec, ensure_custom_app

    compose_path = tmp_path / "docker-compose.yaml"
    compose_path.write_text("services:\n  foo:\n    image: hello-world\n")

    existing = {"id": "pxe", "name": "pxe", "state": "RUNNING", "custom_app": True}
    stored_compose = {"services": {"foo": {"image": "hello-world"}}}
    cli = _mk_cli([[existing], stored_compose])

    spec = AppSpec(name="pxe", compose_path=compose_path)
    diff = ensure_custom_app(cli, spec=spec, apply=True)

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "app.create" not in names
    assert "app.update" not in names


def test_ensure_custom_app_updates_when_compose_drifts(tmp_path: Path) -> None:
    """Real-world trigger: we flip `network_mode: bridge` → `host` in the
    committed compose. Drift-check sees the mismatch → app.update pushes
    the new YAML and TrueNAS redeploys the container in the same job.
    """
    from truenas_infra.modules.apps import AppSpec, ensure_custom_app

    compose_path = tmp_path / "docker-compose.yaml"
    compose_path.write_text(
        "services:\n  pxe:\n"
        "    image: homelab-pxe:latest\n"
        "    network_mode: host\n"
    )

    existing = {"id": "pxe", "name": "pxe", "state": "RUNNING", "custom_app": True}
    # Stored state still has the old bridge mode — the very drift we want
    # ensure_custom_app to reconcile.
    stored_compose = {
        "services": {
            "pxe": {
                "image": "homelab-pxe:latest",
                "network_mode": "bridge",
            }
        }
    }
    cli = _mk_cli([[existing], stored_compose, None])  # None = app.update return

    spec = AppSpec(name="pxe", compose_path=compose_path)
    diff = ensure_custom_app(cli, spec=spec, apply=True)

    assert diff.changed is True
    update = next(c for c in cli.call.call_args_list if c.args[0] == "app.update")
    # app.update(name, {custom_compose_config_string: ...}, job=True)
    assert update.args[1] == "pxe"
    assert "network_mode: host" in update.args[2]["custom_compose_config_string"]
    assert update.kwargs.get("job") is True
    # No create call (app already exists).
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "app.create" not in names


def test_ensure_custom_app_dryrun_reports_drift_without_calling_update(
    tmp_path: Path,
) -> None:
    """`apply=False` must never mutate: detect drift, return a Diff.update
    for the caller to display, but no `app.update` call."""
    from truenas_infra.modules.apps import AppSpec, ensure_custom_app

    compose_path = tmp_path / "docker-compose.yaml"
    compose_path.write_text(
        "services:\n  pxe:\n    image: homelab-pxe:latest\n    network_mode: host\n"
    )

    existing = {"id": "pxe", "name": "pxe", "state": "RUNNING", "custom_app": True}
    stored_compose = {
        "services": {"pxe": {"image": "homelab-pxe:latest", "network_mode": "bridge"}}
    }
    cli = _mk_cli([[existing], stored_compose])

    spec = AppSpec(name="pxe", compose_path=compose_path)
    diff = ensure_custom_app(cli, spec=spec, apply=False)

    assert diff.changed is True
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "app.update" not in names
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


def test_render_compose_substitutes_vars_from_doppler(tmp_path: Path, monkeypatch) -> None:
    """${VAR} references in compose are replaced by values fetched from Doppler."""
    from truenas_infra.modules import apps as apps_module

    compose = tmp_path / "docker-compose.yaml"
    compose.write_text(
        "services:\n  minio:\n    environment:\n"
        "      - MINIO_ROOT_USER=${MINIO_ROOT_USER}\n"
        "      - MINIO_ROOT_PASSWORD=${MINIO_ROOT_PASSWORD}\n"
    )

    # Monkey-patch the Doppler loader to avoid actually calling Doppler in tests.
    monkeypatch.setattr(
        apps_module, "_load_doppler_for_app",
        lambda _name: {"MINIO_ROOT_USER": "admin", "MINIO_ROOT_PASSWORD": "s3cret"},
    )

    rendered = apps_module._render_compose(compose, app_name="minio-prd")

    assert "MINIO_ROOT_USER=admin" in rendered
    assert "MINIO_ROOT_PASSWORD=s3cret" in rendered
    # No un-substituted placeholders left.
    assert "${" not in rendered


def test_render_compose_passes_through_when_no_secrets(tmp_path: Path) -> None:
    from truenas_infra.modules import apps as apps_module

    compose = tmp_path / "docker-compose.yaml"
    compose.write_text("services:\n  foo:\n    image: hello-world\n")

    # No app_name → no Doppler fetch attempted → compose returned verbatim.
    rendered = apps_module._render_compose(compose)
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


def _apps_yaml(tmp_path: Path, *names: str) -> Path:
    """Write an apps.yaml with `names` enabled, each with a trivial compose."""
    entries = []
    for n in names:
        d = tmp_path / "apps" / n
        d.mkdir(parents=True, exist_ok=True)
        compose = d / "docker-compose.yaml"
        compose.write_text(f"services:\n  {n}:\n    image: alpine:3.22\n")
        entries.append(
            f"  - name: {n}\n"
            f"    enabled: true\n"
            f"    compose: {compose}\n"
            f"    bind_ip: 10.10.5.10\n"
        )
    cfg = tmp_path / "apps.yaml"
    cfg.write_text("apps:\n" + "".join(entries))
    return cfg


@pytest.fixture
def shipped(monkeypatch):
    """Record which file-shipping helpers run(), instead of executing them.

    ⚠ These helpers build an `upload_file(...)` closure from
    `ctx.config.truenas_host`, so leaving them real makes the test POST to
    whatever that resolves to. The previous version of this test set the host
    to the LIVE NAS (10.10.5.10) and relied on every file size-matching a
    hand-maintained table to avoid an upload — a fixture that had to be
    updated whenever any shipped file changed size, and which the 2026-09-13
    content-verification change defeated anyway (a size match now triggers a
    read). Patching is not a shortcut here; it is what makes the test about
    run()'s DISPATCH rather than about file sizes on disk.
    """
    seen: list[str] = []
    for name in (
        "_ensure_wiki_config_via_ctx",
        "_ensure_cluster_agent_config_via_ctx",
        "_ensure_tls_rotate_via_ctx",
        "_ensure_traefik_routes_via_ctx",
    ):
        monkeypatch.setattr(
            "truenas_infra.modules.apps." + name,
            (lambda n: lambda cli, ctx, log: seen.append(n))(name),
        )
    return seen


def test_run_configures_docker_pool_and_apps(tmp_path: Path, shipped) -> None:
    """Happy path: pool configured, then every enabled app created."""
    from truenas_infra.modules.apps import run

    cfg_path = _apps_yaml(tmp_path, "wiki", "traefik")
    cli = _mk_cli([
        {"id": 1, "pool": "tank", "dataset": "tank/.ix-apps"},  # docker.config
        [], {"id": "wiki"},                                     # app.query + app.create
        [], {"id": "traefik"},                                  # app.query + app.create
    ])

    rc = run(cli, _Ctx(apply=True), only=None,
             config_path=cfg_path, pool_name="tank")

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert names == [
        "docker.config",
        "app.query", "app.create",
        "app.query", "app.create",
    ]


def test_run_skips_config_upload_for_app_absent_from_apps_yaml(
    tmp_path: Path, shipped
) -> None:
    """THE 2026-09-23 regression: config uploads must follow apps.yaml.

    The dispatch used to gate on `only` alone. `cfg` holds only ENABLED apps,
    but nothing consulted it — so a retired app kept having its config
    re-uploaded forever. Measured, not hypothetical: the first
    `phase apps --apply` after the pool rebuild recreated four retired apps'
    config directories on a pool rebuilt specifically to be rid of them.

    Here `wiki` is enabled and `cluster-agent` is not, so exactly one of the
    two config helpers may run.
    """
    from truenas_infra.modules.apps import run

    cfg_path = _apps_yaml(tmp_path, "wiki")
    cli = _mk_cli([
        {"id": 1, "pool": "tank", "dataset": "tank/.ix-apps"},
        [], {"id": "wiki"},
    ])

    run(cli, _Ctx(apply=True), only=None, config_path=cfg_path, pool_name="tank")

    assert "_ensure_wiki_config_via_ctx" in shipped
    assert "_ensure_cluster_agent_config_via_ctx" not in shipped, \
        "a config helper ran for an app that is not in apps.yaml"


def test_run_ships_tls_rotate_even_though_no_tls_app_exists(
    tmp_path: Path, shipped
) -> None:
    """TLS rotation is infrastructure, NOT an app — it must stay ungated.

    ⚠ There is no "tls" entry in apps.yaml, so folding this into `_want()`
    for consistency would make its predicate ALWAYS false and silently stop
    cert rotation. The failure is invisible until the wildcard expires, and
    re-issuing is rate-limited (5/week per exact identifier set). This test
    exists to fail loudly if someone tidies that block.
    """
    from truenas_infra.modules.apps import run

    cfg_path = _apps_yaml(tmp_path, "wiki")
    cli = _mk_cli([
        {"id": 1, "pool": "tank", "dataset": "tank/.ix-apps"},
        [], {"id": "wiki"},
    ])

    run(cli, _Ctx(apply=True), only=None, config_path=cfg_path, pool_name="tank")

    assert "_ensure_tls_rotate_via_ctx" in shipped


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


# ─── ensure_file_on_nas: CONTENT vs size (regression for the 2026-09-13 bug) ──
#
# The size-only check reported `noop changed=False` for an equal-length
# content edit. Real instance: apps/pxe/build/Dockerfile went
# `FROM alpine:3.20` -> `3.23` in git; identical byte length; the NAS kept
# building on 3.20 for months and every dry-run called it clean.
# These tests fail against the size-only implementation.

def test_ensure_file_on_nas_detects_equal_size_content_change(tmp_path: Path) -> None:
    """Same size, different bytes ⇒ MUST upload (the bug)."""
    from truenas_infra.modules.apps import ensure_file_on_nas

    local = tmp_path / "Dockerfile"
    local.write_text("FROM alpine:3.23\n")
    remote_bytes = b"FROM alpine:3.20\n"
    assert len(remote_bytes) == local.stat().st_size, "fixture must be equal-length"

    class _Cli:
        def call(self, method, *a, **k):
            return {"size": local.stat().st_size, "mode": 0o644}

    uploaded: list[str] = []

    def _upload(*, local_path, remote_path, mode):
        uploaded.append(remote_path)

    diff = ensure_file_on_nas(
        _Cli(), _upload,
        local_path=local, remote_path="/remote/Dockerfile",
        mode=0o644, apply=True,
        read_fn=lambda _p: remote_bytes,
    )
    assert uploaded == ["/remote/Dockerfile"], "equal-size content change must re-upload"
    assert diff.changed is True


def test_ensure_file_on_nas_noop_when_content_identical(tmp_path: Path) -> None:
    """Same size AND same bytes ⇒ still a noop, and marked verified."""
    from truenas_infra.modules.apps import ensure_file_on_nas

    local = tmp_path / "Dockerfile"
    local.write_text("FROM alpine:3.23\n")

    class _Cli:
        def call(self, method, *a, **k):
            return {"size": local.stat().st_size, "mode": 0o644}

    uploaded: list[str] = []

    def _upload(*, local_path, remote_path, mode):
        uploaded.append(remote_path)

    diff = ensure_file_on_nas(
        _Cli(), _upload,
        local_path=local, remote_path="/remote/Dockerfile",
        mode=0o644, apply=True,
        read_fn=lambda _p: local.read_bytes(),
    )
    assert uploaded == []
    assert diff.changed is False
    assert (diff.after or {}).get("content_verified") is True


def test_ensure_file_on_nas_degrades_to_size_only_when_read_fails(tmp_path: Path) -> None:
    """A broken /_download must NOT fail the phase — degrade, but say so."""
    from truenas_infra.modules.apps import ensure_file_on_nas

    local = tmp_path / "Dockerfile"
    local.write_text("FROM alpine:3.23\n")

    class _Cli:
        def call(self, method, *a, **k):
            return {"size": local.stat().st_size, "mode": 0o644}

    def _boom(_p):
        raise RuntimeError("/_download unavailable")

    diff = ensure_file_on_nas(
        _Cli(), lambda **k: None,
        local_path=local, remote_path="/remote/Dockerfile",
        mode=0o644, apply=True, read_fn=_boom,
    )
    assert diff.changed is False
    assert (diff.after or {}).get("content_verified") is False, \
        "unverified must be visible, not silently claimed as a match"


def test_ensure_file_on_nas_skips_content_check_for_large_files(tmp_path: Path) -> None:
    """hw-validation's 192 MB artefacts must never be pulled back."""
    from truenas_infra.modules.apps import CONTENT_VERIFY_MAX_BYTES, ensure_file_on_nas

    local = tmp_path / "modloop-lts"
    local.write_bytes(b"x" * 64)

    class _Cli:
        def call(self, method, *a, **k):
            return {"size": CONTENT_VERIFY_MAX_BYTES + 1, "mode": 0o644}

    reads: list[str] = []

    # stat reports oversize; local_size is what gates the pull, so fake it
    import os
    real_stat = os.stat

    diff = ensure_file_on_nas(
        _Cli(), lambda **k: None,
        local_path=local, remote_path="/remote/modloop-lts",
        mode=0o644, apply=True,
        read_fn=lambda p: (reads.append(p), b"")[1],
    )
    # sizes differ (64 vs cap+1) so it uploads without ever reading
    assert reads == [], "must not pull large files back over HTTP"
