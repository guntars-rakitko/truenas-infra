"""FastAPI sidecar for the amtctl dashboard.

Async-polls all AMT-managed nodes every `poll_interval_s` seconds, caches
the results in-memory, serves them over REST for the static UI and
Homepage customapi widgets.

Endpoints:
  GET  /api/nodes                → list of all cached node statuses
  GET  /api/nodes/{name}         → single node full status
  GET  /api/nodes/{name}/power   → just power state (Homepage-friendly JSON)
  POST /api/nodes/{name}/action  → trigger an action; body: {action, boot?}
                                   action: on/off_graceful/off_hard/reset/power_cycle
                                   boot:   pxe/bios/disk/default (default if omitted)
  GET  /                         → static UI (web/index.html)

Config:
  AMTCTL_NODES_YAML     path to nodes.yaml (default /config/nodes.yaml)
  AMTCTL_AMT_USER       AMT admin username, same across all nodes
  AMTCTL_AMT_PASSWORD   AMT admin password, same across all nodes
  AMTCTL_POLL_INTERVAL  seconds between refresh cycles (default 60)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from amt import AMTClient, AMTError

log = logging.getLogger("amtctl")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

NODES_YAML = Path(os.environ.get("AMTCTL_NODES_YAML", "/config/nodes.yaml"))
AMT_USER = os.environ.get("AMTCTL_AMT_USER", "admin")
AMT_PASSWORD = os.environ["AMTCTL_AMT_PASSWORD"]  # required — fail loud if unset
POLL_INTERVAL = int(os.environ.get("AMTCTL_POLL_INTERVAL", "60"))

# ── State ────────────────────────────────────────────────────────────────────

# Cached node status: name → dict (last full_status result + polled_at)
_cache: dict[str, dict[str, Any]] = {}
_nodes: list[dict[str, str]] = []  # from nodes.yaml: [{name, host, role}, ...]


def _load_nodes() -> list[dict[str, str]]:
    if not NODES_YAML.exists():
        log.error("nodes.yaml not found at %s", NODES_YAML)
        return []
    data = yaml.safe_load(NODES_YAML.read_text()) or {}
    return data.get("nodes") or []


async def _poll_one(node: dict[str, str]) -> tuple[str, dict[str, Any]]:
    name = node["name"]
    host = node["host"]
    try:
        async with AMTClient(host, AMT_USER, AMT_PASSWORD, timeout=8.0) as c:
            status = await c.full_status()
    except AMTError as e:
        status = {"host": host, "reachable": False, "errors": [f"{e.kind}: {e.detail}"]}
    except Exception as e:  # noqa: BLE001 — last-ditch catch, log and continue
        log.exception("unexpected error polling %s", host)
        status = {"host": host, "reachable": False, "errors": [f"internal: {type(e).__name__}: {e}"]}
    status["name"] = name
    status["role"] = node.get("role", "")
    status["polled_at"] = time.time()
    return name, status


async def _poll_loop() -> None:
    log.info("poll loop starting; interval=%ss, nodes=%s", POLL_INTERVAL,
             [n["name"] for n in _nodes])
    while True:
        t0 = time.monotonic()
        try:
            results = await asyncio.gather(*[_poll_one(n) for n in _nodes])
            for name, status in results:
                _cache[name] = status
            reachable = sum(1 for s in _cache.values() if s.get("reachable"))
            log.info("poll done in %.2fs  reachable=%d/%d",
                     time.monotonic() - t0, reachable, len(_nodes))
        except Exception:  # noqa: BLE001
            log.exception("poll loop error")
        await asyncio.sleep(POLL_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: ARG001 — FastAPI hook signature
    global _nodes  # noqa: PLW0603
    _nodes = _load_nodes()
    # Prime the cache with placeholder entries so the UI doesn't 404 until
    # the first poll completes.
    for n in _nodes:
        _cache[n["name"]] = {
            "name": n["name"], "host": n["host"], "role": n.get("role", ""),
            "reachable": False, "errors": ["not yet polled"],
            "polled_at": 0.0,
        }
    task = asyncio.create_task(_poll_loop())
    yield
    task.cancel()


app = FastAPI(title="amtctl", lifespan=lifespan)

# ── API routes ────────────────────────────────────────────────────────────────


@app.get("/api/nodes")
async def list_nodes() -> list[dict[str, Any]]:
    return list(_cache.values())


@app.get("/api/nodes/{name}")
async def get_node(name: str) -> dict[str, Any]:
    if name not in _cache:
        raise HTTPException(404, f"unknown node: {name}")
    return _cache[name]


def _power_badge(state_name: str, reachable: bool) -> str:
    """Prefix the power state with a colored emoji dot so Homepage's
    customapi widget renders a visual cue inline. Homepage doesn't
    support color mappings natively — this is the emoji workaround."""
    if not reachable:
        return "🔴 Unreachable"
    if state_name.startswith("On"):
        return "🟢 " + state_name
    if "Off - Soft" in state_name or "Soft" in state_name:
        # Soft-off means AC present, ME alive, just OS shutdown. Yellow,
        # not red — actionable (Power On works), not "dead".
        return "🟡 " + state_name
    if "Sleep" in state_name or "Hibernate" in state_name:
        return "🟡 " + state_name
    if state_name.startswith("Off"):
        return "🔴 " + state_name
    return "⚪ " + state_name  # unknown intermediate state


@app.get("/api/nodes/{name}/power")
async def get_power(name: str) -> dict[str, Any]:
    """Homepage-customapi-friendly shape: simple flat JSON, few fields."""
    if name not in _cache:
        raise HTTPException(404, f"unknown node: {name}")
    s = _cache[name]
    power = s.get("power") or {}
    state_name = power.get("state_name", "unreachable")
    reachable = s.get("reachable", False)
    return {
        "node": name,
        "reachable": reachable,
        "power_state": state_name,
        "power_badge": _power_badge(state_name, reachable),
        "last_seen_ago_s": int(time.time() - s.get("polled_at", 0)),
    }


@app.post("/api/nodes/{name}/action")
async def post_action(
    name: str,
    body: dict[str, Any] = Body(...),
) -> dict[str, Any]:
    if name not in _cache:
        raise HTTPException(404, f"unknown node: {name}")
    action = body.get("action")
    boot = body.get("boot")  # None / "default" / "pxe" / "bios" / "disk"
    if not action:
        raise HTTPException(400, "action required (on/off_graceful/off_hard/reset/power_cycle)")
    node = next(n for n in _nodes if n["name"] == name)
    host = node["host"]
    try:
        async with AMTClient(host, AMT_USER, AMT_PASSWORD, timeout=30.0) as c:
            result = await c.power_action(action, boot)
    except AMTError as e:
        log.warning("action %s on %s failed: %s", action, name, e)
        raise HTTPException(502, f"{e.kind}: {e.detail}") from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    # Kick off a refresh of just this node so the UI reflects post-action state
    # without waiting for the next full poll cycle.
    asyncio.create_task(_refresh_one(name))
    return result


async def _refresh_one(name: str) -> None:
    await asyncio.sleep(3)  # let the ME settle
    try:
        node = next(n for n in _nodes if n["name"] == name)
        n, status = await _poll_one(node)
        _cache[n] = status
    except Exception:  # noqa: BLE001
        log.exception("refresh failed for %s", name)


# ── Health ───────────────────────────────────────────────────────────────────


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ok", "nodes_configured": len(_nodes)}


# ── Static UI at / ───────────────────────────────────────────────────────────

# Mount the web directory at /static so the HTML can reference /static/*.
_WEB_DIR = Path(__file__).parent / "web"
if _WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=_WEB_DIR), name="static")

    @app.get("/")
    async def root() -> FileResponse:
        return FileResponse(_WEB_DIR / "index.html")
else:
    @app.get("/")
    async def root_no_ui() -> JSONResponse:  # noqa: D103
        return JSONResponse({"error": "web/ not found — UI unavailable, API still works at /api/nodes"})
