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


# Ports we check in parallel to answer "is this node's OS actually
# serving on the network?" — AMT's PowerState=On means "CPU has power",
# NOT "OS is booted and responsive". Any open port here → OS alive.
# Covers:
#   22    SSH — generic Linux / provisioning systems
#   50000 Talos machined API (Talos nodes)
#   50001 Talos apid
#   6443  Kubernetes API (control-plane nodes once cluster is up)
#   10250 kubelet (any K8s node)
# Add more per-node via `os_probe_ports: [...]` in nodes.yaml.
_DEFAULT_OS_PROBE_PORTS = [22, 50000, 50001, 6443, 10250]


async def _os_alive(host: str, ports: list[int], timeout: float = 1.2) -> tuple[bool, int | None]:
    """Return (alive, first-open-port). Probes all ports concurrently;
    returns on first success. All failing → (False, None). Total wall
    time ≈ single `timeout` even with a dead host."""

    async def _try_port(p: int) -> int | None:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, p), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return p
        except (asyncio.TimeoutError, OSError):
            return None

    tasks = [asyncio.create_task(_try_port(p)) for p in ports]
    try:
        for coro in asyncio.as_completed(tasks):
            port = await coro
            if port is not None:
                # Cancel the remaining probes — first success wins
                for t in tasks:
                    if not t.done():
                        t.cancel()
                return True, port
    finally:
        # Ensure cancellation propagates cleanly
        for t in tasks:
            if not t.done():
                t.cancel()
    return False, None


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
    # Secondary probe: TCP-connect a set of common "OS is serving"
    # ports. AMT PowerState=On tells us the CPU is powered, NOT that
    # the OS is running. Having any of these ports respond is a strong
    # signal the OS is alive and networking. See _DEFAULT_OS_PROBE_PORTS.
    probe_ports = node.get("os_probe_ports") or _DEFAULT_OS_PROBE_PORTS
    # Use node's hostname (DNS-resolves to OS-side IP) when provided;
    # otherwise default to <name>.w1.lv (mirrors our AMT naming).
    probe_host = node.get("os_hostname") or f"{name}.w1.lv"
    alive, which_port = await _os_alive(probe_host, probe_ports)
    status["os_alive"] = alive
    status["os_alive_port"] = which_port  # which port responded (for UI)
    status["os_probe_host"] = probe_host
    status["os_probe_ports"] = probe_ports
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


def _classify(state_name: str, reachable: bool) -> str:
    """Condense AMT power state + reachability into one bucket:
    'on' / 'off' / 'sleep' / 'unreachable'. OS-reachability probe
    data (os_alive) still runs in the background + is available on
    /api/nodes/{name} for operator use, but we don't factor it into
    the status signal — bare-metal AMT view only, per operator ask."""
    if not reachable:
        return "unreachable"
    if state_name.startswith("On"):
        return "on"
    if any(k in state_name for k in ("Soft", "Sleep", "Hibernate")):
        return "sleep"
    if state_name.startswith("Off"):
        return "off"
    return "unknown"


def _power_badge(state_name: str, reachable: bool) -> str:
    """Emoji-prefixed state for Homepage customapi compatibility.
      🟢 On            = CPU powered (AMT PowerState=2)
      🟡 Off - Soft    = Soft-off / Sleep / Hibernate (AC present, ME alive)
      🔴 Off - Hard    = Hard off (S5)
      🔴 Unreachable  = AMT not answering
    """
    cls = _classify(state_name, reachable)
    if cls == "unreachable":
        return "🔴 Unreachable"
    if cls == "on":
        return "🟢 " + state_name
    if cls == "sleep":
        return "🟡 " + state_name
    if cls == "off":
        return "🔴 " + state_name
    return "⚪ " + state_name


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
        "os_alive": s.get("os_alive", False),   # still exposed for operators
        "power_state": state_name,
        "power_badge": _power_badge(state_name, reachable),
        "last_seen_ago_s": int(time.time() - s.get("polled_at", 0)),
    }


@app.get("/api/summary")
async def summary() -> dict[str, str]:
    """Flat summary for Homepage's customapi widget: one line per node.
    Response is a dict keyed by node name (not an array) because
    Homepage's customapi mappings are positional field-name accessors,
    not array iterators — it can't loop over a JSON array. With this
    shape the dashboard config explicitly names each node, which also
    keeps render order deterministic.
    """
    out: dict[str, str] = {}
    for name in sorted(_cache.keys()):
        s = _cache[name]
        power = s.get("power") or {}
        state_name = power.get("state_name", "unreachable")
        reachable = s.get("reachable", False)
        badge = _power_badge(state_name, reachable)
        ip = s.get("host", "?")
        out[name] = f"{badge}  ·  {ip}"
    return out


@app.get("/api/nodes/{name}/status", responses={503: {}, 418: {}})
async def get_status(name: str) -> JSONResponse:
    """Homepage-siteMonitor endpoint. Returns HTTP status based on node
    class so Homepage renders a colored dot in the card's upper-right:

      200 — OS is alive (AMT On + SSH port open)
      418 — AMT On but OS not responding (POST/stuck) or sleeping
      503 — AMT unreachable OR hard off

    Homepage's siteMonitor is binary (2xx = green, anything else = red)
    so 418 still shows red — but the status endpoint distinguishes
    cases in case we later wire a richer UI element. The amtctl
    dashboard card renders full 3-color state.
    """
    if name not in _cache:
        raise HTTPException(404, f"unknown node: {name}")
    s = _cache[name]
    power = s.get("power") or {}
    state_name = power.get("state_name", "unreachable")
    reachable = s.get("reachable", False)
    cls = _classify(state_name, reachable)
    if cls == "on":
        code = 200
    elif cls == "sleep":
        code = 418     # "powered but not at S0" — shows as down on Homepage siteMonitor
    else:
        code = 503     # hard off or unreachable
    return JSONResponse(
        status_code=code,
        content={"node": name, "class": cls, "state": state_name, "reachable": reachable},
    )


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
    # Bumped to 60s — AMT's internal PT60S op timeout plus a little buffer.
    # Previous 30s was tight enough that a slow AMT response could blow
    # past httpx before AMT finished, leaving the request half-done.
    try:
        async with AMTClient(host, AMT_USER, AMT_PASSWORD, timeout=60.0) as c:
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
