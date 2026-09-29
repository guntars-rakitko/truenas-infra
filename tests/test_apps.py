"""Tests for modules/apps.py — phase 9 (Custom App deployment from compose)."""

from __future__ import annotations

import os
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


def _pool_cli(status_fn, *, pool=None):
    """cli double whose docker.status behaviour is supplied by `status_fn`.

    A callable, not a list: the wait loop polls an unbounded number of times,
    and a short list would raise StopIteration INSIDE the try — which the loop
    treats as a transient error, silently turning a status test into an error
    test.
    """
    live = {"id": 1, "pool": pool, "dataset": None}
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
    assert "of setting pool 'tank'" in msg, "must say WE set the pool"
    assert calls.count("docker.status") == 30, "60s at sleep(2) = 30 polls"


def test_ensure_docker_pool_noop_path_raises_when_daemon_down(fake_clock) -> None:
    """A correct pool with a dead daemon must NOT report a green noop.

    `docker.update` persists the pool BEFORE the wait, so a run that set the
    pool and then timed out leaves the config correct and the daemon dead —
    and every later run lands on the noop path. While that path returned an
    unconditional `Diff.noop`, the operator's instinctive re-run reported
    success while app.create kept failing: a second false-clean sitting right
    behind the timeout one.
    """
    from truenas_infra.modules.apps import ensure_docker_pool

    cli, calls = _pool_cli(lambda: {"status": "STOPPED"}, pool="tank")

    with pytest.raises(RuntimeError) as exc:
        ensure_docker_pool(cli, pool_name="tank", apply=True, wait_s=60)

    msg = str(exc.value)
    assert "already configured" in msg and "nothing here disturbed it" in msg, (
        "must NOT claim we set the pool — on this path we changed nothing, and "
        "blaming our own write would send the operator down the wrong path"
    )
    assert "'STOPPED'" in msg
    assert "docker.update" not in calls, "the noop path must never write"


def test_ensure_docker_pool_noop_path_skips_check_on_dry_run() -> None:
    """Dry-run must stay read-only and fast — no daemon wait, no failure.

    ⚠ Deliberate asymmetry, not an oversight. `ensure_custom_app` never calls
    app.create without `apply`, so the daemon is not a precondition for
    anything a dry-run does. Making an inspection command block for wait_s and
    then hard-fail on state it was only asked to report would be a worse bug
    than the one being fixed. If this ever starts waiting, the suite hangs for
    60s per dry-run test — which is exactly how this was noticed.
    """
    from truenas_infra.modules.apps import ensure_docker_pool

    cli, calls = _pool_cli(lambda: {"status": "STOPPED"}, pool="tank")

    diff = ensure_docker_pool(cli, pool_name="tank", apply=False, wait_s=60)

    assert diff.changed is False
    assert calls == ["docker.config"], "dry-run must not poll the daemon"


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
    # ⚠ The status response is required even though NOTHING changes. A correct
    # pool does not imply a live daemon, so the noop path verifies too.
    cli = _mk_cli([live, {"status": "RUNNING"}])
    diff = ensure_docker_pool(cli, pool_name="tank", apply=True)
    assert diff.changed is False
    assert [c.args[0] for c in cli.call.call_args_list] == [
        "docker.config", "docker.status",
    ], "noop must still confirm the daemon — a green re-run has to mean something"


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
        {"status": "RUNNING"},                                  # docker.status
        [], {"id": "wiki"},                                     # app.query + app.create
        [], {"id": "traefik"},                                  # app.query + app.create
    ])

    rc = run(cli, _Ctx(apply=True), only=None,
             config_path=cfg_path, pool_name="tank")

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert names == [
        "docker.config", "docker.status",
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
        {"status": "RUNNING"},
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
        {"status": "RUNNING"},
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


# ─── cluster-agent: requirements.lock.txt ships with the code ────────────────
#
# Since 2026-09-29 the container builds /venv from EXACTLY
# apps/cluster-agent/requirements.lock.txt (a hashed `uv export` of uv.lock)
# and rebuilds it when the file's sha256 changes. So the file must reach the
# pool, and a lock bump must reach it too — which size-only idempotency does
# not guarantee: `starlette==1.7.0` -> `==1.8.0` with new fixed-width hashes
# is the same byte length.


# Isolated from the operator's git config (hooks, signing) — same idiom as
# tests/test_verify.py. Only the fixture's own `git init`/`git add` use it; the
# code under test runs plain `git ls-files`, which reads no user config it needs.
_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}

# Tracked source files of the fake tree (path under cluster-agent/ -> bytes).
_CA_TRACKED_CODE = {
    "src/cluster_agent/__init__.py": b"",
    "src/cluster_agent/llm.py": b"MODEL = 'sonnet-4-5'\n",
    "prompts/digest.md": b"Summarise the last 24h.\n",
    "prompts/_shared/house_style.md": b"Be terse.\n",
}


@pytest.fixture
def cluster_agent_tree(tmp_path: Path, monkeypatch):
    """A fake apps/cluster-agent/ in a real git work tree — main.py,
    requirements, tracked src/ + prompts/ files, and an IGNORED
    `__pycache__/*.pyc` as pytest leaves on the laptop — with the module's
    paths pointed at it and upload/read transport recorded instead of sent.

    The NAS starts holding every file byte-for-byte except requirements, which
    is an older lock of the SAME size.

    `uv export` is mocked at subprocess.run: by default it returns the
    committed file's bytes (export in sync); a test sets t["export"] to
    simulate a stale file, or t["export_rc"] to simulate uv failing. Every
    other command (the `git ls-files` under test) runs for real."""
    import subprocess

    import truenas_infra.client as client
    import truenas_infra.modules.apps as m

    app = tmp_path / "cluster-agent"
    app.mkdir()
    (app / "main.py").write_text("app = None\n")
    (app / "requirements.lock.txt").write_text("starlette==1.8.0 \\\n    --hash=sha256:bb\n")
    (app / ".gitignore").write_text("__pycache__/\n*.py[cod]\n")
    for rel, body in _CA_TRACKED_CODE.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_bytes(body)
    pycache = app / "src" / "cluster_agent" / "__pycache__"
    pycache.mkdir()
    (pycache / "llm.cpython-313.pyc").write_bytes(b"\xf3\r\r\n laptop bytecode")
    for args in (("init", "-q"), ("add", "-A")):
        subprocess.run(["git", "-C", str(tmp_path), *args],
                       check=True, capture_output=True, env=_GIT_ENV)
    # Git refuses to see a repo above GIT_CEILING_DIRECTORIES; keeps a test
    # that deletes .git from finding some unrelated repo further up.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))

    monkeypatch.setattr(m, "CLUSTER_AGENT_LOCAL_DIR", app)
    monkeypatch.setattr(m, "CLUSTER_AGENT_SRC_LOCAL_DIR", app / "src")
    monkeypatch.setattr(m, "CLUSTER_AGENT_PROMPTS_LOCAL_DIR", app / "prompts")
    monkeypatch.setattr(m, "CLUSTER_AGENT_MAIN_LOCAL_FILE", app / "main.py")
    monkeypatch.setattr(m, "CLUSTER_AGENT_REQUIREMENTS_LOCAL_FILE", app / "requirements.lock.txt")

    # What the NAS holds: every file identical, except an older requirements
    # of the same SIZE.
    remote = f"{m.CLUSTER_AGENT_CODE_REMOTE_DIR}"
    nas = {
        f"{remote}/main.py": (app / "main.py").read_bytes(),
        f"{remote}/requirements.lock.txt": b"starlette==1.7.0 \\\n    --hash=sha256:aa\n",
        **{f"{remote}/{rel}": body for rel, body in _CA_TRACKED_CODE.items()},
    }
    assert len(nas[f"{remote}/requirements.lock.txt"]) == \
        (app / "requirements.lock.txt").stat().st_size, "fixture must be equal-length"

    uploaded: list[str] = []
    reads: list[str] = []
    stats: list[str] = []
    monkeypatch.setattr(client, "upload_file",
                        lambda cli, **kw: uploaded.append(kw["remote_path"]))

    def _read(cli, *, host, remote_path, verify_ssl=False, timeout=30.0):
        reads.append(remote_path)
        return nas[remote_path]
    monkeypatch.setattr(client, "read_remote_file", _read)

    def _stat(method, path, *a):
        stats.append(path)
        return {"size": len(nas[path]), "mode": 0o644}   # KeyError = missing
    cli = MagicMock()
    cli.call.side_effect = _stat

    t = {"app": app, "cli": cli, "uploaded": uploaded, "reads": reads, "stats": stats,
         "remote": remote, "nas": nas, "uv_calls": [],
         "export": (app / "requirements.lock.txt").read_bytes(), "export_rc": 0}

    real_run = subprocess.run

    def _run(cmd, **kw):
        if cmd[0] != "/fake/bin/uv":
            return real_run(cmd, **kw)
        t["uv_calls"].append((list(cmd), kw.get("cwd")))
        return subprocess.CompletedProcess(cmd, t["export_rc"], stdout=t["export"],
                                           stderr=b"error: uv.lock is not a valid lockfile")
    monkeypatch.setenv("UV", "/fake/bin/uv")
    monkeypatch.setattr(m.subprocess, "run", _run)
    return t


def test_cluster_agent_upload_ships_equal_size_requirements_change(cluster_agent_tree) -> None:
    """A lock bump of the same byte length MUST re-upload requirements.lock.txt.

    Fails against a size-only upload (no read_fn): it reports `noop`, the NAS
    keeps the old file, and /venv never rebuilds — the 2026-09-13 PXE
    Dockerfile false clean, on the file that decides every runtime version."""
    import structlog

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=True), structlog.get_logger("test"))

    assert f"{t['remote']}/requirements.lock.txt" in t["reads"], "requirements must be content-checked"
    assert t["uploaded"] == [f"{t['remote']}/requirements.lock.txt"], \
        "equal-size requirements change must upload (and nothing else changed)"


# ─── cluster-agent: EVERY code file content-verified; git-tracked files only ──
#
# Until 2026-09-29 only requirements.lock.txt got `read_fn`. main.py, src/**
# and prompts/** were SIZE-ONLY — an equal-length edit reported `noop` and never
# reached the NAS — and src/ was walked with rglob("*"), so the laptop's pytest
# `__pycache__/*.pyc` shipped on every deploy (37 of them sit on the NAS).


@pytest.mark.parametrize("rel", ["main.py", "src/cluster_agent/llm.py", "prompts/digest.md"])
def test_cluster_agent_upload_ships_equal_size_code_change(cluster_agent_tree, rel) -> None:
    """An equal-length edit to ANY code file must upload, not just to the lock.

    Fails against the size-only upload: the NAS copy below is the same length
    (`sonnet-4-5` -> `sonnet-4-6` is the real-world shape), so it reported
    `noop` and the container kept running the old code."""
    import structlog

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    req = f"{t['remote']}/requirements.lock.txt"
    t["nas"][req] = (t["app"] / "requirements.lock.txt").read_bytes()   # isolate `rel`
    path = f"{t['remote']}/{rel}"
    t["nas"][path] = (t["app"] / rel).read_bytes().swapcase()   # older, same length
    assert t["nas"][path] != (t["app"] / rel).read_bytes()

    _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=True), structlog.get_logger("test"))

    assert path in t["reads"], f"{rel} must be content-checked"
    assert t["uploaded"] == [path]


def test_cluster_agent_upload_content_verifies_every_file(cluster_agent_tree) -> None:
    """A clean run is a CONTENT-verified noop for every file, and each log line
    says so (`content_verified=True`) rather than implying it via changed=False."""
    import structlog
    import structlog.testing

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    t["nas"][f"{t['remote']}/requirements.lock.txt"] = \
        (t["app"] / "requirements.lock.txt").read_bytes()

    with structlog.testing.capture_logs() as logs:
        _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=False), structlog.get_logger("test"))

    expected = {f"{t['remote']}/{rel}"
                for rel in ("main.py", "requirements.lock.txt", *_CA_TRACKED_CODE)}
    ensured = {e["path"]: e for e in logs if e["event"] == "cluster_agent_file_ensured"}
    assert set(ensured) == expected
    assert {(e["action"], e["content_verified"]) for e in ensured.values()} == {("noop", True)}
    assert sorted(t["reads"]) == sorted(expected)
    assert t["uploaded"] == []


def test_cluster_agent_upload_ships_only_git_tracked_files(cluster_agent_tree) -> None:
    """The laptop's ignored `__pycache__/*.pyc` and an untracked scratch module
    are never stat'd, read or uploaded; the untracked one is LOGGED, so a
    forgotten `git add` is visible rather than a silent ImportError later.

    Fails against the old rglob walk: it stat'd the .pyc, found it missing on
    the NAS, and uploaded it — every deploy, for every cached module."""
    import structlog
    import structlog.testing

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    (t["app"] / "src" / "cluster_agent" / "scratch.py").write_text("wip = True\n")

    with structlog.testing.capture_logs() as logs:
        _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=True), structlog.get_logger("test"))

    touched = t["stats"] + t["reads"] + t["uploaded"]
    assert not [p for p in touched if "__pycache__" in p or p.endswith(".pyc")], touched
    assert not [p for p in touched if p.endswith("scratch.py")], touched
    assert t["uploaded"] == [f"{t['remote']}/requirements.lock.txt"]   # the one real change
    warned = [e for e in logs if e["event"] == "cluster_agent_untracked_not_uploaded"]
    assert [(e["log_level"], e["paths"]) for e in warned] == \
        [("warning", ["src/cluster_agent/scratch.py"])]


def test_cluster_agent_upload_refuses_outside_a_git_work_tree(cluster_agent_tree) -> None:
    """No work tree = no way to tell source from junk. Fail BEFORE the first
    upload (the file list is built up front) — never fall back to the walk."""
    import shutil

    import structlog

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    shutil.rmtree(t["app"].parent / ".git")
    t["nas"][f"{t['remote']}/main.py"] = b"old = 1\n"   # different size -> would upload

    with pytest.raises(RuntimeError, match=r"(?s)git ls-files.*failed in.*Nothing was uploaded"):
        _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=True), structlog.get_logger("test"))
    assert t["uploaded"] == []


# The other single-file uploads were size-only too. A route or listen edit is
# routinely equal-length (`10.10.5.21` -> `10.10.5.22`, `:8080` -> `:8081`).
_CONFIG_UPLOADS = {
    "_ensure_wiki_config_via_ctx": {
        "WIKI_NGINX_CONF_PATH": "/mnt/tank/system/apps-config/wiki/nginx.conf"},
    "_ensure_traefik_routes_via_ctx": {
        "TRAEFIK_ROUTES_PATH": "/mnt/tank/system/apps-config/traefik/routes.yaml"},
    "_ensure_tls_rotate_via_ctx": {
        "TLS_EXPORT_SCRIPT_PATH": "/mnt/tank/system/tls/tls-export.sh",
        "TLS_ROTATE_SCRIPT_PATH": "/mnt/tank/system/tls/tls-rotate.sh"},
}


@pytest.mark.parametrize("helper", sorted(_CONFIG_UPLOADS))
def test_config_upload_ships_equal_size_change(tmp_path: Path, monkeypatch, helper) -> None:
    """wiki nginx.conf, traefik routes.yaml and the tls scripts: an older NAS
    copy of the SAME length must re-upload. Fails against size-only (no read_fn)."""
    import structlog

    import truenas_infra.client as client
    import truenas_infra.modules.apps as m

    nas: dict[str, bytes] = {}
    for const, remote in _CONFIG_UPLOADS[helper].items():
        local = tmp_path / Path(remote).name
        local.write_bytes(b"server 10.10.5.22:8080;\n")
        nas[remote] = b"server 10.10.5.21:8080;\n"
        monkeypatch.setattr(m, const, local)

    uploaded: list[str] = []
    monkeypatch.setattr(client, "upload_file",
                        lambda cli, **kw: uploaded.append(kw["remote_path"]))
    monkeypatch.setattr(client, "read_remote_file",
                        lambda cli, *, remote_path, **kw: nas[remote_path])

    def _call(method, *args):
        if method == "filesystem.stat":
            return {"size": len(nas[args[0]]), "mode": 0o644}
        return [] if method == "cronjob.query" else {"id": 1}   # tls: cronjob create
    cli = MagicMock()
    cli.call.side_effect = _call

    getattr(m, helper)(cli, _Ctx(apply=True), structlog.get_logger("test"))

    assert sorted(uploaded) == sorted(_CONFIG_UPLOADS[helper].values())


def test_cluster_agent_upload_refuses_without_requirements(cluster_agent_tree) -> None:
    """No requirements.lock.txt ⇒ fail the phase BEFORE ensure_custom_app can
    roll out a compose whose startup script exits without it."""
    import structlog

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    (t["app"] / "requirements.lock.txt").unlink()
    with pytest.raises(RuntimeError, match="requirements.lock.txt is missing"):
        _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=False), structlog.get_logger("test"))
    assert t["uploaded"] == []


def test_cluster_agent_requirements_file_is_committed(repo_root: Path) -> None:
    """The real file exists where the upload helper looks for it."""
    from truenas_infra.modules.apps import CLUSTER_AGENT_REQUIREMENTS_LOCAL_FILE

    assert (repo_root / CLUSTER_AGENT_REQUIREMENTS_LOCAL_FILE).is_file()


# ─── cluster-agent: requirements.lock.txt must BE `uv export` of uv.lock ─────
#
# Renovate lock PRs bump uv.lock but cannot re-export, and truenas-infra has no
# CI, so a lock bump merged without the re-export would deploy the OLD pins
# while uv.lock (and the Dependabot alerts it closes) claim the new ones. The
# deploy step refuses that — before the first upload, dry-run included.


def test_cluster_agent_upload_runs_the_committed_uv_export(cluster_agent_tree) -> None:
    """The guard runs `uv` with EXACTLY the export flags, in apps/cluster-agent/."""
    import structlog

    import truenas_infra.modules.apps as m

    t = cluster_agent_tree
    m._ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=False), structlog.get_logger("test"))

    assert t["uv_calls"] == [
        (["/fake/bin/uv", "export", "--frozen", "--no-dev", "--no-emit-project",
          "--format", "requirements-txt"], t["app"]),
    ]


def test_cluster_agent_upload_refuses_stale_export(cluster_agent_tree) -> None:
    """A committed file that differs from `uv export` fails the phase, names the
    drifted pins, and uploads NOTHING — not even a main.py that did change."""
    import structlog

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    t["nas"][f"{t['remote']}/main.py"] = b"old = 1\n"   # different size -> would upload
    t["export"] = b"starlette==1.9.0 \\\n    --hash=sha256:cc\n"   # what the lock now says

    with pytest.raises(RuntimeError, match="is STALE") as exc:
        _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=True), structlog.get_logger("test"))
    assert "starlette==1.8.0" in str(exc.value) and "starlette==1.9.0" in str(exc.value)
    assert "uv export --frozen --no-dev --no-emit-project --format requirements-txt" in str(exc.value)
    assert t["uploaded"] == [], "a stale export must stop the phase before ANY upload"


def test_cluster_agent_upload_refuses_hash_only_drift(cluster_agent_tree) -> None:
    """Same pins, different hashes (a re-published wheel set) is still stale:
    `--require-hashes` on the NAS would install against the committed hashes."""
    import structlog

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    t["export"] = b"starlette==1.8.0 \\\n    --hash=sha256:cc\n"

    with pytest.raises(RuntimeError, match="same pins, different hashes or header"):
        _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=True), structlog.get_logger("test"))
    assert t["uploaded"] == []


def test_cluster_agent_upload_refuses_when_uv_export_fails(cluster_agent_tree) -> None:
    """uv exiting non-zero is a failure with uv's stderr, never a pass."""
    import structlog

    from truenas_infra.modules.apps import _ensure_cluster_agent_config_via_ctx

    t = cluster_agent_tree
    t["export_rc"] = 2
    with pytest.raises(RuntimeError, match=r"failed in .*exit 2.*\n.*not a valid lockfile"):
        _ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=True), structlog.get_logger("test"))
    assert t["uploaded"] == []


def test_cluster_agent_upload_refuses_without_uv(cluster_agent_tree, monkeypatch) -> None:
    """No uv on the operator's PATH is an error that says how to fix it — a
    skipped drift check would read as a passing one."""
    import structlog

    import truenas_infra.modules.apps as m

    t = cluster_agent_tree
    monkeypatch.delenv("UV", raising=False)
    monkeypatch.setattr(m.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match=r"uv not found.*~/\.local/bin"):
        m._ensure_cluster_agent_config_via_ctx(t["cli"], _Ctx(apply=True), structlog.get_logger("test"))
    assert t["uv_calls"] == [] and t["uploaded"] == []


def test_cluster_agent_export_args_match_the_committed_file(repo_root: Path) -> None:
    """uv writes its command into the export header, and apps/cluster-agent's
    test_requirements_lock.py proves the file byte-equals that export — so this
    ties the deploy guard's flags to the ones that actually produced the file."""
    from truenas_infra.modules.apps import (
        CLUSTER_AGENT_REQUIREMENTS_LOCAL_FILE,
        CLUSTER_AGENT_UV_EXPORT_ARGS,
    )

    header = (repo_root / CLUSTER_AGENT_REQUIREMENTS_LOCAL_FILE).read_text().splitlines()[:3]
    assert f"#    uv {' '.join(CLUSTER_AGENT_UV_EXPORT_ARGS)}" in header, header


def test_cluster_agent_export_guard_passes_on_the_real_tree(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unmocked: real uv, real apps/cluster-agent/ — the committed file IS the
    export, so the guard must pass (catches a wrong cwd or flag in the guard
    that the mocked tests cannot see). Needs uv: run via `uv run --extra dev pytest`."""
    import structlog

    from truenas_infra.modules.apps import _verify_cluster_agent_requirements_export

    monkeypatch.chdir(repo_root)   # CLUSTER_AGENT_LOCAL_DIR is repo-relative, as under manage.sh
    _verify_cluster_agent_requirements_export(structlog.get_logger("test"))
