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

    return {
        "filename": path.name,
        "hostname": doc.get("hostname", "?"),
        "profile": doc.get("profile", "?"),
        "started_at": started,
        "ended_at": ended,
        "duration_s": duration_s,
        "test_count": len(tests),
        "pass_count": pass_ct,
        "fail_count": fail_ct,
        "overall_pass": fail_ct == 0 and pass_ct > 0,
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


def _all_summaries() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in _list_report_files():
        doc = _load_report(p)
        if not doc:
            continue
        out.append(_summarise(p, doc))
    return out


# ── HTML routes ─────────────────────────────────────────────────────────────


@app.get("/", response_class=HTMLResponse)
async def overview(request: Request) -> HTMLResponse:
    summaries = _all_summaries()
    total = len(summaries)
    passing = sum(1 for s in summaries if s["overall_pass"])
    return TEMPLATES.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "reports": summaries,
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

    # Sample charting inputs. samples[] is a list of JSON objects — each
    # typically has `ts` (unix s), CPU temps, disk temps, fan rpm etc.
    samples = doc.get("samples") or []
    ts: list[float] = []
    cpu_pkg: list[float | None] = []
    nvme_tmp: list[float | None] = []
    ssd_tmp: list[float | None] = []
    for sam in samples:
        if not isinstance(sam, dict):
            continue
        ts.append(sam.get("ts", 0))
        cpu_pkg.append(_dig(sam, "cpu", "pkg_c"))
        nvme_tmp.append(_dig(sam, "nvme", "composite_c"))
        ssd_tmp.append(_dig(sam, "ssd", "temperature_c"))

    return TEMPLATES.TemplateResponse(
        "report.html",
        {
            "request": request,
            "filename": filename,
            "doc": doc,
            "hardware": doc.get("hardware") or {},
            "tests": doc.get("tests") or [],
            "sample_count": len(samples),
            "chart_ts": json.dumps(ts),
            "chart_cpu": json.dumps(cpu_pkg),
            "chart_nvme": json.dumps(nvme_tmp),
            "chart_ssd": json.dumps(ssd_tmp),
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
