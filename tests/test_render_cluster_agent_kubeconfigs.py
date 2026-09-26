"""Guard tests for scripts/render-cluster-agent-kubeconfigs.sh.

WHY THIS EXISTS
---------------
The script mints the cluster-agent's ServiceAccount tokens. At the MS-A2
cutover each of its two keys (KUBECONFIG_DEV / KUBECONFIG_PRD) moves from a
Q170S1 cluster to an MS-A2 one, one cluster at a time (kube-infra msa2 plan
§ Cutover inventory row 16), so the source kubeconfig became selectable per
key. The dangerous mistake it must refuse is minting against an MS-A2 BUILD
address: Talos makes the endpoint the token issuer, so the re-address kills
that token (plan D12) and the agent goes blind again, the 14-day silent
failure of 2026-08-21.

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
    (kub-dev on the dev VIP, kub-prd on the prd VIP) and a bin/ of stubs."""

    def __init__(self, tmp: Path, bash: str) -> None:
        self.bash = bash
        self.dir = tmp / "talos-os"
        self.dir.mkdir()
        _kubeconfig(self.dir / "kubeconfig-dev", "https://10.10.5.3:6443")
        _kubeconfig(self.dir / "kubeconfig-prd", "https://10.10.5.2:6443")
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
            STUB_ISS="https://10.10.5.2:6443",
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


# ── the default path is unchanged ────────────────────────────────────────────


def test_defaults_mint_from_kubeconfig_dev_and_prd(rack: Rack) -> None:
    rc, out, calls = rack.run()
    assert rc == 97, out  # stopped by the stub at prd's services-proxy read
    assert _mints(calls) == [str(rack.dir / "kubeconfig-dev"), str(rack.dir / "kubeconfig-prd")]
    assert _rendered(calls) == ["https://10.10.5.3:6443", "https://10.10.5.2:6443"]
    _no_publish(calls)


# ── per-key source selection ─────────────────────────────────────────────────


def test_mixed_period_prd_from_msa2_dev_from_kub_dev(rack: Rack) -> None:
    """Row 16(d): prd = msa2-prd (re-addressed to its final .11), dev = kub-dev."""
    msa2_prd = rack.kubeconfig("kubeconfig-msa2-prd", "https://10.10.5.11:6443")
    rc, out, calls = rack.run(
        "--prd-kubeconfig", str(msa2_prd),
        STUB_ISS="https://10.10.5.11:6443",
    )
    assert rc == 97, out
    assert _mints(calls) == [str(rack.dir / "kubeconfig-dev"), str(msa2_prd)]
    assert _rendered(calls) == ["https://10.10.5.3:6443", "https://10.10.5.11:6443"]
    assert f"prd: {msa2_prd} -> https://10.10.5.11:6443" in out
    assert "nodes   : kubeconfig-msa2-prd-node" in out


def test_missing_override_is_fatal_before_any_mint(rack: Rack) -> None:
    rc, out, calls = rack.run("--dev-kubeconfig", "/nonexistent/kubeconfig")
    assert rc == 1
    assert "dev admin kubeconfig not found: /nonexistent/kubeconfig" in out
    assert _mints(calls) == []


# ── pre-flight refusals: nothing is minted ───────────────────────────────────


@pytest.mark.parametrize("key,server", [
    ("prd", "https://10.10.5.17:6443"),  # msa2-prd build address
    ("dev", "https://10.10.5.18:6443"),  # msa2-dev build address
])
def test_build_address_is_refused_before_any_mint(rack: Rack, key: str, server: str) -> None:
    kc = rack.kubeconfig(f"kubeconfig-msa2-{key}", server)
    rc, out, calls = rack.run(f"--{key}-kubeconfig", str(kc))
    assert rc == 1
    assert f"{key} server {server} is an MS-A2 BUILD address" in out
    assert _mints(calls) == []


def test_allow_build_address_overrides_with_a_warning(rack: Rack) -> None:
    kc = rack.kubeconfig("kubeconfig-msa2-dev", "https://10.10.5.18:6443")
    rc, out, calls = rack.run(
        "--dev-kubeconfig", str(kc), "--allow-build-address",
        STUB_ISS="https://10.10.5.18:6443",
    )
    assert rc == 97, out
    assert "WARNING : MS-A2 BUILD address" in out
    assert _mints(calls) == [str(kc), str(rack.dir / "kubeconfig-prd")]


@pytest.mark.parametrize("server", [
    "https://10.10.5.1:6443",     # a prefix of both build addresses
    "https://10.10.5.170:6443",   # has a build address as a prefix
    "https://10.10.5.11:6443",    # msa2-prd FINAL address
])
def test_near_miss_is_not_a_build_address(rack: Rack, server: str) -> None:
    kc = rack.kubeconfig("kubeconfig-other", server)
    rc, out, calls = rack.run("--prd-kubeconfig", str(kc))
    assert rc == 97, out
    assert "BUILD address" not in out
    assert len(_mints(calls)) == 2


def test_dev_and_prd_on_the_same_server_is_refused(rack: Rack) -> None:
    rc, out, calls = rack.run("--dev-kubeconfig", str(rack.dir / "kubeconfig-prd"))
    assert rc == 1
    assert "dev and prd both point at https://10.10.5.2:6443" in out
    assert _mints(calls) == []


def test_unreachable_cluster_is_refused_before_any_mint(rack: Rack) -> None:
    rc, out, calls = rack.run(STUB_NODES_RC="1")
    assert rc == 1
    assert "cannot list nodes on dev" in out
    assert _mints(calls) == []


# ── post-mint: the token's own issuer ────────────────────────────────────────


def test_token_issuer_on_a_build_address_is_refused(rack: Rack) -> None:
    """A hostname server passes the pre-flight; the issuer is what the
    re-address invalidates, so it is checked on the minted token itself."""
    kc = rack.kubeconfig("kubeconfig-by-name", "https://msa2-dev-01.w1.lv:6443")
    rc, out, calls = rack.run("--dev-kubeconfig", str(kc), STUB_ISS="https://10.10.5.18:6443")
    assert rc == 1
    assert "dev token issuer https://10.10.5.18:6443 is an MS-A2 BUILD address" in out
    assert _mints(calls) == [str(kc)]      # minted, then refused ...
    assert _rendered(calls) == []          # ... before any render or proof
    _no_publish(calls)


def test_help_prints_the_whole_header(rack: Rack) -> None:
    rc, out, calls = rack.run("--help")
    assert rc == 0
    assert "## Pre-flight (runs before ANY token is minted)" in out
    assert "## Verification after running with --apply" in out
    assert "set -euo pipefail" not in out
    assert calls == []
