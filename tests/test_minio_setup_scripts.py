"""Guard tests for scripts/setup-minio-{buckets,encryption,lifecycle}.sh.

WHY THIS EXISTS
---------------
The three scripts keep three hand-written lists of bucket names, and two of
the lists' mistakes are silent on a live run:

- A bucket missing from setup-minio-buckets.sh is never created, and its
  consumer fails with "bucket missing" on the first backup.
- A name in setup-minio-encryption.sh that setup-minio-buckets.sh never
  creates (a typo, `postgres-backup` for `postgres-backups`) is a "SKIP ... bucket
  missing" line and exit 0: default encryption is never set anywhere.
- An ILM row in setup-minio-lifecycle.sh on a bucket whose client owns
  deletion destroys that client's data by age (for a restic repository the
  first objects to expire are its `config` and `keys/*`, so no password opens
  it afterwards). The script's header says so; this makes it a failing test
  instead of a comment.

Every external is stubbed: `mc` answers from environment switches and logs its
arguments. PATH holds only the stub directory and /usr/bin:/bin, so the
operator's real `mc` (Homebrew) cannot be reached, and MC_CONFIG_DIR points at
an empty temporary directory in case it were. NOTHING here reaches a MinIO.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
BUCKETS_SH = SCRIPTS / "setup-minio-buckets.sh"
ENCRYPTION_SH = SCRIPTS / "setup-minio-encryption.sh"
LIFECYCLE_SH = SCRIPTS / "setup-minio-lifecycle.sh"

ALIASES = ("nas-dev", "nas-prd")

# Buckets whose own client prunes them, so an age-based ILM rule would fight it
# (setup-minio-lifecycle.sh header): Loki's compactor, Litestream. Until the
# 2026-10-06 teardown this also held longhorn, velero and pvc-backups.
NO_ILM_BUCKETS = frozenset({"loki-chunks", "pocket-id-litestream"})

# Deleted at the Q170S1 teardown (2026-10-06, kube-infra#1443). A bootstrap that
# recreated them would bring back empty buckets that nothing writes.
RETIRED_BUCKETS = frozenset({"longhorn", "velero", "pvc-backups"})

# mc stub. Every call is logged as "mc <args>".
#   STUB_UNREACHABLE    space-separated aliases whose `mc ls <alias>` fails
#   STUB_BUCKETS_EXIST  1 (default): `mc ls <alias>/<bucket>` succeeds; 0: fails
# Anything the scripts are not expected to run is logged as UNEXPECTED, exit 98.
MC = r"""#!/bin/bash
echo "mc $*" >> "$STUB_LOG"
case "$1" in
  ls)
    case "$2" in
      */*) [ "${STUB_BUCKETS_EXIST:-1}" = 1 ] && exit 0; exit 1 ;;
      *) case " ${STUB_UNREACHABLE:-} " in *" $2 "*) exit 1 ;; esac; exit 0 ;;
    esac ;;
  mb) exit 0 ;;
  admin) [ "$2 $3 $4" = "kms key status" ] && exit 0 ;;
  encrypt)
    case "$2" in
      info) echo "Auto encryption is not enabled for $3"; exit 0 ;;
      set) exit 0 ;;
    esac ;;
  ilm) [ "$2" = rule ] && exit 0 ;;
esac
echo "UNEXPECTED mc $*" >> "$STUB_LOG"
echo "stub: unexpected mc $*" >&2
exit 98
"""


def _run(
    script: Path, tmp_path: Path, *, unreachable: str = "", buckets_exist: bool = True
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    mc = stub / "mc"
    mc.write_text(MC, encoding="utf-8")
    mc.chmod(0o755)
    log = tmp_path / "mc.log"
    log.write_text("", encoding="utf-8")
    mc_config = tmp_path / "mc-config"
    mc_config.mkdir(exist_ok=True)

    bash = shutil.which("bash")
    assert bash, "bash not found"
    env = {
        "PATH": f"{stub}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "MC_CONFIG_DIR": str(mc_config),
        "STUB_LOG": str(log),
        "STUB_UNREACHABLE": unreachable,
        "STUB_BUCKETS_EXIST": "1" if buckets_exist else "0",
    }
    proc = subprocess.run(
        [bash, str(script)], env=env, capture_output=True, text=True, timeout=60, check=False
    )
    calls = log.read_text(encoding="utf-8").splitlines()
    return proc, calls


def _targets(calls: list[str], prefix: str) -> set[str]:
    """The last argument (<alias>/<bucket>) of every logged call starting with prefix."""
    return {c.split()[-1] for c in calls if c.startswith(prefix)}


def _unexpected(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("UNEXPECTED")]


@pytest.fixture
def created(tmp_path: Path) -> set[str]:
    """<alias>/<bucket> for every `mc mb` setup-minio-buckets.sh runs on an empty MinIO."""
    proc, calls = _run(BUCKETS_SH, tmp_path, buckets_exist=False)
    assert proc.returncode == 0, proc.stderr
    assert _unexpected(calls) == []
    return _targets(calls, "mc mb ")


def test_buckets_script_does_not_recreate_the_retired_buckets(created: set[str]) -> None:
    assert created, "no bucket created at all: the stub or the parse is broken"
    assert {t for t in created if t.split("/", 1)[1] in RETIRED_BUCKETS} == set()


def test_buckets_script_creates_the_same_buckets_on_both_instances(created: set[str]) -> None:
    per_alias = {a: {t.split("/", 1)[1] for t in created if t.startswith(f"{a}/")} for a in ALIASES}
    assert per_alias["nas-dev"], "no bucket created at all: the stub or the parse is broken"
    assert per_alias["nas-dev"] == per_alias["nas-prd"]


def test_encryption_script_sets_sse_s3_on_postgres_backups_on_both_instances(tmp_path: Path) -> None:
    proc, calls = _run(ENCRYPTION_SH, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _unexpected(calls) == []
    encrypted = _targets(calls, "mc encrypt set sse-s3 ")
    assert {f"{a}/postgres-backups" for a in ALIASES} <= encrypted


def test_every_encrypted_bucket_is_one_the_buckets_script_creates(
    tmp_path: Path, created: set[str]
) -> None:
    proc, calls = _run(ENCRYPTION_SH, tmp_path)
    assert proc.returncode == 0, proc.stderr
    encrypted = _targets(calls, "mc encrypt set sse-s3 ")
    assert encrypted, "no bucket encrypted at all: the stub or the parse is broken"
    # A name only in the encryption list is a live "SKIP ... bucket missing", exit 0.
    assert encrypted - created == set()


def test_lifecycle_never_expires_a_bucket_whose_client_owns_deletion(
    tmp_path: Path, created: set[str]
) -> None:
    proc, calls = _run(LIFECYCLE_SH, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert _unexpected(calls) == []
    ilm = _targets(calls, "mc ilm rule ")
    # Non-vacuous: an empty set would pass the next assert for the wrong reason.
    assert ilm, "no ILM call at all: the stub or the parse is broken"
    assert {t for t in ilm if t.split("/", 1)[1] in NO_ILM_BUCKETS} == set()
    # A rule on a bucket that is never created is a live "SKIP", exit 0.
    assert ilm - created == set()


@pytest.mark.parametrize("script", [BUCKETS_SH, ENCRYPTION_SH], ids=lambda p: p.name)
def test_unreachable_alias_is_skipped_without_running_mc_alias(
    script: Path, tmp_path: Path
) -> None:
    # setup-minio-buckets.sh used to put `mc alias set` in backticks inside a
    # double-quoted echo, so an unreachable alias RAN `mc alias set` (usage
    # error) instead of printing the hint. Only the reachability probes may run.
    proc, calls = _run(script, tmp_path, unreachable=" ".join(ALIASES))
    assert proc.returncode == 0, proc.stderr
    assert calls == [f"mc ls {a}" for a in ALIASES]
    for a in ALIASES:
        assert f"SKIP  {a} (alias unreachable — set up `mc alias set` first)" in proc.stdout
