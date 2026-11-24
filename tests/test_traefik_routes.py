"""The NAS Traefik's routing isolation: no admin UI answers on the dev-VLAN `devapi` entry point.

`devapi` (10.10.15.10:8000) is a data-plane entry for Bambuddy's API alone (docs/runbooks/add-a-service.md).
Traefik v3 attaches a router that names no entry point to every default one, so every router must name its own,
and only the Bambuddy API router may name `devapi`, for API paths only.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ROUTES = ROOT / "apps" / "traefik" / "routes.yaml"
COMPOSE = ROOT / "apps" / "traefik" / "docker-compose.yaml"

DEVAPI_ROUTERS = {"bambuddy-api-dev"}


def _routers() -> dict:
    return yaml.safe_load(ROUTES.read_text())["http"]["routers"]


def test_every_router_names_its_entry_points():
    for name, router in _routers().items():
        assert router.get("entryPoints"), f"router {name} names no entry point"


def test_only_the_bambuddy_api_router_is_on_devapi():
    on_devapi = {name for name, r in _routers().items() if "devapi" in r["entryPoints"]}
    assert on_devapi == DEVAPI_ROUTERS
    for name in DEVAPI_ROUTERS:
        router = _routers()[name]
        assert router["entryPoints"] == ["devapi"], f"{name} must be on devapi alone"
        assert "PathPrefix(`/api/`)" in router["rule"], f"{name} must serve API paths only"


def test_admin_routers_are_on_websecure_alone():
    for name, router in _routers().items():
        if name not in DEVAPI_ROUTERS:
            assert router["entryPoints"] == ["websecure"], f"{name} must be on websecure alone"


def test_websecure_is_the_default_entry_point():
    command = yaml.safe_load(COMPOSE.read_text())["services"]["traefik"]["command"]
    assert "--entrypoints.websecure.asDefault=true" in command
    assert not any(c.startswith("--entrypoints.devapi.asDefault") for c in command)
