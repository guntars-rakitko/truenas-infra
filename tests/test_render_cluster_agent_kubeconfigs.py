"""Guard tests for scripts/render-cluster-agent-kubeconfigs.sh.

WHY THIS EXISTS
---------------
The script mints the cluster-agent's ServiceAccount tokens, one per Doppler
key (KUBECONFIG_DEV / KUBECONFIG_PRD), each from its own admin kubeconfig. The
defaults are the MS-A2 clusters' since the Q170S1 teardown (kube-infra#1443);
either key can be pointed elsewhere by flag. It must refuse a run that would
leave the agent blind — the 14-day silent failure of 2026-08-21 — before any
token is minted: a missing kubeconfig, an unreachable cluster, both keys on
one cluster. (Until the teardown it also refused the MS-A2 BUILD addresses,
which the boxes have left: git history.)

Every external is stubbed: kubectl answers from the synthetic kubeconfigs
below and fakes the cluster verbs; doppler / curl / ssh fail loudly. NOTHING
here reaches a cluster. A run is stopped before the non --apply publish step
(which writes the hardcoded /tmp/cluster-agent-*.kubeconfig) by failing the
proof read of the RENDERED prd kubeconfig; an autouse fixture asserts that no
test ever wrote those /tmp files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "render-cluster-agent-kubeconfigs.sh"

SA_SUB = "system:serviceaccount:flux-system:cluster-agent-readonly"

# kubectl stub. `config view` is answered by reading the synthetic kubeconfig
# ($KUBECONFIG) line by line; cluster verbs are logged and faked.
#   STUB_NODES_RC  non-zero → `get nodes` fails (cluster unreachable)
#   STUB_ISS       the `iss` claim of every minted token
#   STUB_STOP_RAW  a substring of $KUBECONFIG whose `get --raw` fails (rc 97).
#                  The script proves the RENDERED file, $WORKDIR/cluster-agent-<key>.kubeconfig.
KUBECTL = r"""#!/usr/bin/env bash
echo "kubectl $* KUBECONFIG=$KUBECONFIG" >> "$STUB_LOG"
field() { sed -n "s/^ *$1: *//p" "$KUBECONFIG" | head -1; }
case "$*" in
  *"config view"*"cluster.server"*)               field server ;;
  *"config view"*"certificate-authority-data"*)   field certificate-authority-data ;;
  *"get nodes"*)
    [ "${STUB_NODES_RC:-0}" -eq 0 ] || { echo "stub: connection refused" >&2; exit 1; }
    echo "node/$(basename "$KUBECONFIG")-node" ;;
  *"create token"*)
    python3 - "$STUB_ISS" <<'PY'
import base64, json, sys, time
enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
print(enc({"alg": "none"}) + "." + enc({
    "sub": "system:serviceaccount:flux-system:cluster-agent-readonly",
    "iss": sys.argv[1], "exp": int(time.time()) + 86400}) + ".sig")
PY
    ;;
  *"get ns flux-system"*) echo "rendered-server $(field server)" >> "$STUB_LOG" ;;
  *"get --raw"*)
    case "$KUBECONFIG" in *"${STUB_STOP_RAW:?}"*) echo "stub: stop before publish" >&2; exit 97 ;; esac ;;
  *) echo "stub: unexpected kubectl $*" >&2; exit 98 ;;
esac
"""

FORBIDDEN = r"""#!/usr/bin/env bash
echo "FORBIDDEN $(basename "$0") $*" >> "$STUB_LOG"; exit 96
"""


def _kubeconfig(path: Path, server: str) -> Path:
    path.write_text(
        "apiVersion: v1\nkind: Config\nclusters:\n- name: c\n  cluster:\n"
        f"    server: {server}\n"
        "    certificate-authority-data: RkFLRS1DQS1OT1QtQS1TRUNSRVQ=\n"
        "users:\n- name: u\n  user: {}\ncontexts:\n- name: c\n"
        "  context: {cluster: c, user: u}\ncurrent-context: c\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def bash() -> str:
    """The script needs bash >= 4 (associative arrays), as it always has — the
    operator's Homebrew bash, not macOS's /bin/bash 3.2."""
    found = shutil.which("bash")
    assert found, "no bash on PATH"
    major = subprocess.run(
        [found, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True,
    ).stdout.strip()
    assert int(major) >= 4, f"{found} is bash {major}; the script needs bash >= 4"
    return found


class Rack:
    """The stub environment: a KUBECONFIG_DIR with the two default kubeconfigs
    (msa2-dev at .12, msa2-prd at .11, each node's own address: no VIP) and a
    bin/ of stubs."""

    def __init__(self, tmp: Path, bash: str) -> None:
        self.bash = bash
        self.dir = tmp / "talos-os"
        self.dir.mkdir()
        _kubeconfig(self.dir / "kubeconfig-msa2-dev", "https://10.10.5.12:6443")
        _kubeconfig(self.dir / "kubeconfig-msa2-prd", "https://10.10.5.11:6443")
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "kubectl").write_text(KUBECTL, encoding="utf-8")
        for name in ("doppler", "curl", "ssh"):
            (self.bin / name).write_text(FORBIDDEN, encoding="utf-8")
        for f in self.bin.iterdir():
            f.chmod(0o755)
        self.log = tmp / "calls.log"

    def kubeconfig(self, name: str, server: str) -> Path:
        return _kubeconfig(self.dir / name, server)

    def run(self, *args: str, **env_extra: str) -> tuple[int, str, list[str]]:
        self.log.write_text("", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k != "KUBECONFIG"}
        env.update(
            PATH=f"{self.bin}:{env['PATH']}",
            STUB_LOG=str(self.log),
            KUBECONFIG_DIR=str(self.dir),
            STUB_ISS="https://10.10.5.11:6443",
            STUB_STOP_RAW="cluster-agent-prd.kubeconfig",
        )
        env.update(env_extra)
        proc = subprocess.run(
            [self.bash, str(SCRIPT), *args],
            env=env, capture_output=True, text=True, timeout=60,
        )
        calls = self.log.read_text(encoding="utf-8").splitlines()
        return proc.returncode, proc.stdout + proc.stderr, calls


@pytest.fixture
def rack(tmp_path: Path, bash: str) -> Rack:
    return Rack(tmp_path, bash)


_PUBLISHED = [Path(f"/tmp/cluster-agent-{k}.kubeconfig") for k in ("dev", "prd")]


@pytest.fixture(autouse=True)
def never_reaches_the_publish_step():
    """The non --apply path installs the rendered files at a hardcoded /tmp
    path the operator copies from. A test that got that far would leave fake
    kubeconfigs there, so refuse to start over a real one, and fail if one
    appears."""
    pre = [p for p in _PUBLISHED if p.exists()]
    assert not pre, f"refusing to run over an operator's rendered kubeconfig: {pre}"
    yield
    leaked = [p for p in _PUBLISHED if p.exists()]
    for p in leaked:
        p.unlink()
    assert not leaked, f"a test reached the publish step and wrote {leaked}"


def _mints(calls: list[str]) -> list[str]:
    """The KUBECONFIG each `create token` ran against, in order."""
    return [c.split("KUBECONFIG=")[1] for c in calls if "create token" in c]


def _rendered(calls: list[str]) -> list[str]:
    return [c.split(" ", 1)[1] for c in calls if c.startswith("rendered-server ")]


def _no_publish(calls: list[str]) -> None:
    assert not [c for c in calls if c.startswith("FORBIDDEN")], calls


# ── the default path: the MS-A2 clusters ────────────────────────────────────


def test_defaults_mint_from_the_msa2_kubeconfigs(rack: Rack) -> None:
    rc, out, calls = rack.run()
    assert rc == 97, out  # stopped by the stub at prd's services-proxy read
    assert _mints(calls) == [
        str(rack.dir / "kubeconfig-msa2-dev"), str(rack.dir / "kubeconfig-msa2-prd"),
    ]
    assert _rendered(calls) == ["https://10.10.5.12:6443", "https://10.10.5.11:6443"]
    assert "nodes   : kubeconfig-msa2-dev-node" in out
    _no_publish(calls)


def test_the_q170s1_kubeconfigs_are_not_the_defaults_any_more(rack: Rack) -> None:
    """The old defaults pointed at kub-dev / kub-prd, dark since the cutover.
    With only those files present, the run stops before any mint."""
    for env in ("dev", "prd"):
        (rack.dir / f"kubeconfig-msa2-{env}").unlink()
    _kubeconfig(rack.dir / "kubeconfig-dev", "https://10.10.5.3:6443")
    _kubeconfig(rack.dir / "kubeconfig-prd", "https://10.10.5.2:6443")
    rc, out, calls = rack.run()
    assert rc == 1
    assert f"dev admin kubeconfig not found: {rack.dir / 'kubeconfig-msa2-dev'}" in out
    assert _mints(calls) == []


# ── per-key source selection ─────────────────────────────────────────────────


def test_one_key_can_be_pointed_elsewhere(rack: Rack) -> None:
    other = rack.kubeconfig("kubeconfig-other", "https://10.10.5.99:6443")
    rc, out, calls = rack.run("--prd-kubeconfig", str(other), STUB_ISS="https://10.10.5.99:6443")
    assert rc == 97, out
    assert _mints(calls) == [str(rack.dir / "kubeconfig-msa2-dev"), str(other)]
    assert f"prd: {other} -> https://10.10.5.99:6443" in out


def test_missing_override_is_fatal_before_any_mint(rack: Rack) -> None:
    rc, out, calls = rack.run("--dev-kubeconfig", "/nonexistent/kubeconfig")
    assert rc == 1
    assert "dev admin kubeconfig not found: /nonexistent/kubeconfig" in out
    assert _mints(calls) == []


def test_the_build_address_flag_is_gone(rack: Rack) -> None:
    rc, out, calls = rack.run("--allow-build-address")
    assert rc == 2
    assert "unknown arg: --allow-build-address" in out
    assert calls == []


# ── pre-flight refusals: nothing is minted ───────────────────────────────────


def test_dev_and_prd_on_the_same_server_is_refused(rack: Rack) -> None:
    rc, out, calls = rack.run("--dev-kubeconfig", str(rack.dir / "kubeconfig-msa2-prd"))
    assert rc == 1
    assert "dev and prd both point at https://10.10.5.11:6443" in out
    assert _mints(calls) == []


def test_unreachable_cluster_is_refused_before_any_mint(rack: Rack) -> None:
    rc, out, calls = rack.run(STUB_NODES_RC="1")
    assert rc == 1
    assert "cannot list nodes on dev" in out
    assert _mints(calls) == []


# ── post-mint ────────────────────────────────────────────────────────────────


def test_each_token_issuer_is_printed(rack: Rack) -> None:
    """The issuer is what an endpoint change invalidates, so the operator sees
    it for every key before anything is published."""
    rc, out, calls = rack.run()
    assert rc == 97, out
    assert out.count("issuer  : https://10.10.5.11:6443") == 2, out


def test_help_prints_the_whole_header(rack: Rack) -> None:
    rc, out, calls = rack.run("--help")
    assert rc == 0
    assert "## Pre-flight (runs before ANY token is minted)" in out
    assert "## Verification after running with --apply" in out
    assert "set -euo pipefail" not in out
    assert calls == []
