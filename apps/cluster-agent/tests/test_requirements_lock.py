"""The container's /venv is built from requirements.lock.txt — keep it honest.

Chain: pyproject.toml -> uv.lock -> requirements.lock.txt -> /venv on the NAS.
The compose startup script pip-installs EXACTLY requirements.lock.txt
(`--require-hashes --only-binary=:all:`), so every link must hold:

- the lock satisfies pyproject (a Renovate floor bump that skipped `uv lock`
  broke that once: 82a91ee);
- the committed export IS `uv export` of the lock. A stale export silently
  keeps production on the old versions while the tests run the new ones —
  the exact runtime-deps drift this file exists to end (until 2026-09-29 the
  compose ignored the lock and pip-installed hand-written pins);
- every runtime package has a wheel the alpine container can install, since
  source builds are refused (the 2026-05-25 crash loop was a pydantic-core
  source build on 3.14-alpine);
- the compose installs from the file, not from inline pins.

Regenerate after any pyproject/lock change, from apps/cluster-agent/:

    uv lock
    uv export --frozen --no-dev --no-emit-project --format requirements-txt \\
        > requirements.lock.txt
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import yaml
from packaging.markers import Marker
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

APP_DIR = Path(__file__).resolve().parents[1]
REQUIREMENTS = APP_DIR / "requirements.lock.txt"
LOCK = APP_DIR / "uv.lock"
COMPOSE = APP_DIR / "docker-compose.yaml"

# Must match the regeneration command in the module docstring, the
# pyproject.toml header and truenas-infra CLAUDE.md — uv writes this exact
# command into the file's header, so the comparison below covers it too.
EXPORT_ARGS = ["export", "--frozen", "--no-dev", "--no-emit-project",
               "--format", "requirements-txt"]

# The NAS is an Intel N150 (x86_64); python:3.X-alpine ships musl 1.2.x.
CONTAINER_PLATFORMS = [f"musllinux_1_{minor}_x86_64" for minor in (2, 1, 0)]


def _uv() -> str:
    """`uv run` exports UV=<its own binary>; fall back to PATH. Fail, never
    skip: a skipped drift check reads as a passing one."""
    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv, "uv not found — run the suite with `uv run --extra dev pytest`"
    return uv


def _compose_service() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]["cluster-agent"]


def _container_python() -> tuple[int, int]:
    image = _compose_service()["image"]
    m = re.match(r"python:(\d+)\.(\d+)-alpine@sha256:[0-9a-f]{64}$", image)
    assert m, f"unexpected cluster-agent image {image!r}: expected python:X.Y-alpine@sha256:…"
    return int(m.group(1)), int(m.group(2))


def _runtime_requirements() -> list[tuple[str, str, str | None]]:
    """(name, version, marker) for every pinned line of requirements.lock.txt."""
    reqs = []
    for line in REQUIREMENTS.read_text().splitlines():
        if not line or line[0] in "# ":
            continue  # header/annotation comments, `--hash=` continuation lines
        spec = line.rstrip(" \\")
        m = re.fullmatch(r"([A-Za-z0-9._-]+)==([^\s;]+)(?:\s*;\s*(.+))?", spec)
        assert m, f"unparseable requirement line: {line!r}"
        reqs.append((m.group(1), m.group(2), m.group(3)))
    assert reqs, "requirements.lock.txt has no requirements: the parse is broken"
    return reqs


def test_lock_satisfies_pyproject():
    """`uv lock --check` — the lock is what pyproject.toml resolves to."""
    out = subprocess.run(
        [_uv(), "lock", "--check", "--offline"],
        cwd=APP_DIR, capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, (
        "uv.lock is out of date with pyproject.toml — run `uv lock` in "
        f"apps/cluster-agent/, then re-export requirements.lock.txt:\n{out.stderr}"
    )


def test_requirements_file_is_the_export_of_the_lock():
    """The file the container installs is byte-identical to `uv export`."""
    out = subprocess.run(
        [_uv(), *EXPORT_ARGS],
        cwd=APP_DIR, capture_output=True, text=True, check=True,
    )
    assert REQUIREMENTS.read_text() == out.stdout, (
        "requirements.lock.txt is stale: production would keep the OLD versions "
        "while tests run the lock. Regenerate from apps/cluster-agent/:\n"
        "  uv export --frozen --no-dev --no-emit-project --format requirements-txt "
        "> requirements.lock.txt"
    )


def test_every_requirement_is_hash_pinned():
    """--require-hashes rejects the whole install if one line lacks a hash, so
    catch it here rather than as a crash-looping container (and catch a
    `--no-hashes` sneaking into EXPORT_ARGS, which the export test would bless)."""
    hashed: dict[str, bool] = {}
    current = None
    for line in REQUIREMENTS.read_text().splitlines():
        if line and line[0] not in "# ":
            current = line.split("==", 1)[0]
            hashed[current] = False
        elif current and line.strip().startswith("--hash=sha256:"):
            hashed[current] = True
    assert hashed, "requirements.lock.txt has no requirements: the parse is broken"
    unhashed = sorted(n for n, ok in hashed.items() if not ok)
    assert not unhashed, f"no --hash for: {', '.join(unhashed)}"


def test_every_runtime_package_has_a_wheel_for_the_container():
    """Each package the container installs has a wheel for its cpXY-musllinux
    x86_64, or a pure-Python one. The install runs `--only-binary=:all:`, so a
    missing wheel is a container that never starts — find out at PR time.
    The Python minor comes from the compose `image:` tag, so bumping the image
    (e.g. to 3.14) re-runs this against the new ABI."""
    major, minor = _container_python()
    py = f"{major}.{minor}"
    supported = set(cpython_tags((major, minor), abis=[f"cp{major}{minor}"],
                                 platforms=CONTAINER_PLATFORMS))
    supported |= set(compatible_tags((major, minor), interpreter=f"cp{major}{minor}",
                                     platforms=CONTAINER_PLATFORMS))
    env = {
        "implementation_name": "cpython", "platform_python_implementation": "CPython",
        "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
        "platform_machine": "x86_64", "python_version": py,
        "python_full_version": f"{py}.0", "implementation_version": f"{py}.0",
    }
    lock = {canonicalize_name(p["name"]): p
            for p in tomllib.loads(LOCK.read_text())["package"]}

    checked, missing = [], []
    for name, version, marker in _runtime_requirements():
        if marker and not Marker(marker).evaluate(env):
            continue  # e.g. colorama/tzdata (win32 only)
        pkg = lock[canonicalize_name(name)]
        assert pkg["version"] == version, f"{name}: export {version} != lock {pkg['version']}"
        wheel_tags = set()
        for wheel in pkg.get("wheels", []):
            wheel_tags |= set(parse_wheel_filename(wheel["url"].rsplit("/", 1)[-1])[3])
        checked.append(name)
        if not wheel_tags & supported:
            missing.append(f"{name}=={version}")
    assert checked, "no requirement applied to the container: the marker env is broken"
    assert not missing, (
        f"no cp{major}{minor}-musllinux x86_64 or pure-Python wheel for: "
        f"{', '.join(missing)} — the container would refuse to install (source "
        f"builds are disabled). Hold that bump, or pin a version that ships the wheel."
    )


def test_compose_installs_exactly_the_requirements_file():
    """The startup script installs from the file, hash-checked, binaries only,
    and keys the venv rebuild on the file's sha256 (plus an import probe for
    a damaged venv) — and has no inline pins left for the lock to drift away
    from."""
    script = _compose_service()["command"][-1]
    lines = script.splitlines()
    starts = [i for i, ln in enumerate(lines) if "pip install" in ln]
    assert len(starts) == 1, f"expected ONE pip install in the compose command, got {len(starts)}"
    cmd = [lines[starts[0]]]
    while cmd[-1].rstrip().endswith("\\"):
        cmd.append(lines[starts[0] + len(cmd)])
    install = " ".join(cmd)
    for flag in ("--require-hashes", "--only-binary=:all:", '-r "$$req"'):
        assert flag in install, f"compose pip install lacks {flag}: {install!r}"
    assert "req=/app/requirements.lock.txt" in script
    assert 'sha256sum "$$req"' in script, "the venv rebuild is not keyed on the file's hash"
    assert not re.search(r"[A-Za-z0-9_.\]-]==[0-9]", script), \
        "inline `pkg==X` pins are back in the compose command — use the lock"
    probe = re.search(r"/venv/bin/python -c '([^']*)'", script)
    assert probe and re.match(r"import \w", probe.group(1)), (
        "the /venv health probe must IMPORT the core packages, not just start "
        "the interpreter: site-packages damaged under an intact stamp would "
        f"crash-loop at import instead of rebuilding (got {probe and probe.group(0)!r})"
    )
