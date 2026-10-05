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
  * `hang` makes the call block until something kills it.

Since the Q170S1 teardown (kube-infra#1443) the rack is the two MS-A2 boxes at
their FINAL addresses, and every call follows the script's rules (a)-(c): one
bounded `--wait=false` call per box, and only an accepted shutdown is waited
for. The tests also pin what the teardown removed: no Q170S1 address, no BUILD
address and no Q170S1 config is ever used, even with the old configs still on
the NAS.
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

MSA2_PRD = "msa2-prd-shutdown.talosconfig"
MSA2_DEV = "msa2-dev-shutdown.talosconfig"
# The Q170S1 configs: still on the NAS after the teardown (the setup script
# never deletes a file), never used again.
OLD_PRD = "prd-shutdown.talosconfig"
OLD_DEV = "dev-shutdown.talosconfig"
MAINTENANCE = "maintenance-mode-self-signed"  # matches no staged config
ROTATED = "rotated-pki"  # a rebuild with NEW_PKI=1 and no re-stage since
# An old config by name. Not a plain substring: `msa2-prd-shutdown.talosconfig`
# CONTAINS `prd-shutdown.talosconfig`.
OLD_CFG = re.compile(r"(?<![\w-])(?:prd|dev)-shutdown\.talosconfig")

MSA2_PRD_IP = "10.10.5.11"
MSA2_DEV_IP = "10.10.5.12"
# Addresses the teardown took out of the inventory: kub-prd-03 and kub-dev-01..03,
# and the two boxes' BUILD addresses.
RETIRED = ["10.10.5.13", "10.10.5.14", "10.10.5.15", "10.10.5.16", "10.10.5.17", "10.10.5.18"]


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


def msa2_live(delay: int = 0) -> dict[str, Node]:
    return {
        MSA2_PRD_IP: Node(MSA2_PRD, delay=delay),
        MSA2_DEV_IP: Node(MSA2_DEV, delay=delay),
    }


def run_orchestrator(
    tmp_path: Path,
    bash: str,
    nodes: dict[str, Node],
    staged: tuple[str, ...] = (MSA2_PRD, MSA2_DEV),
    *,
    talosctl: bool = True,
    timeout: int = 20,
    poll: int = 1,
    call_timeout: int | None = 60,
) -> Run:
    """call_timeout=None leaves UPS_ORCH_CALL_TIMEOUT unset, so the script's own
    production default applies."""
    state, talos_dir, bindir = tmp_path / "state", tmp_path / "talos", tmp_path / "bin"
    for d in (
        state,
        talos_dir,
        bindir,
        *(state / s for s in ("pki", "up", "delay", "hang", "down_at")),
    ):
        d.mkdir(parents=True, exist_ok=True)
    for ip, n in nodes.items():
        (state / "pki" / ip).write_text(n.pki)
        if n.up:
            (state / "up" / ip).write_text("")
        (state / "delay" / ip).write_text(str(n.delay))
        if n.hang:
            (state / "hang" / ip).write_text("")
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
    env.pop("UPS_ORCH_MSA2_CALL_TIMEOUT", None)  # the seam's name before the teardown
    if call_timeout is None:
        env.pop("UPS_ORCH_CALL_TIMEOUT", None)
    else:
        env["UPS_ORCH_CALL_TIMEOUT"] = str(call_timeout)
    t0 = time.monotonic()
    proc = subprocess.run([bash, str(ORCH)], env=env, capture_output=True, text=True, timeout=120)
    elapsed = time.monotonic() - t0
    run = Run(proc, state, talos_dir, log.read_text() if log.exists() else "", elapsed)
    run.calls = run.lines("calls.log")
    return run


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


def polled_set(run: Run) -> list[str]:
    assert run.log.count("until down:") == 1, run.log
    return run.log.split("until down:", 1)[1].split("\n", 1)[0].split()


# ── both boxes up: the normal outage ─────────────────────────────────────────
def test_both_boxes_are_shut_down_with_force_and_waited_for(tmp_path: Path, bash: str) -> None:
    run = run_orchestrator(tmp_path, bash, msa2_live(delay=2))
    assert_clean_finish(run)
    assert sorted(run.calls) == sorted(
        [
            msa2_argv(run.talos_dir, MSA2_PRD, MSA2_PRD_IP),
            msa2_argv(run.talos_dir, MSA2_DEV, MSA2_DEV_IP),
        ]
    )
    for cl, ip in (("msa2-prd", MSA2_PRD_IP), ("msa2-dev", MSA2_DEV_IP)):
        assert f"] {cl} {ip} shutdown --force rc=0\n" in run.log
    assert "polling apid on 2 node(s)" in run.log
    assert sorted(polled_set(run)) == [MSA2_PRD_IP, MSA2_DEV_IP]
    # the NAS halted only after both boxes were down
    halted_at = int(run.halts[0].split()[0])
    for ip in (MSA2_PRD_IP, MSA2_DEV_IP):
        assert halted_at >= int((run.state / "down_at" / ip).read_text()), ip
    assert "no node accepted" not in run.log
    assert "NOT shut down" not in run.log


def test_every_call_is_bounded_and_waitless(tmp_path: Path, bash: str) -> None:
    """Rules (b) and (c): every call is `--wait=false` and wrapped in timeout."""
    run = run_orchestrator(tmp_path, bash, msa2_live())
    assert_clean_finish(run)
    wrapped = run.lines("timeout.log")
    assert len(wrapped) == 2, wrapped
    assert all("shutdown --force --wait=false" in w for w in wrapped), wrapped


# ── the teardown: nothing of the Q170S1 estate is targeted any more ───────────
def test_no_retired_address_or_q170s1_config_is_ever_used(tmp_path: Path, bash: str) -> None:
    """The NAS after the teardown: the old `{dev,prd}-shutdown.talosconfig` are
    still staged (nothing deletes them), and the retired addresses still answer.
    Neither may be touched: a call to .13-.18 or with an old config would mean
    the teardown left a list behind."""
    nodes = msa2_live()
    for ip in RETIRED:
        nodes[ip] = Node(OLD_DEV)  # something answers there, with an old CA
    run = run_orchestrator(tmp_path, bash, nodes, staged=(MSA2_PRD, MSA2_DEV, OLD_PRD, OLD_DEV))
    assert_clean_finish(run)
    assert len(run.calls) == 2, run.calls
    for ip in RETIRED:
        assert run.calls_for(ip) == [], ip
        assert ip not in run.probes, ip
    assert not any(OLD_CFG.search(c) for c in run.calls), run.calls


def test_inventory_names_no_retired_address() -> None:
    """Static pin of the teardown: no code line of the orchestrator names a
    Q170S1 node or a BUILD address, and no Q170S1 list or config is left."""
    code = "\n".join(
        ln for ln in ORCH.read_text().splitlines() if not ln.lstrip().startswith("#")
    )
    for ip in RETIRED:
        assert ip not in code, ip
    for name in ("DEV_NODES=", "PRD_NODES=", "Q170S1_NODES", "DEV_CFG=$", "PRD_CFG=$",
                 "poll_q170s1"):
        assert re.search(rf"(^|[^A-Z_]){re.escape(name)}", code) is None, name
    assert OLD_CFG.search(code) is None, OLD_CFG.search(code)
    assert 'MSA2_PRD_NODES="10.10.5.11"' in code
    assert 'MSA2_DEV_NODES="10.10.5.12"' in code


def test_nas_waits_for_the_slowest_accepted_box(tmp_path: Path, bash: str) -> None:
    """The NAS halts only once EVERY accepted box is down, not the first. The
    two boxes stop at different times, so "wait for all" and "wait for any"
    give different answers."""
    nodes = {MSA2_PRD_IP: Node(MSA2_PRD, delay=1), MSA2_DEV_IP: Node(MSA2_DEV, delay=4)}
    run = run_orchestrator(tmp_path, bash, nodes)
    assert_clean_finish(run)
    last = max(int((run.state / "down_at" / ip).read_text()) for ip in nodes)
    assert int(run.halts[0].split()[0]) >= last


def test_accepted_box_that_never_stops_answering_hits_the_backstop(
    tmp_path: Path, bash: str
) -> None:
    """A box that accepts its shutdown but keeps answering :50000 must not hold
    the NAS awake for ever: at the backstop the NAS halts anyway (and arms the
    UPS kill-power), with the box still counted as up."""
    nodes = msa2_live()
    nodes[MSA2_DEV_IP] = Node(MSA2_DEV, delay=3600)  # accepted, answers for ever
    run = run_orchestrator(tmp_path, bash, nodes, timeout=3)
    assert run.proc.returncode == 0, run.proc.stderr
    assert "TIMEOUT 3s reached (1/2 still up) — halting NAS anyway" in run.log, run.log
    assert len(run.halts) == 1 and run.halts[0].endswith(" -P now"), run.halts
    assert 2 <= run.elapsed < 15, run.elapsed


# ── a box that does not accept is never waited for (rule a) ───────────────────
@pytest.mark.parametrize(
    ("pki", "why"),
    [(MAINTENANCE, "maintenance mode"), (ROTATED, "a rotated PKI")],
    ids=["maintenance-mode", "rotated-pki"],
)
def test_box_that_does_not_accept_is_not_polled(
    tmp_path: Path, bash: str, pki: str, why: str
) -> None:
    """msa2-dev answers the unauthenticated :50000 probe FOR EVER but accepts
    nothing ({why}). Polling it would run to the backstop; rule (a) keeps it out
    and the log says the box was NOT shut down."""
    nodes = msa2_live(delay=1)
    nodes[MSA2_DEV_IP] = Node(pki)
    run = run_orchestrator(tmp_path, bash, nodes, timeout=8)
    assert_clean_finish(run)
    assert run.elapsed < 8, f"took {run.elapsed:.1f}s — waited on an unaccepted box ({why})"
    assert f"] msa2-dev {MSA2_DEV_IP} shutdown --force rc=1\n" in run.log
    assert f"msa2-dev {MSA2_DEV_IP} did not accept — NOT polled, ⚠ NOT shut down" in run.log
    assert MSA2_DEV_IP not in run.probes
    assert polled_set(run) == [MSA2_PRD_IP]


def test_absent_box_fails_fast_and_is_not_polled(tmp_path: Path, bash: str) -> None:
    """A box with its cord out: no route, rc 1, nothing to wait for."""
    nodes = {MSA2_PRD_IP: Node(MSA2_PRD)}
    run = run_orchestrator(tmp_path, bash, nodes, timeout=8)
    assert_clean_finish(run)
    assert f"] msa2-dev {MSA2_DEV_IP} shutdown --force rc=1\n" in run.log
    assert MSA2_DEV_IP not in run.probes


def test_no_box_accepts_logs_it_and_halts_at_once(tmp_path: Path, bash: str) -> None:
    """Both boxes reject (say, both rebuilt with a new PKI and no re-stage).
    Nothing this script does would bring them down, so it halts the NAS at
    once rather than burning the backstop, and says so in the log."""
    nodes = {MSA2_PRD_IP: Node(ROTATED), MSA2_DEV_IP: Node(MAINTENANCE)}
    run = run_orchestrator(tmp_path, bash, nodes, timeout=8)
    assert_clean_finish(run)
    assert run.elapsed < 8, f"took {run.elapsed:.1f}s"
    assert "⚠ no node accepted its shutdown — nothing to wait for" in run.log
    assert "polling apid on 0 node(s)" in run.log
    assert "all nodes down at 0s" in run.log
    assert run.probes == set()


# ── a config that is not staged ───────────────────────────────────────────────
def test_unstaged_cluster_is_skipped_and_logged_as_not_shut_down(
    tmp_path: Path, bash: str
) -> None:
    run = run_orchestrator(tmp_path, bash, msa2_live(), staged=(MSA2_PRD,))
    assert_clean_finish(run)
    assert run.calls_for(MSA2_DEV_IP) == []
    assert "msa2-dev: no talosconfig staged at" in run.log
    assert "skipped, NOT shut down" in run.log
    assert polled_set(run) == [MSA2_PRD_IP]


def test_nothing_staged_still_halts_the_nas(tmp_path: Path, bash: str) -> None:
    """No config staged at all leaves the call list EMPTY. Under `set -u`,
    bash < 4.4 (macOS's /bin/bash 3.2) aborts on "${pids[@]}" of an empty array
    — before the NAS halt. The orchestrator must still halt the NAS."""
    run = run_orchestrator(tmp_path, bash, msa2_live(), staged=())
    assert_clean_finish(run)
    assert "unbound variable" not in run.proc.stderr, run.proc.stderr
    assert run.calls == []
    assert "⚠ no node accepted its shutdown" in run.log


# ── the cap (rule c) ──────────────────────────────────────────────────────────
def _log_time(line: str) -> int:
    """Epoch seconds of a `[%F %T] …` log line."""
    stamp = line[1:20]
    return int(time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")))


def test_call_that_hangs_delays_the_poll_by_at_most_the_cap(tmp_path: Path, bash: str) -> None:
    """An address that swallows packets is BOUNDED, not free: the poll (and so the
    NAS halt) starts only after every call has returned, so a hung call delays it
    by up to the cap — never longer."""
    cap = 2
    nodes = msa2_live()
    nodes[MSA2_PRD_IP] = Node(MSA2_PRD, hang=True)
    run = run_orchestrator(tmp_path, bash, nodes, call_timeout=cap)
    assert_clean_finish(run)
    assert run.elapsed < 15, f"took {run.elapsed:.1f}s — the hung call was not capped"
    assert f"] msa2-prd {MSA2_PRD_IP} shutdown --force rc=124\n" in run.log
    assert f"{MSA2_PRD_IP} did not accept — NOT polled" in run.log
    assert all(w.startswith(f"--kill-after=5 {cap} ") for w in run.lines("timeout.log"))
    lines = run.log.splitlines()
    t_start = _log_time(next(ln for ln in lines if "orchestrator START" in ln))
    t_poll = _log_time(next(ln for ln in lines if "until down:" in ln))
    # the other call returns at once here, so the gap is the hung call's cap
    assert cap - 1 <= t_poll - t_start <= cap + 2, (t_start, t_poll)


def test_call_cap_production_default_is_60s(tmp_path: Path, bash: str) -> None:
    """Every other test overrides the cap through the seam; this one leaves it
    unset, so an edit to the script's default turns it red."""
    run = run_orchestrator(tmp_path, bash, msa2_live(), call_timeout=None)
    assert_clean_finish(run)
    wrapped = run.lines("timeout.log")
    assert len(wrapped) == 2, wrapped
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
        "UPS_ORCH_CALL_TIMEOUT:-60}",
    ):
        assert text.count(literal) == 1, literal


# ── the pre-existing FATAL path ──────────────────────────────────────────────
def test_missing_talosctl_still_halts_the_nas(tmp_path: Path, bash: str) -> None:
    run = run_orchestrator(tmp_path, bash, msa2_live(), talosctl=False)
    assert run.proc.returncode == 0
    assert "FATAL: talosctl not found" in run.log
    assert run.calls == []
    assert len(run.halts) == 1


# ── setup-talos-shutdown-orchestrator.sh ──────────────────────────────────────
def test_setup_requires_both_msa2_keys_and_no_q170s1_key(tmp_path: Path) -> None:
    """Both MS-A2 configs are required since the teardown: a missing key stops
    the run before anything is downloaded or uploaded. The Q170S1 keys are no
    longer read at all."""
    text = SETUP.read_text()
    for key in ("TALOS_NAS_SHUTDOWN_CONFIG_DEV", "TALOS_NAS_SHUTDOWN_CONFIG_PRD"):
        assert re.search(rf"\b{key}\b", text) is None, key
    base = {k: v for k, v in os.environ.items() if not k.startswith("TALOS_NAS_")}
    base.update(TRUENAS_HOST="nas.invalid", TRUENAS_API_KEY="stub")
    for missing in ("TALOS_NAS_SHUTDOWN_CONFIG_MSA2_DEV", "TALOS_NAS_SHUTDOWN_CONFIG_MSA2_PRD"):
        env = dict(base)
        env["TALOS_NAS_SHUTDOWN_CONFIG_MSA2_DEV"] = "c3R1Yg=="
        env["TALOS_NAS_SHUTDOWN_CONFIG_MSA2_PRD"] = "c3R1Yg=="
        env.pop(missing)
        out = subprocess.run(
            [str(SETUP)], env=env, capture_output=True, text=True, timeout=30, cwd=tmp_path
        )
        assert out.returncode != 0, out.stdout
        assert f"set {missing} (base64)" in out.stderr, out.stderr
        assert "downloading talosctl" not in out.stdout, out.stdout


def test_setup_stages_exactly_the_configs_the_orchestrator_reads() -> None:
    """The setup script names the config files it stages (`CFGS=`) separately
    from the orchestrator's `*_CFG=` lines. A rename on one side alone would
    stage a file the orchestrator never reads, and the orchestrator would skip
    that box. Hold the two lists equal."""
    orch = re.findall(r"^MSA2_(?:DEV|PRD)_CFG=\$TALOS_DIR/(\S+)$", ORCH.read_text(), re.M)
    m = re.search(r'^CFGS="([^"]*)"$', SETUP.read_text(), re.M)
    assert m, "no CFGS= line in the setup script"
    assert len(orch) == 2, orch
    assert sorted(m.group(1).split()) == sorted(orch)
    # and the setup script writes each of them from Doppler
    for cfg in orch:
        assert f'> "$WORK/{cfg}"' in SETUP.read_text(), cfg


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
    for cfg in (MSA2_DEV, OLD_PRD, OLD_DEV):  # msa2-prd deliberately NOT staged
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
    got = set()
    for c in calls[1:]:
        parts = c.split()
        assert parts[0] == "--talosconfig" and parts[-2:] == ["version", "--short"], c
        got.add((Path(parts[1]).name, parts[3]))
        assert parts[3] == parts[5], c  # -n and -e name the same address
    # exactly the inventory: one pair per box, the old configs never checked
    assert got == {(MSA2_DEV, MSA2_DEV_IP)}
    assert f"== {MSA2_PRD} @ {MSA2_PRD_IP}\nnot staged" in res.stdout
    assert OLD_CFG.search(res.stdout) is None, res.stdout
