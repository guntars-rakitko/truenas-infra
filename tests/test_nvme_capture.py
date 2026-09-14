"""Control-flow tests for scripts/nvme-drop-capture.sh.

WHY THIS EXISTS
---------------
On 2026-09-14 the pool SUSPENDED and the capture script recorded nothing,
losing the kernel signature for the only double-drop on record. The bug was
control flow, not the capture commands: the script wrote to
/mnt/tank/system/nvme-diag -- the pool being diagnosed -- and a SUSPENDED pool
blocks all I/O uninterruptibly, so its unconditional `mkdir -p` hung.

The prior "SELFTEST" only checked that dmesg/nvme/aer produce output. It could
never have caught this, because it never exercised the healthy-vs-unhealthy
branch. These tests do exactly that.

`test_unhealthy_never_touches_archive` is the regression guard: it FAILS
against the pre-2026-09-14 script.
"""
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "nvme-drop-capture.sh"


def _run(tmp_path, healthy: bool):
    """Run the script with every external command stubbed.

    The archive dir is deliberately NOT created: if the unhealthy path tries to
    write there, the stubs record it and we can assert on it.
    """
    local = tmp_path / "local"
    archive = tmp_path / "archive"
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    touched = tmp_path / "archive-touched.log"

    status = "pool 'tank' is healthy" if healthy else (
        "  pool: tank\n state: SUSPENDED\nstatus: One or more devices are faulted."
    )
    (bindir / "zpool").write_text(
        "#!/bin/bash\n"
        f"if [ \"$1\" = status ] && [ \"$2\" = -x ]; then printf '%s\\n' {status!r}; exit 0; fi\n"
        "echo stub-zpool \"$@\"\n"
    )
    # Every other external the script calls -> harmless stub.
    for cmd in ("dmesg", "journalctl", "lspci", "nvme", "upsc", "logger",
                "uptime", "lsblk", "timeout"):
        if cmd == "timeout":
            # emulate `timeout N cmd ...` by dropping the duration
            (bindir / cmd).write_text('#!/bin/bash\nshift\nexec "$@"\n')
        else:
            (bindir / cmd).write_text(f'#!/bin/bash\necho stub-{cmd} "$@"\n')
    # Wrap mkdir/cp so any attempt against the archive path is recorded.
    for cmd in ("mkdir", "cp"):
        (bindir / cmd).write_text(
            "#!/bin/bash\n"
            f'case "$*" in *{archive}*) echo "{cmd} $*" >> "{touched}";; esac\n'
            f'exec /bin/{cmd} "$@"\n'
        )
    for f in bindir.iterdir():
        f.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["NVME_DIAG_LOCAL"] = str(local)
    env["NVME_DIAG_ARCHIVE"] = str(archive)
    proc = subprocess.run(["/bin/bash", str(SCRIPT)], env=env,
                          capture_output=True, text=True, timeout=60)
    return proc, local, archive, touched


def test_unhealthy_never_touches_archive(tmp_path):
    """THE regression guard — fails against the pre-2026-09-14 script."""
    proc, local, _archive, touched = _run(tmp_path, healthy=False)
    assert proc.returncode == 0, proc.stderr
    assert not touched.exists(), (
        "capture touched the archive path while the pool was unhealthy — this is "
        f"the 2026-09-14 bug: {touched.read_text() if touched.exists() else ''}"
    )


def test_unhealthy_captures_locally(tmp_path):
    """It must actually produce artifacts, not merely avoid tank."""
    proc, local, _archive, _touched = _run(tmp_path, healthy=False)
    assert proc.returncode == 0, proc.stderr
    caps = [d for d in local.iterdir() if d.is_dir()]
    assert len(caps) == 1, f"expected one capture dir, got {[d.name for d in caps]}"
    names = {f.name for f in caps[0].iterdir()}
    # the artifacts the triage table in docs/nvme-dropout-forensics.md relies on
    for required in ("00-when.txt", "01-zpool-status.txt",
                     "04-dmesg-nvme-pcie.txt", "12-lsblk.txt"):
        assert required in names, f"missing {required}; got {sorted(names)}"
    assert (local / ".incident-active").exists(), "marker not set"


def test_unhealthy_captures_only_once_per_incident(tmp_path):
    """The marker must suppress a second capture while still unhealthy."""
    _run(tmp_path, healthy=False)
    proc, local, _archive, _touched = _run(tmp_path, healthy=False)
    assert proc.returncode == 0
    caps = [d for d in local.iterdir() if d.is_dir()]
    assert len(caps) == 1, "re-captured while marker was set"


def test_healthy_flushes_to_archive_and_clears_marker(tmp_path):
    """On recovery: pending captures move to tank and the marker clears."""
    _run(tmp_path, healthy=False)           # incident
    proc, local, archive, _touched = _run(tmp_path, healthy=True)   # recovery
    assert proc.returncode == 0, proc.stderr
    assert not (local / ".incident-active").exists(), "marker not cleared"
    assert [d for d in archive.iterdir() if d.is_dir()], "nothing flushed to archive"
    assert not [d for d in local.iterdir() if d.is_dir()], "local copy not cleaned up"
