"""Control-flow tests for scripts/nas-ups-orchestrator.sh (the NAS NUT SHUTDOWNCMD).

WHY THIS EXISTS
---------------
The orchestrator only ever runs during a real power cut, or a drill. Before the
MS-A2 boxes joined it (2026-09-26) nothing exercised it off the NAS, and its one
known failure mode was never testable: apid's :50000 probe is UNAUTHENTICATED,
so a node that REJECTS the shutdown (another cluster's PKI, Talos maintenance
mode) still reads "up" and the poll burns the whole 300 s backstop on battery
(kube-infra plan § Cutover inventory row 15).

Every external the script touches is stubbed. The model of the rack:

  * a node EXISTS if it has a PKI entry; it is UP (answers :50000) per `up`;
  * the talosctl stub accepts a shutdown only when the --talosconfig basename
    equals the node's PKI (a different cluster's CA -> x509 -> rc 1), exactly
    as the real TLS handshake behaves; an absent node -> "no route" -> rc 1;
  * an accepted shutdown takes the node down after `delay` seconds;
  * `hang` makes the call block until something kills it;
  * `tracker_error` makes an ACCEPTED shutdown still exit 1, as the default
    `--wait` tracker can after the RPC succeeded.

The Q170S1 tests pin the old path: the six calls keep their exact argv, run
unbounded, and a Q170S1 node is polled whatever its rc — the legacy behaviour
this change keeps on purpose while the old estate is live. The msa2 FINAL
addresses (.11/.12) are also in the Q170S1 prd list: they follow that legacy
rule while kub-prd is live and rule (a) once it is gone (the script's rule
(d)); the rule-(d) tests pin both sides, including its one change to the
Q170S1 path.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ORCH = REPO / "scripts" / "nas-ups-orchestrator.sh"
SETUP = REPO / "scripts" / "setup-talos-shutdown-orchestrator.sh"

PRD = "prd-shutdown.talosconfig"
DEV = "dev-shutdown.talosconfig"
MSA2_PRD = "msa2-prd-shutdown.talosconfig"
MSA2_DEV = "msa2-dev-shutdown.talosconfig"
MAINTENANCE = "maintenance-mode-self-signed"  # matches no staged config

Q170S1_PRD = ["10.10.5.11", "10.10.5.12", "10.10.5.13"]
Q170S1_DEV = ["10.10.5.14", "10.10.5.15", "10.10.5.16"]
MSA2_PRD_BUILD, MSA2_PRD_FINAL = "10.10.5.17", "10.10.5.11"
MSA2_DEV_BUILD, MSA2_DEV_FINAL = "10.10.5.18", "10.10.5.12"


# The orchestrator must run under the NAS's bash (5.x) and must not break under
# macOS's /bin/bash 3.2 either, so run every scenario under each distinct bash.
def _bashes() -> list[str]:
    found = []
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
    return request.param


# ── stubs ────────────────────────────────────────────────────────────────────
TALOSCTL = r"""#!/bin/bash
S="$UPS_ORCH_TEST_STATE"
all="$*"; cfg=""; ip=""
while [ $# -gt 0 ]; do
  case "$1" in
    --talosconfig) cfg=$2; shift 2 ;;
    --nodes) ip=$2; shift 2 ;;
    *) shift ;;
  esac
done
echo "$all" >> "$S/calls.log"
[ -f "$S/hang/$ip" ] && exec sleep 30
if [ ! -f "$S/pki/$ip" ]; then
  echo "error: dial tcp $ip:50000: connect: no route to host" >&2; exit 1
fi
if [ "$(basename "$cfg")" != "$(cat "$S/pki/$ip")" ]; then
  echo "rpc error: x509: certificate signed by unknown authority ($ip)" >&2; exit 1
fi
d=0; [ -f "$S/delay/$ip" ] && d=$(cat "$S/delay/$ip")
echo $(( $(date +%s) + d )) > "$S/down_at/$ip"
if [ -f "$S/tracker_error/$ip" ]; then
  echo "error: $ip: tracker: event stream closed" >&2; exit 1
fi
echo "$ip: shutdown accepted"
"""

# nc -z -w2 <ip> 50000
NC = r"""#!/bin/bash
S="$UPS_ORCH_TEST_STATE"; ip=$3
echo "$ip" >> "$S/probes.log"
[ -f "$S/up/$ip" ] || exit 1
if [ -f "$S/down_at/$ip" ] && [ "$(date +%s)" -ge "$(cat "$S/down_at/$ip")" ]; then exit 1; fi
exit 0
"""

# coreutils `timeout SECS cmd...` (macOS has none): run cmd, and when SECS pass,
# SIGTERM it and exit 124 — the same contract as coreutils.
TIMEOUT = r"""#!/bin/bash
echo "$*" >> "$UPS_ORCH_TEST_STATE/timeout.log"
case "$1" in --kill-after=*) shift ;; esac
exec perl -e '
  my $secs = shift @ARGV;
  my $pid = fork() // die "fork: $!";
  if ($pid == 0) { exec { $ARGV[0] } @ARGV or die "exec: $!" }
  $SIG{ALRM} = sub { kill "TERM", $pid; waitpid $pid, 0; exit 124 };
  alarm $secs;
  waitpid $pid, 0;
  exit(($? & 127) ? 128 + ($? & 127) : $? >> 8);
' "$@"
"""

HALT = r"""#!/bin/bash
echo "$(date +%s) $*" >> "$UPS_ORCH_TEST_STATE/halt.log"
"""


@dataclass
class Node:
    pki: str  # basename of the talosconfig this node's CA accepts
    up: bool = True
    delay: int = 0
    hang: bool = False
    tracker_error: bool = False


@dataclass
class Run:
    proc: subprocess.CompletedProcess[str]
    state: Path
    talos_dir: Path
    log: str
    elapsed: float
    calls: list[str] = field(default_factory=list)

    def lines(self, name: str) -> list[str]:
        f = self.state / name
        return f.read_text().splitlines() if f.exists() else []

    @property
    def probes(self) -> set[str]:
        return set(self.lines("probes.log"))

    @property
    def halts(self) -> list[str]:
        return self.lines("halt.log")

    def calls_for(self, ip: str) -> list[str]:
        return [c for c in self.calls if f"--nodes {ip} " in c + " "]


def q170s1_live() -> dict[str, Node]:
    nodes = {ip: Node(PRD) for ip in Q170S1_PRD}
    nodes.update({ip: Node(DEV) for ip in Q170S1_DEV})
    return nodes


def run_orchestrator(
    tmp_path: Path,
    bash: str,
    nodes: dict[str, Node],
    staged: tuple[str, ...] = (PRD, DEV),
    *,
    talosctl: bool = True,
    timeout: int = 20,
    poll: int = 1,
    msa2_call_timeout: int | None = 60,
) -> Run:
    """msa2_call_timeout=None leaves UPS_ORCH_MSA2_CALL_TIMEOUT unset, so the
    script's own production default applies."""
    state, talos_dir, bindir = tmp_path / "state", tmp_path / "talos", tmp_path / "bin"
    for d in (
        state,
        talos_dir,
        bindir,
        *(state / s for s in ("pki", "up", "delay", "hang", "down_at", "tracker_error")),
    ):
        d.mkdir(parents=True, exist_ok=True)
    for ip, n in nodes.items():
        (state / "pki" / ip).write_text(n.pki)
        if n.up:
            (state / "up" / ip).write_text("")
        (state / "delay" / ip).write_text(str(n.delay))
        if n.hang:
            (state / "hang" / ip).write_text("")
        if n.tracker_error:
            (state / "tracker_error" / ip).write_text("")
    for cfg in staged:
        (talos_dir / cfg).write_text("context: stub\n")
    stubs = {bindir / "nc": NC, bindir / "timeout": TIMEOUT, bindir / "halt": HALT}
    if talosctl:
        stubs[talos_dir / "talosctl"] = TALOSCTL
    for path, text in stubs.items():
        path.write_text(text)
        path.chmod(0o755)

    log = tmp_path / "orchestrator.log"
    env = dict(os.environ)
    env.update(
        PATH=f"{bindir}:{env['PATH']}",
        UPS_ORCH_TEST_STATE=str(state),
        UPS_ORCH_TALOS_DIR=str(talos_dir),
        UPS_ORCH_LOG=str(log),
        # never the real /sbin/shutdown — the whole point of this seam
        UPS_ORCH_HALT=str(bindir / "halt"),
        UPS_ORCH_TIMEOUT=str(timeout),
        UPS_ORCH_POLL=str(poll),
    )
    if msa2_call_timeout is None:
        env.pop("UPS_ORCH_MSA2_CALL_TIMEOUT", None)
    else:
        env["UPS_ORCH_MSA2_CALL_TIMEOUT"] = str(msa2_call_timeout)
    t0 = time.monotonic()
    proc = subprocess.run([bash, str(ORCH)], env=env, capture_output=True, text=True, timeout=120)
    elapsed = time.monotonic() - t0
    run = Run(proc, state, talos_dir, log.read_text() if log.exists() else "", elapsed)
    run.calls = run.lines("calls.log")
    return run


def legacy_argv(talos_dir: Path, cfg: str, ip: str) -> str:
    """The Q170S1 call, character for character, as it was before MS-A2."""
    return f"--talosconfig {talos_dir}/{cfg} shutdown --force --nodes {ip} --endpoints {ip}"


def msa2_argv(talos_dir: Path, cfg: str, ip: str) -> str:
    return (
        f"--talosconfig {talos_dir}/{cfg} shutdown --force --wait=false "
        f"--nodes {ip} --endpoints {ip}"
    )


def assert_clean_finish(run: Run) -> None:
    assert run.proc.returncode == 0, run.proc.stderr
    assert "all nodes down at" in run.log, run.log
    assert not re.search(r"TIMEOUT \d+s reached", run.log), run.log
    assert len(run.halts) == 1 and run.halts[0].endswith(" -P now"), run.halts


# ── Q170S1: unchanged ────────────────────────────────────────────────────────
def test_q170s1_path_unchanged_with_no_msa2_config_staged(tmp_path: Path, bash: str) -> None:
    """The NAS as it is today: only the two Q170S1 configs are staged."""
    run = run_orchestrator(tmp_path, bash, q170s1_live())
    assert_clean_finish(run)
    expected = [legacy_argv(run.talos_dir, PRD, ip) for ip in Q170S1_PRD]
    expected += [legacy_argv(run.talos_dir, DEV, ip) for ip in Q170S1_DEV]
    assert sorted(run.calls) == sorted(expected)
    assert run.lines("timeout.log") == [], "a Q170S1 call was wrapped in timeout"
    for cl, ips in (("prd", Q170S1_PRD), ("dev", Q170S1_DEV)):
        for ip in ips:
            assert f"] {cl} {ip} shutdown --force rc=0\n" in run.log
    assert "msa2-prd: no talosconfig staged" in run.log
    assert "msa2-dev: no talosconfig staged" in run.log
    assert "polling apid on 6 node(s)" in run.log
    assert run.probes == set(Q170S1_PRD + Q170S1_DEV)


def test_q170s1_calls_identical_when_msa2_configs_are_staged(tmp_path: Path, bash: str) -> None:
    nodes = q170s1_live()
    nodes[MSA2_DEV_BUILD] = Node(MSA2_DEV)
    run = run_orchestrator(tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_PRD, MSA2_DEV))
    assert_clean_finish(run)
    for cfg, ips in ((PRD, Q170S1_PRD), (DEV, Q170S1_DEV)):
        for ip in ips:
            assert legacy_argv(run.talos_dir, cfg, ip) in run.calls
    # every timeout-wrapped call is an MS-A2 one, and every MS-A2 call is wrapped
    wrapped = run.lines("timeout.log")
    assert len(wrapped) == 4 and all("--wait=false" in w for w in wrapped), wrapped


def test_q170s1_node_that_rejects_is_still_polled_legacy_behaviour(
    tmp_path: Path, bash: str
) -> None:
    """Pins the OLD behaviour, kept on purpose: a Q170S1 node is polled whatever
    its rc, so one that rejects its shutdown but stays up runs to the backstop."""
    nodes = q170s1_live()
    nodes["10.10.5.15"] = Node("rotated-pki")  # up, but rejects the staged config
    run = run_orchestrator(tmp_path, bash, nodes, timeout=3)
    assert run.proc.returncode == 0, run.proc.stderr
    assert "] dev 10.10.5.15 shutdown --force rc=1\n" in run.log
    assert "TIMEOUT 3s reached (1/6 still up) — halting NAS anyway" in run.log
    assert len(run.halts) == 1


# ── MS-A2 reachable ──────────────────────────────────────────────────────────
def test_msa2_reachable_is_shut_down_with_force_and_waited_for(tmp_path: Path, bash: str) -> None:
    nodes = q170s1_live()
    nodes[MSA2_DEV_BUILD] = Node(MSA2_DEV, delay=3)  # slower than every Q170S1 node
    run = run_orchestrator(tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_DEV))
    assert_clean_finish(run)

    assert run.calls_for(MSA2_DEV_BUILD) == [msa2_argv(run.talos_dir, MSA2_DEV, MSA2_DEV_BUILD)]
    assert f"] msa2-dev {MSA2_DEV_BUILD} shutdown --force rc=0\n" in run.log
    assert "polling apid on 7 node(s)" in run.log
    assert MSA2_DEV_BUILD in run.probes
    # the NAS halted only after the msa2 box was down
    down_at = int((run.state / "down_at" / MSA2_DEV_BUILD).read_text())
    halted_at = int(run.halts[0].split()[0])
    assert halted_at >= down_at, (halted_at, down_at)
    # its FINAL address is kub-prd-02 today: another CA, so rejected, and that
    # rejection must not add a poll entry (.12 is polled once, via Q170S1)
    assert f"] msa2-dev {MSA2_DEV_FINAL} shutdown --force rc=1\n" in run.log
    assert f"msa2-dev {MSA2_DEV_FINAL} did not accept (a FINAL address: rule (d)" in run.log
    assert MSA2_DEV_FINAL in run.probes


# ── MS-A2 unreachable / not built / maintenance mode ─────────────────────────
def test_msa2_unbuilt_and_maintenance_mode_never_burn_the_backstop(
    tmp_path: Path, bash: str
) -> None:
    """msa2-prd not built (no box at .17); msa2-dev in Talos maintenance mode at
    .18 — its :50000 answers the unauthenticated probe FOREVER. Both configs are
    staged. Before this change's rule (a), .18 in the poll would run to TIMEOUT."""
    nodes = q170s1_live()
    nodes[MSA2_DEV_BUILD] = Node(MAINTENANCE)  # up, answers :50000, accepts nothing
    run = run_orchestrator(tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_PRD, MSA2_DEV), timeout=8)
    assert_clean_finish(run)
    assert run.elapsed < 8, f"took {run.elapsed:.1f}s — waited on an unaccepted node"
    for cl, ip in (("msa2-prd", MSA2_PRD_BUILD), ("msa2-dev", MSA2_DEV_BUILD)):
        assert f"] {cl} {ip} shutdown --force rc=1\n" in run.log
        assert f"{cl} {ip} did not accept — NOT polled" in run.log
    assert MSA2_DEV_BUILD not in run.probes
    assert MSA2_PRD_BUILD not in run.probes
    assert "polling apid on 6 node(s)" in run.log


def _log_time(line: str) -> int:
    """Epoch seconds of a `[%F %T] …` log line."""
    stamp = line[1:20]
    return int(time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")))


def test_msa2_call_that_hangs_delays_the_poll_by_at_most_the_cap(tmp_path: Path, bash: str) -> None:
    """An address that swallows packets is BOUNDED, not free: the poll (and so the
    NAS halt) starts only after every call has returned, so a hung msa2 call
    delays it by up to the cap — never longer."""
    cap = 2
    nodes = q170s1_live()
    nodes[MSA2_PRD_BUILD] = Node(MSA2_PRD, hang=True)
    run = run_orchestrator(
        tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_PRD), msa2_call_timeout=cap
    )
    assert_clean_finish(run)
    assert run.elapsed < 15, f"took {run.elapsed:.1f}s — the hung call was not capped"
    assert f"] msa2-prd {MSA2_PRD_BUILD} shutdown --force rc=124\n" in run.log
    assert f"{MSA2_PRD_BUILD} did not accept — NOT polled" in run.log
    assert all(w.startswith(f"--kill-after=5 {cap} ") for w in run.lines("timeout.log"))
    lines = run.log.splitlines()
    t_start = _log_time(next(ln for ln in lines if "orchestrator START" in ln))
    t_poll = _log_time(next(ln for ln in lines if "until down:" in ln))
    # the Q170S1 calls return at once here, so the gap is the hung call's cap
    assert cap - 1 <= t_poll - t_start <= cap + 2, (t_start, t_poll)


def test_msa2_call_cap_production_default_is_60s(tmp_path: Path, bash: str) -> None:
    """Every other test overrides the cap through the seam; this one leaves it
    unset, so an edit to the script's default turns it red."""
    nodes = q170s1_live()
    nodes[MSA2_DEV_BUILD] = Node(MSA2_DEV)
    run = run_orchestrator(
        tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_PRD, MSA2_DEV), msa2_call_timeout=None
    )
    assert_clean_finish(run)
    wrapped = run.lines("timeout.log")
    assert len(wrapped) == 4, wrapped
    assert all(w.startswith("--kill-after=5 60 ") for w in wrapped), wrapped


def test_production_defaults_are_pinned() -> None:
    """The UPS_ORCH_* seams replace these in every run, so pin the literals the
    NAS actually runs with."""
    text = ORCH.read_text()
    for literal in (
        "UPS_ORCH_TALOS_DIR:-/mnt/tank/system/talos}",
        "UPS_ORCH_HALT:-/sbin/shutdown}",
        "UPS_ORCH_LOG:-/mnt/tank/system/nut/last-node-shutdown.log}",
        "UPS_ORCH_TIMEOUT:-300}",
        "UPS_ORCH_POLL:-10}",
        "UPS_ORCH_MSA2_CALL_TIMEOUT:-60}",
    ):
        assert text.count(literal) == 1, literal


# ── the address moves: no edit needed at cutover or rollback ─────────────────
def test_after_prd_cutover_no_edit_needed(tmp_path: Path, bash: str) -> None:
    """msa2-prd has taken .11; the old prd nodes' cords are pulled; kub-dev is
    still live and msa2-dev is still on its build address."""
    nodes = {ip: Node(DEV) for ip in Q170S1_DEV}
    nodes[MSA2_PRD_FINAL] = Node(MSA2_PRD, delay=2)
    nodes[MSA2_DEV_BUILD] = Node(MSA2_DEV)
    run = run_orchestrator(tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_PRD, MSA2_DEV))
    assert_clean_finish(run)
    assert "] prd 10.10.5.11 shutdown --force rc=1\n" in run.log  # old CA rejected
    assert f"] msa2-prd {MSA2_PRD_FINAL} shutdown --force rc=0\n" in run.log
    # .11 is in both lists but polled once. .12 (empty, and no kub-prd node
    # accepted) drops out by rule (d): .14-.16 + .13 + .11 + .18
    assert "10.10.5.12 NOT polled — no prd node accepted" in run.log
    assert "polling apid on 6 node(s)" in run.log
    assert run.log.count("until down:") == 1
    polled = run.log.split("until down: ", 1)[1].split("\n", 1)[0].split()
    assert sorted(polled) == sorted(set(polled)), polled


# ── rule (d): a FINAL address, .11/.12 ──────────────────────────────────────
def _post_prd_cutover_maintenance() -> tuple[dict[str, Node], tuple[str, ...]]:
    """E1: msa2-prd at .11 in Talos maintenance mode (a reinstall), config staged."""
    nodes = {ip: Node(DEV) for ip in Q170S1_DEV}
    nodes[MSA2_PRD_FINAL] = Node(MAINTENANCE)
    nodes[MSA2_DEV_BUILD] = Node(MSA2_DEV)
    return nodes, (PRD, DEV, MSA2_PRD, MSA2_DEV)


def _post_prd_cutover_unstaged() -> tuple[dict[str, Node], tuple[str, ...]]:
    """E2: msa2-prd installed at .11, but its config was never staged."""
    nodes = {ip: Node(DEV) for ip in Q170S1_DEV}
    nodes[MSA2_PRD_FINAL] = Node(MSA2_PRD)
    return nodes, (PRD, DEV, MSA2_DEV)


def _post_both_cutovers_maintenance() -> tuple[dict[str, Node], tuple[str, ...]]:
    """E3: both cut over, PRD_NODES not yet deleted; msa2-dev at .12 in
    maintenance mode."""
    nodes = {MSA2_PRD_FINAL: Node(MSA2_PRD), MSA2_DEV_FINAL: Node(MAINTENANCE)}
    return nodes, (PRD, DEV, MSA2_PRD, MSA2_DEV)


@pytest.mark.parametrize(
    ("scenario", "final"),
    [
        (_post_prd_cutover_maintenance, MSA2_PRD_FINAL),
        (_post_prd_cutover_unstaged, MSA2_PRD_FINAL),
        (_post_both_cutovers_maintenance, MSA2_DEV_FINAL),
    ],
    ids=["E1-prd-cutover-maintenance", "E2-prd-cutover-unstaged", "E3-both-maintenance"],
)
def test_final_address_that_does_not_accept_never_burns_the_backstop_once_kub_prd_is_gone(
    tmp_path: Path, bash: str, scenario, final: str
) -> None:
    """Rule (a) alone covered only the BUILD addresses: .11/.12 are also in the
    Q170S1 prd list, which is polled whatever the rc, so before rule (d) each of
    these ran to TIMEOUT. With kub-prd's cords pulled, no prd call is accepted,
    and a FINAL address nothing accepted is not waited for."""
    nodes, staged = scenario()
    run = run_orchestrator(tmp_path, bash, nodes, staged=staged, timeout=8)
    assert_clean_finish(run)
    assert run.elapsed < 8, f"took {run.elapsed:.1f}s — waited on an unaccepted FINAL address"
    assert f"{final} NOT polled — no prd node accepted" in run.log
    assert final not in run.probes


def test_final_address_rejecting_while_kub_prd_is_live_is_still_polled(
    tmp_path: Path, bash: str
) -> None:
    """Rule (d) leaves the live Q170S1 path alone: kub-prd-01 at .11 rejects (a
    rotated PKI), but .12/.13 accepted, so kub-prd is live and .11 is polled
    whatever its rc — to the backstop, exactly as before this change."""
    nodes = q170s1_live()
    nodes[MSA2_PRD_FINAL] = Node("rotated-pki")
    run = run_orchestrator(tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_PRD, MSA2_DEV), timeout=3)
    assert run.proc.returncode == 0, run.proc.stderr
    assert "] prd 10.10.5.11 shutdown --force rc=1\n" in run.log
    assert "10.10.5.11 still polled — a prd node accepted" in run.log
    assert "TIMEOUT 3s reached (1/6 still up) — halting NAS anyway" in run.log
    assert len(run.halts) == 1


def test_every_kub_prd_call_rejected_still_burns_the_backstop_via_13(
    tmp_path: Path, bash: str
) -> None:
    """Rule (d)'s one change to the Q170S1 path, first case: EVERY kub-prd call
    fails (a broken prd config). .11/.12 are no longer waited for — but .13,
    which no MS-A2 list names, still is, so the backstop still burns, as it did
    before."""
    nodes = q170s1_live()
    for ip in Q170S1_PRD:
        nodes[ip] = Node("rotated-pki")
    run = run_orchestrator(tmp_path, bash, nodes, timeout=3)
    assert run.proc.returncode == 0, run.proc.stderr
    for ip in ("10.10.5.11", "10.10.5.12"):
        assert f"{ip} NOT polled — no prd node accepted" in run.log
    assert "10.10.5.13" in run.probes
    assert "TIMEOUT 3s reached (1/4 still up) — halting NAS anyway" in run.log


def test_tracker_error_on_every_kub_prd_call_stops_waiting_for_11_and_12(
    tmp_path: Path, bash: str
) -> None:
    """Rule (d)'s one change to the Q170S1 path, second case — the real trade-off,
    pinned so it stays visible: all three kub-prd shutdowns are DELIVERED but
    every call still exits 1 (the default --wait tracker failing after the RPC).
    Then kub-prd reads as gone, and the NAS no longer waits for .11/.12 — here
    it halts while .11 is still going down. Only .13 (and kub-dev) gate it."""
    nodes = q170s1_live()
    for ip in Q170S1_PRD:
        nodes[ip] = Node(PRD, tracker_error=True)
    nodes["10.10.5.11"].delay = 4
    run = run_orchestrator(tmp_path, bash, nodes)
    assert_clean_finish(run)
    assert "10.10.5.11 NOT polled — no prd node accepted" in run.log
    down_at = int((run.state / "down_at" / "10.10.5.11").read_text())
    halted_at = int(run.halts[0].split()[0])
    assert halted_at < down_at, (halted_at, down_at)


def test_after_both_cutovers_no_edit_needed(tmp_path: Path, bash: str) -> None:
    nodes = {
        MSA2_PRD_FINAL: Node(MSA2_PRD, delay=1),
        MSA2_DEV_FINAL: Node(MSA2_DEV, delay=1),
    }
    run = run_orchestrator(tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_PRD, MSA2_DEV))
    assert_clean_finish(run)
    assert "polling apid on 6 node(s)" in run.log  # both finals already in Q170S1's list
    for cl, ip in (("msa2-prd", MSA2_PRD_FINAL), ("msa2-dev", MSA2_DEV_FINAL)):
        assert f"] {cl} {ip} shutdown --force rc=0\n" in run.log


def test_prd_rollback_no_edit_needed(tmp_path: Path, bash: str) -> None:
    """Rollback: msa2-prd powered off, the old prd nodes plugged back in."""
    nodes = q170s1_live()
    nodes[MSA2_DEV_BUILD] = Node(MSA2_DEV)
    run = run_orchestrator(tmp_path, bash, nodes, staged=(PRD, DEV, MSA2_PRD, MSA2_DEV), timeout=8)
    assert_clean_finish(run)
    assert "] prd 10.10.5.11 shutdown --force rc=0\n" in run.log
    assert f"] msa2-prd {MSA2_PRD_FINAL} shutdown --force rc=1\n" in run.log


# ── the pre-existing FATAL path ──────────────────────────────────────────────
def test_missing_talosctl_still_halts_the_nas(tmp_path: Path, bash: str) -> None:
    run = run_orchestrator(tmp_path, bash, q170s1_live(), talosctl=False)
    assert run.proc.returncode == 0
    assert "FATAL: talosctl not found" in run.log
    assert run.calls == []
    assert len(run.halts) == 1


# ── setup-talos-shutdown-orchestrator.sh --print-checks ──────────────────────
def test_print_checks_covers_every_pair_and_its_quoting_survives_the_nas_shell(
    tmp_path: Path,
) -> None:
    """--print-checks derives its pairs from the orchestrator's own inventory and
    prints ONE ssh command. Replay that command's remote half through a login
    shell, as the NAS would (minus sudo), against a stub talosctl."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TALOS_NAS_")}
    env.pop("TRUENAS_HOST", None)
    out = subprocess.run(
        [str(SETUP), "--print-checks"], env=env, capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    ssh_lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip().startswith("ssh ")]
    assert len(ssh_lines) == 1, out.stdout
    argv = shlex.split(ssh_lines[0])
    assert argv[:3] == ["ssh", "-t", "truenas_admin@nas.w1.lv"]
    remote = argv[3]
    assert remote.startswith("sudo bash -c ")

    talos = tmp_path / "talos"
    talos.mkdir()
    record = tmp_path / "record.log"
    (talos / "talosctl").write_text(f'#!/bin/bash\necho "$*" >> {record}\necho Server: ok\n')
    (talos / "talosctl").chmod(0o755)
    for cfg in (PRD, DEV, MSA2_DEV):  # msa2-prd deliberately NOT staged
        (talos / cfg).write_text("context: stub\n")
    # the staged orchestrator: a byte copy of this checkout's
    (talos / "nas-ups-orchestrator.sh").write_bytes(ORCH.read_bytes())
    replay = remote.removeprefix("sudo ").replace("/mnt/tank/system/talos", str(talos))
    replay_env = dict(os.environ)
    if shutil.which("sha256sum") is None:  # the NAS has GNU sha256sum; stub it here
        stub = tmp_path / "bin"
        stub.mkdir()
        (stub / "sha256sum").write_text('#!/bin/bash\nexec shasum -a 256 "$@"\n')
        (stub / "sha256sum").chmod(0o755)
        replay_env["PATH"] = f"{stub}:{replay_env['PATH']}"
    res = subprocess.run(
        ["bash", "-c", replay], capture_output=True, text=True, timeout=30, env=replay_env
    )
    assert res.returncode == 0, res.stderr

    # the staged script's hash comes first, and print-checks names the value it
    # must have: this checkout's
    want = hashlib.sha256(ORCH.read_bytes()).hexdigest()
    printed = out.stdout.split("which must be", 1)[1].split()[0]
    assert printed == want, (printed, want)
    assert res.stdout.split()[0] == want, res.stdout[:200]

    calls = record.read_text().splitlines()
    assert calls[0] == "version --client --short"
    expected = (
        {(PRD, ip) for ip in Q170S1_PRD}
        | {(DEV, ip) for ip in Q170S1_DEV}
        | {(MSA2_DEV, MSA2_DEV_BUILD), (MSA2_DEV, MSA2_DEV_FINAL)}
    )
    got = set()
    for c in calls[1:]:
        parts = c.split()
        assert parts[0] == "--talosconfig" and parts[-2:] == ["version", "--short"], c
        got.add((Path(parts[1]).name, parts[3]))
        assert parts[3] == parts[5], c  # -n and -e name the same address
    assert got == expected
    for ip in (MSA2_PRD_BUILD, MSA2_PRD_FINAL):
        assert f"== {MSA2_PRD} @ {ip}\nnot staged" in res.stdout
