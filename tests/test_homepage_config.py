"""Structural guards over the declarative Homepage config in apps/homepage/.

These are not unit tests of Python code — they are drift guards over the YAML
that `_ensure_homepage_config_via_ctx` uploads verbatim to the NAS. Homepage
itself validates nothing: a bad group name or a dead href renders as a broken
card and nobody notices. Cheaper to catch here.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
HOMEPAGE_DIR = REPO_ROOT / "apps" / "homepage"

# Cluster admin UIs sit behind Pocket-ID OIDC ForwardAuth on traefik-admin.
# A siteMonitor probe against these resolves green off the 302 to the IdP even
# when the backing app is dead — see design decision D2. Encode that here so a
# future well-meaning edit cannot add a lying status dot.
FORWARDAUTH_GATED_HOST_PREFIXES = (
    "grafana-", "prom-", "alertmanager-", "longhorn-", "hubble-", "traefik-int-",
)


def _load(name: str) -> object:
    return yaml.safe_load((HOMEPAGE_DIR / name).read_text())


def _iter_cards():
    """Yield (group_name, card_name, card_body) for every card in services.yaml."""
    for group in _load("services.yaml"):
        (group_name, entries), = group.items()
        for entry in entries:
            (card_name, body), = entry.items()
            yield group_name, card_name, body


def test_every_homepage_yaml_parses() -> None:
    names = sorted(p.name for p in HOMEPAGE_DIR.glob("*.yaml"))
    assert "services.yaml" in names
    for name in names:
        if name == "docker-compose.yaml":
            continue
        yaml.safe_load((HOMEPAGE_DIR / name).read_text())


def test_no_dead_href_placeholders() -> None:
    """A card linking to '#' is not information — it is a broken promise."""
    dead = [
        f"{group}/{card}"
        for group, card, body in _iter_cards()
        if str(body.get("href", "")).strip() in {"#", ""}
    ]
    assert dead == [], f"cards with placeholder href: {dead}"


def test_every_service_group_appears_in_settings_layout() -> None:
    """Homepage auto-orders any group absent from settings.yaml `layout:`,
    which scrambles the dashboard. Both files must change together."""
    layout = _load("settings.yaml")["layout"]
    groups = [next(iter(g)) for g in _load("services.yaml")]
    missing = [g for g in groups if g not in layout]
    stale = [g for g in layout if g not in groups]
    assert missing == [], f"groups missing from settings.yaml layout: {missing}"
    assert stale == [], f"layout entries with no matching group: {stale}"


def test_forwardauth_gated_cards_have_no_sitemonitor() -> None:
    """Design decision D2 — no false green on OIDC-gated cluster UIs."""
    offenders = []
    for group, card, body in _iter_cards():
        host = urlparse(str(body.get("href", ""))).hostname or ""
        if host.startswith(FORWARDAUTH_GATED_HOST_PREFIXES) and "siteMonitor" in body:
            offenders.append(f"{group}/{card}")
    assert offenders == [], f"gated cards must not be probed (D2): {offenders}"


def test_every_probed_host_has_an_extra_hosts_entry() -> None:
    """The compose `extra_hosts` block is belt-and-braces so siteMonitor
    survives a DNS hiccup. Every probed host must be in it — a probe that
    depends on DNS alone goes red on a resolver blip and reads as an outage.
    """
    compose = yaml.safe_load((HOMEPAGE_DIR / "docker-compose.yaml").read_text())
    pinned = {
        entry.split(":")[0]
        for entry in compose["services"]["homepage"]["extra_hosts"]
    }
    probed = {
        urlparse(str(body["siteMonitor"])).hostname
        for _, _, body in _iter_cards()
        if "siteMonitor" in body
    }
    missing = sorted(probed - pinned)
    assert missing == [], f"probed hosts absent from extra_hosts: {missing}"


def test_traefik_dashboard_hrefs_carry_the_dashboard_path() -> None:
    """Traefik dashboard IngressRoutes match on
    `PathPrefix(/dashboard) || PathPrefix(/api)`, so a bare `/` href returns
    404 from a perfectly healthy Traefik. Shipped broken on 2026-09-13 for
    both traefik-int-* cards; caught by clicking, not by a test.
    """
    offenders = []
    for group, card, body in _iter_cards():
        href = str(body.get("href", ""))
        host = urlparse(href).hostname or ""
        if host.startswith(("traefik-int-", "traefik-nas")) and "/dashboard" not in href:
            offenders.append(f"{group}/{card} -> {href}")
    assert offenders == [], f"Traefik cards needing /dashboard/: {offenders}"
