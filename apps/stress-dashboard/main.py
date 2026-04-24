"""FastAPI dashboard for hw-validation JSON reports.

Reads report.json files (schema per hw-validation/scripts/lib/report.sh)
from /data (bind-mounted stress-results dataset) and renders:

  GET  /                          overview table (all reports, all nodes)
  GET  /matrix                    fleet pass/fail matrix (node × profile)
  GET  /report/{filename}         per-report detail: metrics, telemetry, samples
  POST /report/{filename}/delete  unlink + redirect to /
  GET  /api/reports               JSON list (filename, hostname, profile, rc...)
  GET  /api/report/{filename}     raw report JSON passthrough
  GET  /healthz                   { "status": "ok", "reports": N }

No auth — mgmt VLAN isolation (matches amtctl posture). Reports directory
is mounted RW so deletes work without needing a TrueNAS API round-trip.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

log = logging.getLogger("stress-dashboard")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

DATA_DIR = Path(os.environ.get("STRESS_RESULTS_DIR", "/data"))
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="stress-dashboard")


# ── Report loading ──────────────────────────────────────────────────────────


def _list_report_files() -> list[Path]:
    if not DATA_DIR.exists():
        return []
    return sorted(DATA_DIR.glob("*.json"), reverse=True)


def _load_report(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("failed to read %s: %s", path, e)
        return None


def _summarise(path: Path, doc: dict[str, Any]) -> dict[str, Any]:
    """Flatten one report into table-row fields for the overview."""
    tests = doc.get("tests") or []
    rcs = [t.get("rc") for t in tests if isinstance(t, dict) and "rc" in t]
    pass_ct = sum(1 for r in rcs if r == 0)
    fail_ct = sum(1 for r in rcs if r != 0)
    started = doc.get("started_at") or ""
    ended = doc.get("ended_at") or ""
    duration_s = None
    try:
        from datetime import datetime

        if started and ended:
            a = datetime.fromisoformat(started.replace("Z", "+00:00"))
            b = datetime.fromisoformat(ended.replace("Z", "+00:00"))
            duration_s = int((b - a).total_seconds())
    except Exception:  # noqa: BLE001
        pass

    # Component list in the order they were executed — pulled from test
    # record names, nic_i226v stripped (legacy pre-Phase-D reports still
    # carry it; new reports don't emit it at all).
    components: list[str] = []
    destructive = False
    for t in tests:
        if not isinstance(t, dict):
            continue
        n = t.get("name")
        if not n or n == "nic_i226v":
            continue
        if n not in components:
            components.append(n)
        # Destructive runs produce `fio_write_*_*` metrics.
        for k in t:
            if isinstance(k, str) and k.startswith("fio_write_"):
                destructive = True
                break

    return {
        "filename": path.name,
        "hostname": doc.get("hostname", "?"),
        "profile": doc.get("profile", "?"),
        "started_at": started,
        "ended_at": ended,
        "duration_s": duration_s,
        "test_count": len([t for t in tests
                           if isinstance(t, dict) and t.get("name") != "nic_i226v"]),
        "pass_count": pass_ct,
        "fail_count": fail_ct,
        "overall_pass": fail_ct == 0 and pass_ct > 0,
        "components": components,
        "destructive": destructive,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


def _all_summaries() -> list[dict[str, Any]]:
    """All report summaries, newest-first by started_at (falls back to
    mtime if started_at missing or identical — e.g. same-second reboots).
    """
    out: list[dict[str, Any]] = []
    for p in _list_report_files():
        doc = _load_report(p)
        if not doc:
            continue
        s = _summarise(p, doc)
        # Stash mtime as a tiebreaker / fallback sort key (not rendered).
        try:
            s["_mtime"] = p.stat().st_mtime
        except OSError:
            s["_mtime"] = 0.0
        out.append(s)
    out.sort(key=lambda s: (s.get("started_at") or "", s.get("_mtime", 0.0)),
             reverse=True)
    return out


def _group_by_host(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse per-report summaries into per-hostname groups. Each group
    keeps its reports in the input order (caller passes newest-first).
    Groups themselves are ordered by their newest report's started_at,
    so the host that ran most recently bubbles to the top.
    """
    groups: dict[str, dict[str, Any]] = {}
    for s in summaries:
        host = s["hostname"]
        g = groups.setdefault(host, {
            "hostname": host,
            "reports": [],
            "latest_started_at": "",
            "pass_count": 0,
            "fail_count": 0,
        })
        g["reports"].append(s)
        if s.get("started_at", "") > g["latest_started_at"]:
            g["latest_started_at"] = s.get("started_at", "")
        if s["overall_pass"]:
            g["pass_count"] += 1
        elif s["fail_count"]:
            g["fail_count"] += 1
    # Group ordering: alphabetical by hostname so kub-dev-01..03 then
    # kub-prd-01..03 stack in a predictable operator-friendly order.
    # Reports WITHIN a group stay newest-first (set by caller).
    ordered = sorted(groups.values(), key=lambda g: g["hostname"])
    return ordered


# ── HTML routes ─────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    summaries = _all_summaries()
    total = len(summaries)
    passing = sum(1 for s in summaries if s["overall_pass"])
    groups = _group_by_host(summaries)
    return TEMPLATES.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "reports": summaries,   # kept for the JSON export sidebar
            "groups": groups,
            "total": total,
            "passing": passing,
            "failing": total - passing,
        },
    )


@app.get("/matrix", response_class=HTMLResponse)
async def matrix(request: Request) -> HTMLResponse:
    summaries = _all_summaries()
    # Keep only the latest report per (hostname, profile) pair.
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for s in summaries:
        key = (s["hostname"], s["profile"])
        prev = latest.get(key)
        if not prev or s["started_at"] > prev["started_at"]:
            latest[key] = s
    hosts = sorted({s["hostname"] for s in summaries})
    profiles = sorted({s["profile"] for s in summaries})
    grid: dict[str, dict[str, dict[str, Any] | None]] = {
        h: {p: latest.get((h, p)) for p in profiles} for h in hosts
    }
    return TEMPLATES.TemplateResponse(
        "matrix.html",
        {
            "request": request,
            "hosts": hosts,
            "profiles": profiles,
            "grid": grid,
        },
    )


@app.get("/report/{filename}", response_class=HTMLResponse)
async def report_detail(request: Request, filename: str) -> HTMLResponse:
    path = _safe_path(filename)
    doc = _load_report(path)
    if not doc:
        raise HTTPException(404, f"report not found or unreadable: {filename}")

    # Sample charting inputs. samples[] is a list of flat JSON objects
    # emitted by telemetry.sh --jsonl. Schema:
    #   ts (ISO-8601 string), cpu_c, cpu_mhz, mb_c, load1,
    #   fan_cpu, fan_cha, fan_aux,
    #   nvme_composite_c, nvme_sensor1_c, nvme_sensor2_c, sda_c
    samples = doc.get("samples") or []
    ts_sec: list[float] = []
    cpu_c: list[float | None] = []
    cpu_mhz: list[float | None] = []
    nvme_tmp: list[float | None] = []
    ssd_tmp: list[float | None] = []
    mb_c: list[float | None] = []
    fan_cpu: list[float | None] = []
    for sam in samples:
        if not isinstance(sam, dict):
            continue
        ts_sec.append(_to_unix_s(sam.get("ts")))
        cpu_c.append(_numeric(sam.get("cpu_c")))
        cpu_mhz.append(_numeric(sam.get("cpu_mhz")))
        nvme_tmp.append(_numeric(sam.get("nvme_composite_c")))
        ssd_tmp.append(_numeric(sam.get("sda_c")))
        mb_c.append(_numeric(sam.get("mb_c")))
        fan_cpu.append(_numeric(sam.get("fan_cpu")))

    tests = doc.get("tests") or []
    hardware = doc.get("hardware") or {}

    # Inline baseline scores for CPU + RAM (bogo_ops_per_s). Attaching
    # score info as an extra key on each test lets the "All Tests" table
    # render a utilisation bar without rebuilding the test list in the
    # template. Only populated for tests we have baselines for.
    scored_tests: list[dict[str, Any]] = []
    for t in tests:
        if not isinstance(t, dict):
            scored_tests.append(t)
            continue
        tt = dict(t)
        name = t.get("name")
        if name in ("cpu", "ram") and "bogo_ops_per_s" in t:
            s = _score(t["bogo_ops_per_s"], f"{name}.bogo_ops_per_s")
            if s:
                tt["bogo_ops_per_s_score"] = s
        scored_tests.append(tt)

    return TEMPLATES.TemplateResponse(
        "report.html",
        {
            "request": request,
            "filename": filename,
            "doc": doc,
            "hardware": hardware,
            "tests": scored_tests,
            "fio_by_disk": _fio_stages_by_disk(tests),
            "drive_state_by_disk": _drive_state_by_disk(tests),
            "cpu_state_by_disk": _cpu_state_by_disk(tests),
            "net_tests": _net_tests(tests),
            "nics_with_model": _nics_with_model(hardware.get("nics") or []),
            "sample_count": len(samples),
            "chart_ts": json.dumps(ts_sec),
            "chart_cpu_c": json.dumps(cpu_c),
            "chart_cpu_mhz": json.dumps(cpu_mhz),
            "chart_nvme": json.dumps(nvme_tmp),
            "chart_ssd": json.dumps(ssd_tmp),
            "chart_mb": json.dumps(mb_c),
            "chart_fan_cpu": json.dumps(fan_cpu),
        },
    )


@app.post("/report/{filename}/delete")
async def report_delete(filename: str) -> RedirectResponse:
    path = _safe_path(filename)
    if path.exists():
        try:
            path.unlink()
            log.info("deleted report %s", filename)
        except OSError as e:
            raise HTTPException(500, f"delete failed: {e}") from None
    return RedirectResponse("/", status_code=303)


# ── JSON API ────────────────────────────────────────────────────────────────


@app.get("/api/reports")
async def api_list() -> list[dict[str, Any]]:
    return _all_summaries()


@app.get("/api/report/{filename}")
async def api_report(filename: str) -> JSONResponse:
    path = _safe_path(filename)
    doc = _load_report(path)
    if not doc:
        raise HTTPException(404, filename)
    return JSONResponse(doc)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "reports": len(_list_report_files()),
        "data_dir": str(DATA_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "uptime_s": int(time.time() - _START),
    }


# ── Helpers ─────────────────────────────────────────────────────────────────


_START = time.time()


def _safe_path(filename: str) -> Path:
    """Reject traversal. Reports are flat files directly under DATA_DIR."""
    if "/" in filename or ".." in filename or filename.startswith("."):
        raise HTTPException(400, "bad filename")
    p = DATA_DIR / filename
    if p.parent.resolve() != DATA_DIR.resolve():
        raise HTTPException(400, "bad filename")
    return p


def _dig(d: dict[str, Any], *keys: str) -> float | None:
    """Nested dict walk returning numeric or None."""
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    if isinstance(cur, (int, float)):
        return float(cur)
    return None


def _numeric(v: Any) -> float | None:
    """Coerce to float; reject '?' / None / strings."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _to_unix_s(v: Any) -> float:
    """telemetry.sh emits ts as ISO-8601 ('2026-04-23T10:41:51Z').
    Convert to unix seconds so Chart.js labels can be 't - t0'.
    """
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        try:
            from datetime import datetime

            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


# ─── Fleet-specific performance baselines ─────────────────────────────────
# These are the "what a healthy Q170S1 node should deliver" numbers for
# each measured metric. Hard-coded rather than auto-learned because:
#
#   * The fleet is exactly 6 identical machines (ASUS Q170S1 + i7-7700T +
#     32 GiB DDR4 + PM981 + S3710 + I219-LM + I226-V). There's no device
#     heterogeneity to infer baselines from — it's the same hardware
#     6 times. Auto-learning from the fleet would just regress to the
#     fleet mean, which *hides* the dev-03-is-slow case we're trying to
#     detect.
#
#   * Datasheets + stable-driver numbers give us ground truth. PM981's
#     3.2 GB/s is physics (PCIe Gen3 x4 effective throughput), not an
#     aspiration.
#
#   * We can always adjust if thresholds turn out wrong — one edit, one
#     redeploy. That's cheaper than a self-calibrating mechanism that
#     drifts.
#
# Each entry: target = "healthy hardware should reach or beat this"
#             floor  = "below this, something is wrong — flag red"
#             higher_better = True for throughput/IOPS, False for latency
#
# Sources:
#   PM981: Samsung OEM datasheet (3.5 GB/s seq read peak). Real-world
#          clean runs on this image + kernel landed 3.1 GiB/s, which we
#          use as target. Floor = 2.8 GiB/s (< this = Gen2 link suspect).
#   S3710: Intel DC S3710 400GB datasheet (550 MB/s read, 470 MB/s write,
#          85k/45k r/w 4k IOPS). Adjusted to MiB/s for consistency.
#   I219-LM: 1 GbE line-rate. iperf3 -P4 TCP usually lands 940 Mbps
#            (line - TCP/IP overhead).
#   I226-V: 2.5 GbE line-rate. iperf3 -P4 TCP ≈ 2350 Mbps.
#   CPU/RAM: stress-ng bogo_ops/s — high variance; target is the median
#            of first 10 healthy runs in the fleet. Treat as "suggestive,
#            not diagnostic" — unlike fio numbers these swing ±15 %
#            between kernel versions.
_BASELINES: dict[str, dict[str, Any]] = {
    # NVMe PM981 — fio read matrix
    "fio.nvme.read.seq_1m.bw_mibps":         {"target": 3100, "floor": 2800, "higher_better": True,  "unit": "MiB/s"},
    "fio.nvme.read.seq_1m.iops":             {"target": 3100, "floor": 2800, "higher_better": True,  "unit": "IOPS"},
    "fio.nvme.read.seq_64k.bw_mibps":        {"target": 2300, "floor": 2000, "higher_better": True,  "unit": "MiB/s"},
    "fio.nvme.read.rand_4k_qd32.iops":       {"target": 280000, "floor": 200000, "higher_better": True, "unit": "IOPS"},
    "fio.nvme.read.rand_4k_qd32.bw_mibps":   {"target": 1100, "floor": 780, "higher_better": True, "unit": "MiB/s"},
    "fio.nvme.read.rand_4k_qd1.iops":        {"target": 20000, "floor": 12000, "higher_better": True, "unit": "IOPS"},
    "fio.nvme.read.rand_4k_qd1.lat_us_mean": {"target": 45, "floor": 80, "higher_better": False, "unit": "µs"},
    "fio.nvme.read.rand_4k_qd1.lat_us_p99":  {"target": 120, "floor": 180, "higher_better": False, "unit": "µs"},
    # NVMe PM981 — destructive write matrix
    "fio.nvme.write.seq_1m.bw_mibps":        {"target": 1900, "floor": 1500, "higher_better": True, "unit": "MiB/s"},
    "fio.nvme.write.rand_4k_qd32.iops":      {"target": 220000, "floor": 150000, "higher_better": True, "unit": "IOPS"},
    "fio.nvme.write.rand_4k_qd1.lat_us_mean":{"target": 25, "floor": 60, "higher_better": False, "unit": "µs"},
    # SSD Intel DC S3710 — fio read matrix (SATA 6 Gb/s saturates at ~555 MiB/s)
    "fio.ssd.read.seq_1m.bw_mibps":          {"target": 530, "floor": 480, "higher_better": True, "unit": "MiB/s"},
    "fio.ssd.read.seq_64k.bw_mibps":         {"target": 530, "floor": 480, "higher_better": True, "unit": "MiB/s"},
    "fio.ssd.read.rand_4k_qd32.iops":        {"target": 56000, "floor": 45000, "higher_better": True, "unit": "IOPS"},
    "fio.ssd.read.rand_4k_qd1.iops":         {"target": 10000, "floor": 7000, "higher_better": True, "unit": "IOPS"},
    "fio.ssd.read.rand_4k_qd1.lat_us_mean":  {"target": 70, "floor": 130, "higher_better": False, "unit": "µs"},
    "fio.ssd.read.rand_4k_qd1.lat_us_p99":   {"target": 150, "floor": 250, "higher_better": False, "unit": "µs"},
    # SSD DC S3710 — destructive write matrix (PLP-backed, steady-state)
    "fio.ssd.write.seq_1m.bw_mibps":         {"target": 460, "floor": 380, "higher_better": True, "unit": "MiB/s"},
    "fio.ssd.write.rand_4k_qd32.iops":       {"target": 43000, "floor": 30000, "higher_better": True, "unit": "IOPS"},
    # Network
    "net.net-mgmt.bw_mbps":                  {"target": 940, "floor": 900, "higher_better": True, "unit": "Mbps"},
    "net.net-mgmt.retransmits":              {"target": 0, "floor": 10, "higher_better": False, "unit": ""},
    "net.net-data.bw_mbps":                  {"target": 2350, "floor": 2200, "higher_better": True, "unit": "Mbps"},
    "net.net-data.retransmits":              {"target": 0, "floor": 10, "higher_better": False, "unit": ""},
    # CPU + RAM (stress-ng bogo_ops/s — treat as rough, ±15 % kernel variance)
    "cpu.bogo_ops_per_s":                    {"target": 9500, "floor": 7500, "higher_better": True, "unit": "bops/s"},
    "ram.bogo_ops_per_s":                    {"target": 800, "floor": 550, "higher_better": True, "unit": "bops/s"},
}


def _score(actual: Any, key: str) -> dict[str, Any] | None:
    """Score a measurement against the baseline for `key`. Returns None
    if no baseline or actual is non-numeric. Returns a dict:

      { pct: 0-140   (100 = target, 0 = floor, 140 = 1.4× target),
        band: "good"|"warn"|"fail",
        target, floor, unit, higher_better }

    pct is clamped to 0-140 so the rendered bar never disappears off
    either end — that keeps visual proportionality readable.

    Band thresholds (higher_better case):
      actual >= target   → good  (≥ 100 %)
      actual >= floor    → warn  (≥ scaled floor fraction)
      actual <  floor    → fail
    For lower_better, reciprocal logic.
    """
    spec = _BASELINES.get(key)
    if not spec:
        return None
    v = _numeric(actual)
    if v is None:
        return None
    target = float(spec["target"])
    floor = float(spec["floor"])
    higher = spec["higher_better"]

    if higher:
        # Map floor → 0, target → 100, render beyond target up to 140.
        pct_raw = ((v - floor) / max(target - floor, 1e-9)) * 100 if target != floor else 100.0
        pct = max(0.0, min(140.0, pct_raw))
        if v >= target:
            band = "good"
        elif v >= floor:
            band = "warn"
        else:
            band = "fail"
    else:
        # lower is better. target=best, floor=worst-acceptable.
        # Map floor → 0, target → 100.
        if target == floor:
            pct = 100.0 if v <= target else 0.0
        else:
            pct_raw = ((floor - v) / max(floor - target, 1e-9)) * 100
            pct = max(0.0, min(140.0, pct_raw))
        if v <= target:
            band = "good"
        elif v <= floor:
            band = "warn"
        else:
            band = "fail"

    return {
        "pct": round(pct, 1),
        "band": band,
        "target": spec["target"],
        "floor": spec["floor"],
        "unit": spec["unit"],
        "higher_better": higher,
    }


# fio matrix stage metadata — matches tags used in scripts/lib/fio-matrix.sh.
# Ordering = presentation order in the UI (big sequential first, then random).
_FIO_STAGES: list[tuple[str, str]] = [
    ("seq_1m", "Seq 1 MiB QD32"),
    ("seq_64k", "Seq 64 KiB QD16"),
    ("rand_4k_qd32", "Rand 4 KiB QD32"),
    ("rand_4k_qd1", "Rand 4 KiB QD1"),
]


def _fio_stages_by_disk(tests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """For each disk test (name in {nvme, ssd, nvme-write, ssd-write,
    disk-burnin}) pluck fio_<dir>_<stage>_{iops,bw_mibps,lat_us_mean,lat_us_p99}
    and group by (test_name, direction, stage).

    Returns:
      { "nvme": {
          "read":  {"seq_1m": {"iops":..., "bw_mibps":..., "lat_us_mean":...,
                               "lat_us_p99":..., "label": "Seq 1 MiB QD32"},
                    "seq_64k": {...}, ...},
          "write": {...},
        }, ... }
    """
    out: dict[str, dict[str, Any]] = {}
    disk_tests = {"nvme", "ssd", "nvme-write", "ssd-write", "disk-burnin"}
    for t in tests:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if name not in disk_tests:
            continue
        dirs: dict[str, dict[str, Any]] = {}
        for direction in ("read", "write"):
            stages: dict[str, Any] = {}
            for tag, label in _FIO_STAGES:
                row: dict[str, Any] = {"label": label}
                for metric in ("iops", "bw_mibps", "lat_us_mean", "lat_us_p99"):
                    k = f"fio_{direction}_{tag}_{metric}"
                    if k in t:
                        row[metric] = t[k]
                        # Annotate with baseline score. Key shape matches
                        # _BASELINES (fio.<tag>.<dir>.<stage>.<metric>).
                        # tag here is the TEST name (nvme/ssd) — which
                        # maps 1:1 to the drive type in our fleet.
                        score = _score(t[k], f"fio.{name}.{direction}.{tag}.{metric}")
                        if score:
                            row[f"{metric}_score"] = score
                if len(row) > 1:
                    stages[tag] = row
            if stages:
                dirs[direction] = stages
        if dirs:
            out[name] = dirs
    return out


def _drive_state_by_disk(tests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pull the `drv_<tag>_*` METRIC fields emitted by
    scripts/lib/drive-state.sh into a per-disk-test nested dict.

    Schema per disk:
      {
        "one_shot": {
          "firmware_rev": "...", "percent_used": "N", "power_cycles": "N",
          "available_spare": "N",       # NVMe
          "wear_remaining_pct": "N",    # SATA
          "power_on_hours": "N",        # SATA
          "apst_enabled": "0"|"1",      # NVMe
          "device_model": "...",        # SATA
        },
        "pre":  { "pcie_link_speed": ..., "pcie_link_width": ...,
                  "sata_link_gbps": ..., "temp_c": ...,
                  "throttle_t1_count": ..., "throttle_t1_seconds": ...,
                  "throttle_t2_count": ..., "throttle_t2_seconds": ... },
        "post": { same shape },
        "delta": { "temp_c": (post - pre) or None, ... },  # computed
      }

    Why computed delta: "did the drive throttle during this run" is the
    one question that actually lands diagnostically, and it's answered
    by (post − pre) on throttle_t{1,2}_count/seconds. A zero there is
    the clean signal even on a drive that has throttled in past boots.
    """
    out: dict[str, dict[str, Any]] = {}
    disk_tests = {"nvme", "ssd", "nvme-write", "ssd-write", "disk-burnin"}
    # Fields that appear once per test (no _pre/_post suffix).
    _ONESHOT = (
        "firmware_rev", "percent_used", "power_cycles", "available_spare",
        "wear_remaining_pct", "power_on_hours", "apst_enabled",
        "device_model",
        # Link-level power management & IRQ routing (added in phase C
        # diagnostic follow-up for the dev-02/dev-03 QD1 gap).
        "pcie_aspm", "pcie_aspm_cap",
        "nvme_irq_count", "nvme_irq_cpu_spread",
        # NVMe power-state + feature probes. Added for the 3-tier QD1
        # latency mystery (23.5 / 33.3 / 84 µs across 6 identical PM981
        # drives). These fields show WHICH knob differs:
        #   pwr_state_current    — feature 0x02 low 5 bits (floor PS)
        #   pwr_state_exlat_us   — exit latency (µs) of the floor PS
        #   pwr_max_exlat_us     — worst-case exit latency across all PS
        #   pwr_nop_ps_count     — count of non-operational PS advertised
        #   irq_coalesce_thr     — completions before IRQ (0 = disabled)
        #   irq_coalesce_time_us — coalesce timer in µs (0 = disabled)
        "pwr_state_current", "pwr_state_exlat_us",
        "pwr_max_exlat_us", "pwr_nop_ps_count",
        "irq_coalesce_thr", "irq_coalesce_time_us",
        # APST transition table — per-source-PS (entry) idle-time
        # threshold (ITPT, ms) and target PS (ITPS). This is the knob
        # that controls "how eagerly does the drive park in a deep PS
        # between QD1 I/Os" — hypothesised root cause of the 3-tier
        # QD1 latency clustering across the fleet.
        "apst_ps0_itpt_ms", "apst_ps0_itps",
        "apst_ps1_itpt_ms", "apst_ps1_itps",
        "apst_ps2_itpt_ms", "apst_ps2_itps",
        "apst_ps3_itpt_ms", "apst_ps3_itps",
        "apst_ps4_itpt_ms", "apst_ps4_itps",
    )
    # Fields that appear twice (pre + post).
    _TIMED = (
        "pcie_link_speed", "pcie_link_width", "sata_link_gbps", "temp_c",
        "throttle_t1_count", "throttle_t1_seconds",
        "throttle_t2_count", "throttle_t2_seconds",
        # lspci-derived "real" link state + AER. These can contradict
        # the sysfs link fields when the LTSSM has renegotiated or when
        # the link is replay-storming.
        "lnksta_speed", "lnksta_speed_ok",
        "lnksta_width", "lnksta_width_ok",
        "aer_cesta", "aer_uesta",
    )

    for t in tests:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if name not in disk_tests:
            continue
        # The tag in METRIC keys is FIO_TAG which equals `name` for the
        # read-only case and also for the -write variants (nvme.sh sets
        # FIO_TAG="nvme" even in destructive runs). So the tag used in
        # the key is the test's base name minus any "-write" suffix.
        tag = name.split("-")[0]
        entry: dict[str, Any] = {
            "one_shot": {},
            "pre": {},
            "post": {},
            "delta": {},
        }
        for f in _ONESHOT:
            k = f"drv_{tag}_{f}"
            if k in t:
                entry["one_shot"][f] = t[k]
        for when in ("pre", "post"):
            for f in _TIMED:
                k = f"drv_{tag}_{f}_{when}"
                if k in t:
                    entry[when][f] = t[k]
        # Computed deltas for the fields that meaningfully subtract.
        for f in ("temp_c", "throttle_t1_count", "throttle_t1_seconds",
                  "throttle_t2_count", "throttle_t2_seconds"):
            a = entry["pre"].get(f)
            b = entry["post"].get(f)
            if a is None or b is None:
                continue
            try:
                entry["delta"][f] = float(b) - float(a)
            except (TypeError, ValueError):
                continue
        if entry["one_shot"] or entry["pre"] or entry["post"]:
            out[name] = entry
    return out


def _cpu_state_by_disk(tests: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pull the `cpu_*` METRIC fields emitted by scripts/lib/cpu-state.sh
    off each disk test and return a per-disk-test summary. Keyed by
    disk-test name so the template can render side-by-side with the drive
    state card. Rationale for "per disk test" rather than "per report":
    we want to see whether the *governor-pin override* in nvme.sh / ssd.sh
    actually landed and whether C-state residency was low during the run.

    Schema per disk:
      {
        "one_shot": {
          "governor": "performance",  # what we set
          "freq_driver": "intel_pstate",
          "idle_driver": "intel_idle",
          "no_turbo": "0",
          "freq_min_ghz": 0.8, "freq_max_ghz": 3.8,
          "sched_nvme0n1": "none",
        },
        "pre":  { "freq_mean_ghz": ..., "cstate_residency": {name:usec,...} },
        "post": { same },
        "delta": { "cstate_residency": {name: usec,...} }  # post - pre
      }
    """
    out: dict[str, dict[str, Any]] = {}
    disk_tests = {"nvme", "ssd", "nvme-write", "ssd-write", "disk-burnin"}
    for t in tests:
        if not isinstance(t, dict):
            continue
        name = t.get("name")
        if name not in disk_tests:
            continue
        os_info: dict[str, Any] = {}
        # One-shot CPU fields.
        for src, dst in (
            ("cpu_governor", "governor"),
            ("cpu_freq_driver", "freq_driver"),
            ("cpu_idle_driver", "idle_driver"),
            ("cpu_no_turbo", "no_turbo"),
            ("cpu_idle_states_count", "idle_states_count"),
            # Phase-C diagnostic additions.
            ("cpu_intel_pstate_status", "intel_pstate_status"),
            ("cpu_energy_perf_pref", "energy_perf_pref"),
            ("cpu_governor_pin_ok", "governor_pin_ok"),
            ("cpu_dimm_populated_count", "dimm_populated_count"),
            ("cpu_dimm_speed_mts", "dimm_speed_mts"),
            ("cpu_dimm_channel_layout", "dimm_channel_layout"),
        ):
            if src in t:
                os_info[dst] = t[src]
        # Governor pre/post — useful to show whether the pin took effect
        # (pre=powersave, post=performance → pin worked). Both go into
        # pre/post buckets instead of one_shot so the template can render
        # them in the time-ordered section.
        if "cpu_governor_pre" in t:
            pre_gov = t["cpu_governor_pre"]
        else:
            pre_gov = None
        if "cpu_governor_post" in t:
            post_gov = t["cpu_governor_post"]
        else:
            post_gov = None
        # Frequency bounds, khz → GHz.
        for src, dst in (
            ("cpu_freq_min_khz", "freq_min_ghz"),
            ("cpu_freq_max_khz", "freq_max_ghz"),
        ):
            v = _numeric(t.get(src))
            if v is not None:
                os_info[dst] = round(v / 1_000_000, 2)
        # Block schedulers, any key starting with cpu_sched_.
        for k, v in t.items():
            if isinstance(k, str) and k.startswith("cpu_sched_"):
                os_info[k.replace("cpu_sched_", "sched_")] = v

        pre: dict[str, Any] = {}
        post: dict[str, Any] = {}
        if pre_gov is not None:
            pre["governor"] = pre_gov
        if post_gov is not None:
            post["governor"] = post_gov
        for when, bucket in (("pre", pre), ("post", post)):
            v = _numeric(t.get(f"cpu_freq_mean_khz_{when}"))
            if v is not None:
                bucket["freq_mean_ghz"] = round(v / 1_000_000, 2)
            res: dict[str, float] = {}
            for k, val in t.items():
                if not isinstance(k, str):
                    continue
                # cpu_cstate_<NAME>_usec_<when>
                prefix = "cpu_cstate_"
                suffix = f"_usec_{when}"
                if k.startswith(prefix) and k.endswith(suffix):
                    state_name = k[len(prefix):-len(suffix)]
                    try:
                        res[state_name] = float(val)
                    except (TypeError, ValueError):
                        continue
            if res:
                bucket["cstate_residency_usec"] = res

        delta: dict[str, Any] = {}
        if "cstate_residency_usec" in pre and "cstate_residency_usec" in post:
            d: dict[str, float] = {}
            for s, v_post in post["cstate_residency_usec"].items():
                v_pre = pre["cstate_residency_usec"].get(s, 0.0)
                d[s] = v_post - v_pre
            delta["cstate_residency_usec"] = d

        entry = {"one_shot": os_info, "pre": pre, "post": post, "delta": delta}
        if os_info or pre or post:
            out[name] = entry
    return out


def _net_tests(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pull iperf3-based NIC tests (name ∈ {net-mgmt, net-data}).

    Each test may carry per-second series as pipe-separated METRICs
    (bw_series_mbps=940|941|…, retr_series=0|0|1|…) — split those into
    real arrays under the `bw_series` / `retr_series` keys so the template
    can plot them without doing string surgery in Jinja.
    """
    out: list[dict[str, Any]] = []
    for t in tests:
        if not isinstance(t, dict) or t.get("name") not in ("net-mgmt", "net-data"):
            continue
        tt = dict(t)
        bw_raw = str(t.get("bw_series_mbps") or "")
        retr_raw = str(t.get("retr_series") or "")
        tt["bw_series"] = _split_num_pipe(bw_raw)
        tt["retr_series"] = _split_num_pipe(retr_raw)
        # Baseline scores for bandwidth + retransmits.
        bw_score = _score(t.get("bw_mbps"), f"net.{t.get('name')}.bw_mbps")
        if bw_score:
            tt["bw_mbps_score"] = bw_score
        retr_score = _score(t.get("retransmits"), f"net.{t.get('name')}.retransmits")
        if retr_score:
            tt["retransmits_score"] = retr_score
        out.append(tt)
    return out


def _split_num_pipe(s: str) -> list[float]:
    """Split pipe-separated numeric list; drop blanks + non-numerics."""
    out: list[float] = []
    for chunk in s.split("|"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(float(chunk))
        except ValueError:
            continue
    return out


def _nic_status(tests: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The universal nic_i226v presence record — rc/status/iface/reason."""
    for t in tests:
        if isinstance(t, dict) and t.get("name") == "nic_i226v":
            return t
    return None


# Driver → friendly model map for the 6× Q170S1 node NICs.
# Canonical mapping: e1000e = Intel I219-LM (1 GbE mgmt),
#                    igc    = Intel I226-V (2.5 GbE data, PCIe add-in).
_NIC_MODEL_BY_DRIVER = {
    "e1000e": "Intel I219-LM (1 GbE, mgmt)",
    "igc": "Intel I226-V (2.5 GbE, data)",
}


def _nics_with_model(nics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Enrich NIC records with a `model` string derived from driver."""
    out: list[dict[str, Any]] = []
    for n in nics:
        if not isinstance(n, dict):
            continue
        nn = dict(n)
        nn["model"] = _NIC_MODEL_BY_DRIVER.get(n.get("driver"), n.get("driver") or "?")
        out.append(nn)
    return out
