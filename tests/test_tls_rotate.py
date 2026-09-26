"""Control-flow tests for apps/tls/tls-rotate.sh (the hourly `tls-rotate` cronjob).

WHY THIS EXISTS
---------------
The script's whole job is: when tls-export.sh says the cert changed (exit 10),
`midclt call app.redeploy` every app that serves the cert. It had never done
that, and nothing ever ran it with a changing cert:

- Until 2026-09-23 it read `$?` after an `if` block, which is always 0, so a
  rotation was logged as "failed with exit=0" and nothing was redeployed.
- e3d5663 (2026-09-23) moved the call out of the `if`, but made it a bare
  `"$EXPORT_SCRIPT"` under `set -e` -- so exit 10 made the SHELL exit on that
  line, silently, still before any redeploy.
- Traefik was also left out of the consumer list on the belief that it
  hot-reloads the cert. It does not (its file provider watches only
  /etc/traefik/dynamic). NAS Traefik served the pre-renewal cert for nine days
  after the 2026-09-14 renewal, until the 2026-09-23 pool-rebuild restart
  (kube-infra #1252 / #1253, BlackboxCertExpiringWarn on wiki.w1.lv).
- MinIO, which WAS on the list, turned out to reload on its own. See
  RELOADS_ON_ITS_OWN below.

These tests run the real script under every distinct bash on the machine, laid
out as it is on the NAS (tls-rotate.sh next to tls-export.sh), with tls-export.sh
and `midclt` stubbed.

`test_changed_cert_redeploys_every_consumer` is the regression guard: it FAILS
against both earlier versions of the script.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ROTATE = REPO / "apps" / "tls" / "tls-rotate.sh"
APPS_YAML = REPO / "config" / "apps.yaml"
TLS_DIR_ON_NAS = "/mnt/tank/system/tls"
PENDING_NAME = ".tls-redeploy-pending"
PRODUCTION_CONSUMERS = ["traefik"]

# Apps that mount the TLS dir but pick up a renewed cert WITHOUT a redeploy.
# Each entry needs evidence. Without an entry here an app must be in
# TLS_CONSUMERS (test_every_app_that_mounts_the_cert_is_handled).
RELOADS_ON_ITS_OWN = {
    # 2026-09-14: the renewal was exported at 04:00 UTC and tls-rotate.sh
    # redeployed nothing (its `$?` bug). Blackbox on both clusters:
    # s3-{prd,dev}.w1.lv:9000 served the old cert (exp 2026-10-13) at 03:59:38
    # and the new one (exp 2026-12-13) at 04:00:08, with probe_success=1 on
    # every 30 s sample, so there was no restart. Redeploying would only add a
    # ~30 s S3 outage per rotation.
    "minio-prd": "AIStor re-reads --certs-dir on change (measured 2026-09-14)",
    "minio-dev": "AIStor re-reads --certs-dir on change (measured 2026-09-14)",
}

# Several consumers, so the retry tests can fail one and succeed on the rest.
# The stub midclt accepts any name.
MULTI = ["app-one", "app-two", "app-three"]
MULTI_ENV = {"TLS_CONSUMERS": " ".join(MULTI)}

# Env vars the script reads. Stripped so a value in the developer's shell can
# never mask the production defaults these tests pin.
_SCRIPT_ENV = ("EXPORT_SCRIPT", "TLS_CONSUMERS", "PENDING_FILE")


# Run under the NAS's bash (5.x) and macOS's /bin/bash 3.2 alike -- same rule as
# tests/test_ups_orchestrator.py.
def _bashes() -> list[str]:
    found: list[str] = []
    for cand in ("/bin/bash", shutil.which("bash")):
        if (
            cand
            and os.path.exists(cand)
            and os.path.realpath(cand) not in {os.path.realpath(b) for b in found}
        ):
            found.append(cand)
    return found


@pytest.fixture(params=_bashes())
def bash(request: pytest.FixtureRequest) -> str:
    return str(request.param)


class Nas:
    """A fake /mnt/tank/system/tls with the real tls-rotate.sh and stubs."""

    def __init__(self, root: Path, bash: str) -> None:
        self.bash = bash
        self.tls = root / "tls"
        self.bin = root / "bin"
        self.calls_log = root / "midclt.log"
        self.tls.mkdir()
        self.bin.mkdir()
        self.script = self.tls / "tls-rotate.sh"
        shutil.copy(ROTATE, self.script)
        # Default EXPORT_SCRIPT is $SCRIPT_DIR/tls-export.sh -- exercise that
        # default rather than overriding it.
        export = self.tls / "tls-export.sh"
        export.write_text('#!/bin/bash\necho "stub tls-export" >&2\nexit "${STUB_EXPORT_RC:?}"\n')
        export.chmod(0o755)
        midclt = self.bin / "midclt"
        midclt.write_text(
            "#!/bin/bash\n"
            f'echo "$*" >> "{self.calls_log}"\n'
            'for a in ${STUB_MIDCLT_FAIL:-}; do [ "$3" = "$a" ] && exit 1; done\n'
            "exit 0\n"
        )
        midclt.chmod(0o755)

    @property
    def pending(self) -> Path:
        return self.tls / PENDING_NAME

    def run(
        self, export_rc: int, *, fail: str = "", extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run tls-rotate.sh once. `fail` = space-separated apps whose
        `midclt call app.redeploy` exits 1."""
        self.calls_log.unlink(missing_ok=True)
        env = {k: v for k, v in os.environ.items() if k not in _SCRIPT_ENV}
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["STUB_EXPORT_RC"] = str(export_rc)
        env["STUB_MIDCLT_FAIL"] = fail
        env.update(extra_env or {})
        return subprocess.run(
            [self.bash, str(self.script)], env=env, capture_output=True, text=True, timeout=60
        )

    def redeployed(self) -> list[str]:
        if not self.calls_log.exists():
            return []
        calls = self.calls_log.read_text().splitlines()
        assert all(c.startswith("call app.redeploy ") for c in calls), calls
        return [c.split()[-1] for c in calls]


@pytest.fixture
def nas(tmp_path: Path, bash: str) -> Nas:
    return Nas(tmp_path, bash)


# ── the three export outcomes ────────────────────────────────────────────────


def test_changed_cert_redeploys_every_consumer(nas: Nas) -> None:
    """THE regression guard -- fails against both earlier versions of the script."""
    proc = nas.run(10)
    assert proc.returncode == 0, proc.stderr
    assert nas.redeployed() == PRODUCTION_CONSUMERS, proc.stderr
    # The line the runbook and the cron log check for.
    assert "cert rotated — redeploying TLS consumers" in proc.stderr
    assert not nas.pending.exists(), "pending list left behind after every redeploy succeeded"


def test_unchanged_cert_redeploys_nothing(nas: Nas) -> None:
    proc = nas.run(0)
    assert proc.returncode == 0, proc.stderr
    assert nas.redeployed() == []
    assert "no cert change" in proc.stderr


@pytest.mark.parametrize("export_rc", [1, 2, 11])
def test_export_error_redeploys_nothing_and_propagates(nas: Nas, export_rc: int) -> None:
    proc = nas.run(export_rc)
    assert proc.returncode == export_rc, proc.stderr
    assert nas.redeployed() == []
    assert f"tls-export.sh failed with exit={export_rc}" in proc.stderr


# ── a missed redeploy is retried, not forgotten ─────────────────────────────


def test_failed_redeploy_is_retried_on_the_next_run(nas: Nas) -> None:
    """tls-export.sh reports a change exactly once: the next run finds the files
    already in place and exits 0. A redeploy that failed must survive that."""
    first = nas.run(10, fail="app-two", extra_env=MULTI_ENV)
    assert first.returncode == 3, first.stderr
    assert nas.redeployed() == MULTI
    assert nas.pending.read_text().split() == ["app-two"]

    # Next hour: export sees no change. Only the app still pending is retried.
    second = nas.run(0, extra_env=MULTI_ENV)
    assert second.returncode == 0, second.stderr
    assert nas.redeployed() == ["app-two"]
    assert not nas.pending.exists()

    # And the hour after that, nothing is left to do.
    third = nas.run(0, extra_env=MULTI_ENV)
    assert third.returncode == 0, third.stderr
    assert nas.redeployed() == []


def test_failed_production_redeploy_is_retried_with_the_default_list(nas: Nas) -> None:
    """Same thing with no env overrides at all, i.e. exactly what the cronjob runs."""
    assert nas.run(10, fail="traefik").returncode == 3
    assert nas.pending.read_text().split() == ["traefik"]
    proc = nas.run(0)
    assert proc.returncode == 0, proc.stderr
    assert nas.redeployed() == ["traefik"]
    assert not nas.pending.exists()


def test_redeploy_that_keeps_failing_stays_pending(nas: Nas) -> None:
    nas.run(10, fail="app-two app-three", extra_env=MULTI_ENV)
    proc = nas.run(0, fail="app-three", extra_env=MULTI_ENV)
    assert proc.returncode == 3, proc.stderr
    assert nas.redeployed() == ["app-two", "app-three"], (
        "a retry must not re-bounce apps that already succeeded"
    )
    assert nas.pending.read_text().split() == ["app-three"]


def test_new_rotation_while_a_retry_is_pending_redeploys_everything(nas: Nas) -> None:
    nas.run(10, fail="app-two", extra_env=MULTI_ENV)
    proc = nas.run(10, extra_env=MULTI_ENV)
    assert proc.returncode == 0, proc.stderr
    assert nas.redeployed() == MULTI
    assert not nas.pending.exists()


def test_unwritable_pending_file_still_redeploys(nas: Nas, tmp_path: Path) -> None:
    """Recording the retry list is a safety net. Failing to write it must never
    cost the redeploy itself -- that would reintroduce the original bug."""
    proc = nas.run(10, extra_env={"PENDING_FILE": str(tmp_path / "no-such-dir" / "pending")})
    assert proc.returncode == 0, proc.stderr
    assert nas.redeployed() == PRODUCTION_CONSUMERS
    assert "will NOT be retried" in proc.stderr


# ── the consumer list matches what actually mounts the cert ─────────────────


def _default_consumers() -> list[str]:
    m = re.search(r'^TLS_CONSUMERS="\$\{TLS_CONSUMERS:-([^}]*)\}"', ROTATE.read_text(), re.M)
    assert m, "TLS_CONSUMERS default not found in tls-rotate.sh"
    return m.group(1).split()


def _enabled_apps() -> dict[str, Path]:
    cfg = yaml.safe_load(APPS_YAML.read_text())
    return {a["name"]: REPO / a["compose"] for a in cfg["apps"] if a.get("enabled")}


def _mounts_tls_dir(compose: Path) -> bool:
    doc = yaml.safe_load(compose.read_text())
    for svc in (doc.get("services") or {}).values():
        for vol in svc.get("volumes") or []:
            src = vol.split(":", 1)[0] if isinstance(vol, str) else vol.get("source", "")
            if src.rstrip("/") == TLS_DIR_ON_NAS:
                return True
    return False


def test_default_consumers_are_pinned() -> None:
    assert _default_consumers() == PRODUCTION_CONSUMERS


def test_every_app_that_mounts_the_cert_is_handled() -> None:
    """The invariant Traefik violated. An app that serves the wildcard from
    /mnt/tank/system/tls, does not re-read it, and is not in TLS_CONSUMERS keeps
    the old cert in memory after a renewal and serves it until it expires. A
    new app that mounts the dir must be put in TLS_CONSUMERS, or in
    RELOADS_ON_ITS_OWN with evidence. Leaving it out of both fails here."""
    mounting = sorted(n for n, c in _enabled_apps().items() if _mounts_tls_dir(c))
    assert "traefik" in mounting, "the compose parsing is broken: traefik mounts the TLS dir"
    consumers = _default_consumers()
    assert not set(consumers) & set(RELOADS_ON_ITS_OWN), "an app is both redeployed and exempt"
    assert sorted(consumers + list(RELOADS_ON_ITS_OWN)) == mounting


def test_exemptions_are_not_stale() -> None:
    """An exemption for an app that no longer mounts the dir (or no longer
    exists) is dead weight that could hide the next app of the same name."""
    enabled = _enabled_apps()
    for app in RELOADS_ON_ITS_OWN:
        assert app in enabled, f"{app} is exempt but not an enabled app"
        assert _mounts_tls_dir(enabled[app]), f"{app} is exempt but does not mount the TLS dir"


def test_every_default_consumer_is_an_enabled_app() -> None:
    """`midclt call app.redeploy <typo>` only logs a failure; catch it here."""
    enabled = _enabled_apps()
    assert [c for c in _default_consumers() if c not in enabled] == []
