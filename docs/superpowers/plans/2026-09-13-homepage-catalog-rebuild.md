# Homepage Catalog Rebuild Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the `home.w1.lv` service catalog from 19 NAS-only cards to 37 cards covering every human-clickable service across the NAS, both Kubernetes clusters, the GIKS applications, the SMS-gateway appliances and the network fleet — and remove two dead DNS names the audit exposed.

**Architecture:** Homepage is declarative-only: six YAML files in `apps/homepage/` are glob-uploaded to `/mnt/tank/system/apps-config/homepage` by `_ensure_homepage_config_via_ctx` (`src/truenas_infra/modules/apps.py:1398`) during `manage.sh` phase `apps --apply`. No code change is needed to add cards — only YAML. A new pytest module adds structural guards so the two failure modes the design flagged (dead `href: '#'` links, and groups missing from the `settings.yaml` `layout:` block) cannot regress silently.

**Tech Stack:** Homepage v1.13.2, YAML, pytest, MkDocs (wiki mirror), MikroTik RouterOS (DNS), Flux CD (kube-infra).

**Spec:** `docs/superpowers/specs/2026-09-13-homepage-catalog-rebuild-design.md`

---

## File Structure

| File | Repo | Responsibility | Action |
|---|---|---|---|
| `tests/test_homepage_config.py` | truenas-infra | Structural guards over the Homepage YAML set | **Create** |
| `apps/homepage/services.yaml` | truenas-infra | The card catalog — 8 groups | **Rewrite** |
| `apps/homepage/settings.yaml` | truenas-infra | Group order + column counts (`layout:`) | Modify |
| `apps/homepage/bookmarks.yaml` | truenas-infra | Side-rail links: repos, ops consoles, vendor docs | **Rewrite** |
| `apps/homepage/kubernetes.yaml` | truenas-infra | Records D4 (declarative by choice) | Modify (comment only) |
| `apps/homepage/docker-compose.yaml` | truenas-infra | `extra_hosts` for newly probed hosts | Modify |
| `docs/reference/links.md` | wiki | Browser-facing URL mirror | **Rewrite** tables |
| `docs/architecture/hostnames.md` | wiki | Full hostname inventory | Verify + patch |
| `configs/dns.yaml` | mikrotik-infra | Remove 2 dead records | Modify |
| `.../per-cluster/prd/admin-plane-ingress.yaml` | kube-infra | Remove dead commented placeholder | Modify |

`tests/test_homepage_config.py` is a **new file**, deliberately not added to
`tests/test_apps.py` — that file has uncommitted in-flight work on another
branch (`fix/rebuild-on-build-context-change`) and must not be touched.

---

## Task 1: Structural guards for the Homepage YAML set

**Files:**
- Create: `tests/test_homepage_config.py`

- [ ] **Step 1: Write the failing tests**

```python
"""Structural guards over the declarative Homepage config in apps/homepage/.

These are not unit tests of Python code — they are drift guards over the YAML
that `_ensure_homepage_config_via_ctx` uploads verbatim to the NAS. Homepage
itself validates nothing: a bad group name or a dead href renders as a broken
card and nobody notices. Cheaper to catch here.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest
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
```

- [ ] **Step 2: Run the tests to verify `test_no_dead_href_placeholders` fails**

Run: `cd ~/github/truenas-infra && uv run pytest tests/test_homepage_config.py -v`

Expected: `test_no_dead_href_placeholders` **FAILS** with
`cards with placeholder href: ['Media (planned)/Plex (deferred)', 'Media (planned)/qBittorrent (deferred)']`.
The other three tests PASS against the current files.

This is the correct starting state: the guard is real and the current config
violates it. Tasks 2–3 make it pass by deleting the `Media (planned)` group.

- [ ] **Step 3: Commit the guards**

```bash
git add tests/test_homepage_config.py
git commit -m "test(homepage): structural guards over the declarative YAML set

Homepage validates nothing — a dead href or a group missing from the
settings.yaml layout block renders as a broken/scrambled dashboard and
nobody notices. Four guards: every YAML parses, no placeholder href, every
services.yaml group is pinned in settings.yaml layout, and no ForwardAuth-
gated cluster UI carries a siteMonitor (design decision D2 — a probe there
resolves green off the 302 to Pocket-ID even when the app is dead).

test_no_dead_href_placeholders fails as committed: the Media (planned)
group ships two href:'#' cards. Removed in the next commit."
```

---

## Task 2: Rewrite `services.yaml` — the catalog

**Files:**
- Modify: `apps/homepage/services.yaml` (full rewrite)

- [ ] **Step 1: Write the new catalog**

Replace the entire file with the content below. Group order here must match
the `layout:` block written in Task 3.

The header comment keeps the ⚠ WIKI MIRROR rule, and adds an explicit note
that the MikroTik **widget** removal of 2026-04-24 still stands — the Network
group added here is link-only and needs no REST API on any device.

```yaml
# Homepage service catalog — declarative.
# https://gethomepage.dev/configs/services/
#
# A card is earned by a URL a HUMAN OPENS IN A BROWSER (design decision D1).
# Data-plane endpoints — giks-db-*/w1-db-* (Postgres LBs), s3-*:9000, gh-*
# (Flux webhook receivers), hc-* (blackbox probe targets), iperf3, NTP — are
# deliberately absent. They live in wiki/docs/architecture/hostnames.md, which
# is the inventory surface. This file is a launcher.
#
# ⚠️ WIKI MIRROR: Adding/removing a service here ALSO requires updating:
#   wiki/docs/architecture/hostnames.md  (admin / data / infra plane tables)
#   wiki/docs/reference/links.md         (dashboards table)
#
# ── siteMonitor policy ──────────────────────────────────────────────────────
# Cluster admin UIs (grafana-*, prom-*, alertmanager-*, longhorn-*, hubble-*,
# traefik-int-*) sit behind Pocket-ID OIDC ForwardAuth on traefik-admin. A
# probe against them gets 302 → id-*.w1.lv and resolves GREEN even if the
# backing app is dead — that proves the gate answered, not the app. They are
# therefore LINK-ONLY, with no siteMonitor (D2). tests/test_homepage_config.py
# enforces this.
#
# Apps that serve their OWN login page (GIKS AdminApp, SMS-gateway console) DO
# keep a probe: there a 200 is served by the application itself, so it really
# does prove the app is up (D3).
#
# ⚠️ No MikroTik WIDGETS by design (2026-04-24). The `mikrotik` widget only
# works if the NAS can reach each device's REST :80, which required opening
# /ip service www + mgmt→base-vlan firewall carve-outs — a widened attack
# surface for a convenience dashboard. THAT DECISION STILL STANDS. The Network
# group below is plain <a href> links only: no widget, no probe, no REST, no
# firewall rule. Do not "restore" the widget.

- Infrastructure:
    # ── K8s nodes (hardware layer via AMT) ──────────────────────
    # Each line shows "host: 🟢/🟡/🔴 state · IP". Click-through → amtctl
    # for the rich per-node view (hardware info, network, power actions).
    - K8s prd nodes:
        icon: mdi-server-network
        href: https://amtctl.w1.lv/
        description: "3× ASUS Q170S1 (prd)"
        widget:
          type: customapi
          url: http://10.10.5.10:8000/api/summary
          refreshInterval: 15000
          mappings:
            - field: kub-prd-01
              label: kub-prd-01
            - field: kub-prd-02
              label: kub-prd-02
            - field: kub-prd-03
              label: kub-prd-03

    - K8s dev nodes:
        icon: mdi-server-network
        href: https://amtctl.w1.lv/
        description: "3× ASUS Q170S1 (dev)"
        widget:
          type: customapi
          url: http://10.10.5.10:8000/api/summary
          refreshInterval: 15000
          mappings:
            - field: kub-dev-01
              label: kub-dev-01
            - field: kub-dev-02
              label: kub-dev-02
            - field: kub-dev-03
              label: kub-dev-03

    - AMT control:
        icon: mdi-power-plug
        href: https://amtctl.w1.lv/
        description: "Power on/off/reset · one-time boot to PXE or BIOS"
        siteMonitor: https://amtctl.w1.lv/

    - MeshCentral:
        icon: meshcentral.png
        href: https://mc.w1.lv/
        description: "AMT / KVM management (6 nodes)"
        # config.json has RedirPort:0 + TlsOffload:true (2026-04-21), which
        # killed the :4430 bogus redirect. / returns 200 clean.
        siteMonitor: https://mc.w1.lv/

    - PXE / netboot.xyz:
        icon: mdi-network-outline
        href: https://pxe.w1.lv/
        description: "iPXE boot menu — Talos, BIOS flash, stress image"
        siteMonitor: https://pxe.w1.lv/

    - hw-validation stress:
        icon: mdi-pulse
        href: https://stress.w1.lv/
        description: "Stress-test reports + fleet pass/fail matrix"
        siteMonitor: https://stress.w1.lv/

    - NAS Traefik:
        icon: traefik.png
        href: https://traefik-nas.w1.lv/dashboard/
        description: "Mgmt-plane reverse proxy"
        # /api/http/routers returns JSON via api@internal — always 200 when
        # Traefik is up. /ping would need --ping=true in the compose.
        siteMonitor: https://traefik-nas.w1.lv/api/http/routers
        widget:
          type: traefik
          url: https://traefik-nas.w1.lv

# ── Kubernetes ───────────────────────────────────────────────────────────────
# LB IPs: prd 10.10.5.30, dev 10.10.5.40 (Cilium LB-IPAM on mgmt-vlan, same
# /24 as the NAS — no routing, no firewall carve-out). All LINK-ONLY per D2.

- Kubernetes · prd:
    - Grafana prd:
        icon: grafana.png
        href: https://grafana-prd.w1.lv/
        description: "Dashboards · Pocket-ID gated"

    - Prometheus prd:
        icon: prometheus.png
        href: https://prom-prd.w1.lv/
        description: "Metrics + alert rules (read-only API)"

    - Alertmanager prd:
        icon: alertmanager.png
        href: https://alertmanager-prd.w1.lv/
        description: "Alert routing + silences"

    - Longhorn prd:
        icon: longhorn.png
        href: https://longhorn-prd.w1.lv/
        description: "Distributed block storage · volumes + backups"

    - Hubble prd:
        icon: mdi-sitemap
        href: https://hubble-prd.w1.lv/
        description: "Cilium network flow observability"

    - Traefik admin prd:
        icon: traefik.png
        href: https://traefik-int-prd.w1.lv/
        description: "Cluster admin-plane proxy dashboard"

    - Pocket-ID prd:
        icon: mdi-shield-key
        href: https://id-prd.w1.lv/
        description: "OIDC identity provider — the login surface itself"

- Kubernetes · dev:
    - Grafana dev:
        icon: grafana.png
        href: https://grafana-dev.w1.lv/
        description: "Dashboards · Pocket-ID gated"

    - Prometheus dev:
        icon: prometheus.png
        href: https://prom-dev.w1.lv/
        description: "Metrics + alert rules (read-only API)"

    - Alertmanager dev:
        icon: alertmanager.png
        href: https://alertmanager-dev.w1.lv/
        description: "Alert routing + silences"

    - Longhorn dev:
        icon: longhorn.png
        href: https://longhorn-dev.w1.lv/
        description: "Distributed block storage · volumes + backups"

    - Hubble dev:
        icon: mdi-sitemap
        href: https://hubble-dev.w1.lv/
        description: "Cilium network flow observability"

    - Traefik admin dev:
        icon: traefik.png
        href: https://traefik-int-dev.w1.lv/
        description: "Cluster admin-plane proxy dashboard"

    - Pocket-ID dev:
        icon: mdi-shield-key
        href: https://id-dev.w1.lv/
        description: "OIDC identity provider — the login surface itself"

# ── Applications ─────────────────────────────────────────────────────────────
# These serve their own login pages, so a 200 proves the app is alive (D3).
# Reachable from the NAS: fleet.yaml forward chain accepts mgmt → prd/dev.

- Applications:
    - GIKS Admin prd:
        icon: mdi-account-group
        href: https://admin-prd.giks.lv/
        description: "Member management — LAN/WG only (v2 pre-production soak)"
        siteMonitor: https://admin-prd.giks.lv/

    - GIKS Admin dev:
        icon: mdi-account-group
        href: https://admin-dev.giks.lv/
        description: "Member management — dev"
        siteMonitor: https://admin-dev.giks.lv/

    - GIKS portal dev:
        icon: mdi-account-box
        href: https://dev.giks.lv/
        description: "Member self-service portal — dev"
        siteMonitor: https://dev.giks.lv/

    - GIKS Admin (public):
        icon: mdi-cloud-outline
        href: https://admin.giks.lv/
        description: "⚠ serves the MAINTENANCE OVERLAY via cloudflared, not the app"

    - SMS gateway prd:
        icon: mdi-cellphone-message
        href: https://sms-gw-prd.w1.lv/admin
        description: "Blazor console · API at /v1 · OpenAPI at /swagger"
        siteMonitor: https://sms-gw-prd.w1.lv/admin/login

    - SMS gateway dev:
        icon: mdi-cellphone-message
        href: https://sms-gw-dev.w1.lv/admin
        description: "Blazor console · API at /v1 · OpenAPI at /swagger"
        siteMonitor: https://sms-gw-dev.w1.lv/admin/login

- NAS:
    - TrueNAS:
        icon: truenas-scale.png
        href: https://nas.w1.lv/
        description: "NAS management UI"
        siteMonitor: https://nas.w1.lv/
        widget:
          type: truenas
          url: https://nas.w1.lv
          key: "{{HOMEPAGE_VAR_TRUENAS_API_KEY}}"
          enablePools: true   # show tank pool size + health
          nasType: scale      # TrueNAS SCALE / Community Edition

- Storage:
    # The s3-*.w1.lv:9000 API endpoints are NOT carded (D1) — an S3 API is not
    # something a human clicks. They remain in wiki hostnames.md.
    - MinIO prd Console:
        icon: minio.png
        href: https://minio-prd.w1.lv/
        description: "S3 admin — prd (Velero / workloads)"
        siteMonitor: https://minio-prd.w1.lv/
        # No widget: — Homepage v1.12 removed the native `minio` widget type.

    - MinIO dev Console:
        icon: minio.png
        href: https://minio-dev.w1.lv/
        description: "S3 admin — dev"
        siteMonitor: https://minio-dev.w1.lv/

# ── Network ──────────────────────────────────────────────────────────────────
# LINK-ONLY. No widget, no siteMonitor, no REST, no firewall rule. See the
# MikroTik note in the file header before changing anything here.
# These are base-vlan (10.10.0.0/24) devices; reach them over WireGuard or
# from base-vlan. WebFig is HTTP (self-signed if HTTPS), hence http:// hrefs.

- Network:
    - Router (RB5009):
        icon: mikrotik.png
        href: http://router.w1.lv/
        description: "WebFig — VLANs, firewall, DNS, DHCP, WireGuard"

    - Switch data (CRS310):
        icon: mikrotik.png
        href: http://sw-data.w1.lv/
        description: "2.5G traffic switch — K8s data + NAS trunk"

    - Switch mgmt (CRS326):
        icon: mikrotik.png
        href: http://sw-mgmt.w1.lv/
        description: "1G mgmt switch — K8s mgmt NICs + home"

    - WiFi AP (cAP ax):
        icon: mikrotik.png
        href: http://wifi.w1.lv/
        description: "CAP mode — 5GHz pinned ch36"

    - LTE modem:
        icon: mikrotik.png
        href: http://lte.w1.lv/
        description: "LMT 5G — WAN backup"

    - Egress appliance:
        icon: mikrotik.png
        href: http://egress.w1.lv/
        description: "wAP ac LTE6 — rotating-IP SOCKS5 :1080 for web-tracker"

- Docs:
    - Internal Wiki:
        icon: mkdocs.png
        href: https://wiki.w1.lv/
        description: "Homelab + GIKS docs — start here"
        siteMonitor: https://wiki.w1.lv/
```

- [ ] **Step 2: Verify the card and group counts**

Run:
```bash
cd ~/github/truenas-infra && python3 -c "
import yaml,pathlib
d=yaml.safe_load(pathlib.Path('apps/homepage/services.yaml').read_text())
t=0
for g in d:
    (n,e),=g.items(); print(f'{n}: {len(e)}'); t+=len(e)
print('TOTAL', t)"
```

Expected output:
```
Infrastructure: 7
Kubernetes · prd: 7
Kubernetes · dev: 7
Applications: 6
NAS: 1
Storage: 2
Network: 6
Docs: 1
TOTAL 37
```

- [ ] **Step 3: Do NOT commit yet** — `test_every_service_group_appears_in_settings_layout` will now fail because `settings.yaml` still pins the old groups. Task 3 fixes it, and the two land in one commit.

---

## Task 3: Update `settings.yaml` layout

**Files:**
- Modify: `apps/homepage/settings.yaml` — replace the `layout:` block

- [ ] **Step 1: Replace the `layout:` block**

Replace everything from `layout:` up to (but not including) the
`# Treat service cards with an offline siteMonitor` comment with:

```yaml
layout:
  Infrastructure:
    style: row
    columns: 4
  Kubernetes · prd:
    style: row
    columns: 4
  Kubernetes · dev:
    style: row
    columns: 4
  Applications:
    style: row
    columns: 3
  NAS:
    style: row
    columns: 3
  Storage:
    style: row
    columns: 2
  Network:
    style: row
    columns: 3
  Docs:
    style: row
    columns: 3
```

- [ ] **Step 2: Run the guards — all four must now pass**

Run: `cd ~/github/truenas-infra && uv run pytest tests/test_homepage_config.py -v`

Expected: **4 passed**. In particular `test_no_dead_href_placeholders` now
passes (the `Media (planned)` group is gone) and
`test_every_service_group_appears_in_settings_layout` reports neither missing
nor stale groups.

- [ ] **Step 3: Commit**

```bash
git add apps/homepage/services.yaml apps/homepage/settings.yaml
git commit -m "feat(homepage): rebuild the catalog — 19 cards to 37

home.w1.lv was inventoried in April 2026, before the Kubernetes clusters,
the SMS-gateway appliances, the GIKS apps and the egress appliance existed.
It listed 19 cards covering the NAS mgmt plane only.

Adds: Kubernetes prd + dev groups (7 cards each — Grafana, Prometheus,
Alertmanager, Longhorn, Hubble, Traefik-admin, Pocket-ID), an Applications
group (GIKS admin prd/dev, GIKS portal dev, public admin, SMS gateway
prd/dev), a Network group (router, both switches, AP, LTE, egress), a
dedicated AMT control card, and pxe.w1.lv — which has had both a DNS record
and a NAS Traefik route since day one but never had a card.

No network change was required: Homepage binds 10.10.5.10 and the cluster
admin LB IPs are 10.10.5.30/.40 on the same mgmt /24, and fleet.yaml already
accepts mgmt -> prd and mgmt -> dev.

Cluster admin UIs are link-only with no siteMonitor (D2): a probe against a
ForwardAuth-gated URL resolves green off the 302 to Pocket-ID even when the
app behind it is dead. GIKS and SMS keep probes because they serve their own
login pages, where a 200 really does prove the app is up (D3).

Removes the Media (planned) group (two href:'#' cards; plex/qbittorrent stay
enabled:false in config/apps.yaml and get real cards when the tier lands) and
the two MinIO S3 data-plane cards (an API on :9000 is not clickable — they
stay in wiki hostnames.md).

The MikroTik widget removal of 2026-04-24 stands: the Network group is plain
links, needing no REST, no /ip service www, and no firewall carve-out. Said
explicitly in the file header so nobody restores the widget."
```

---

## Task 4: Rebuild `bookmarks.yaml`

**Files:**
- Modify: `apps/homepage/bookmarks.yaml` (full rewrite)

- [ ] **Step 1: Replace the file**

```yaml
# Homepage bookmarks — declarative side-rail links.
# https://gethomepage.dev/configs/bookmarks/
#
# Repo links live HERE, not as service cards (design decision D6): 14 repos as
# cards would swamp the launcher, and a repo link has no health state worth a
# status dot.

- Repos:
    - kube-infra:
        - abbr: KI
          href: https://github.com/guntars-rakitko/kube-infra
    - truenas-infra:
        - abbr: TN
          href: https://github.com/guntars-rakitko/truenas-infra
    - mikrotik-infra:
        - abbr: MT
          href: https://github.com/guntars-rakitko/mikrotik-infra
    - GIKS:
        - abbr: GK
          href: https://github.com/guntars-rakitko/GIKS
    - sms-gateway:
        - abbr: SG
          href: https://github.com/guntars-rakitko/sms-gateway
    - web-tracker:
        - abbr: WT
          href: https://github.com/guntars-rakitko/web-tracker
    - wiki:
        - abbr: WK
          href: https://github.com/guntars-rakitko/wiki
    - dotfiles:
        - abbr: DF
          href: https://github.com/guntars-rakitko/dotfiles

- More repos:
    - bios-config:
        - abbr: BC
          href: https://github.com/guntars-rakitko/bios-config
    - hw-validation:
        - abbr: HV
          href: https://github.com/guntars-rakitko/hw-validation
    - latvian-registers:
        - abbr: LR
          href: https://github.com/guntars-rakitko/latvian-registers
    - AdTracker:
        - abbr: AT
          href: https://github.com/guntars-rakitko/AdTracker
    - rss-website-monitor:
        - abbr: RS
          href: https://github.com/guntars-rakitko/rss-website-monitor
    - GIKS-v1 (legacy):
        - abbr: G1
          href: https://github.com/guntars-rakitko/GIKS-v1

- Ops consoles:
    - GitHub:
        - abbr: GH
          href: https://github.com/guntars-rakitko
    - CloudFlare:
        - abbr: CF
          href: https://dash.cloudflare.com/
    - Doppler:
        - abbr: DP
          href: https://dashboard.doppler.com/
    - Backblaze B2:
        - abbr: B2
          href: https://secure.backblaze.com/b2_buckets.htm
    # ⚠ Region pinned: SES lives in eu-north-1. The WRONG region returns an
    # EMPTY list rather than an error, which reads as "all clean".
    - AWS SES (eu-north-1):
        - abbr: SE
          href: https://eu-north-1.console.aws.amazon.com/ses/home?region=eu-north-1

- Vendor docs:
    - Talos Linux:
        - abbr: TL
          href: https://www.talos.dev/docs/
    - Flux CD:
        - abbr: FX
          href: https://fluxcd.io/flux/
    - CloudNativePG:
        - abbr: PG
          href: https://cloudnative-pg.io/documentation/
    - Cilium:
        - abbr: CI
          href: https://docs.cilium.io/
    - Longhorn:
        - abbr: LH
          href: https://longhorn.io/docs/
    - Traefik v3:
        - abbr: TR
          href: https://doc.traefik.io/traefik/
    - TrueNAS API:
        - abbr: TA
          href: https://www.truenas.com/docs/api/
    - MkDocs Material:
        - abbr: MK
          href: https://squidfunk.github.io/mkdocs-material/
    - Claude Code:
        - abbr: CC
          href: https://docs.claude.com/claude-code
```

- [ ] **Step 2: Verify it parses and the guards still pass**

Run: `cd ~/github/truenas-infra && uv run pytest tests/test_homepage_config.py -v`
Expected: **4 passed**.

- [ ] **Step 3: Commit**

```bash
git add apps/homepage/bookmarks.yaml
git commit -m "feat(homepage): rebuild bookmarks — 14 repos, ops consoles, vendor docs

Repo links move off service cards into the side rail (D6). The workspace has
14 GitHub repos; the dashboard carded 6.

Adds an Ops consoles group (GitHub, CloudFlare, Doppler, Backblaze B2, AWS
SES) and extends vendor docs with Talos, Flux, CNPG, Cilium and Longhorn —
the stack the clusters actually run.

The SES link pins region=eu-north-1 in the URL: the wrong SES region returns
an EMPTY result rather than an error, which reads as 'all clean'.

Drops the Beelink support bookmark (ME mini NVMe RMA closed)."
```

---

## Task 5: Correct the stale `kubernetes.yaml` comment

**Files:**
- Modify: `apps/homepage/kubernetes.yaml` (comment only — the file stays empty)

- [ ] **Step 1: Replace the file contents**

The current text says the file is empty "until kube-infra stands up the
clusters". Both clusters have been live for months, so as written it reads as
an outstanding TODO. It is a **decision**, and the file should say so.

```yaml
# Homepage Kubernetes integration — INTENTIONALLY DISABLED, BY CHOICE.
#
# This is NOT "not yet". Both clusters have been live since 2026. Homepage's
# `mode: cluster` would auto-discover Ingress/HTTPRoute objects and card them
# automatically. We decline it for three reasons (design decision D4):
#
#   1. Declarative-only is this repo's doctrine. Cards are enumerated in
#      services.yaml the same way Traefik routes live in routes.yaml and DNS
#      lives in dns.yaml. Auto-discovery puts the dashboard out of sync with
#      the YAML that is supposed to be the source of truth.
#
#   2. It needs a long-lived read-only ServiceAccount token per cluster. A
#      90-day SA token lapsing unnoticed is exactly what cost 14 days of
#      silent cluster-agent blindness in Aug 2026. One more expiring
#      credential to rotate, for cards we can write by hand.
#
#   3. It would need the NAS to reach both API servers, widening what the
#      dashboard container can talk to.
#
# If this is ever revisited, `cluster-agent-readonly` already exists at
# kube-infra/flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml —
# scoped to get/list/watch with secrets, pods/exec and eviction withheld.
# Weigh the rotation burden before adopting it.
```

- [ ] **Step 2: Commit**

```bash
git add apps/homepage/kubernetes.yaml
git commit -m "docs(homepage): kubernetes.yaml is disabled by choice, not pending

The comment said 'INTENTIONALLY EMPTY until kube-infra stands up the
clusters'. Both clusters have been live for months, so as written it read as
an outstanding TODO that someone would eventually action.

Records the actual decision (D4) and its reasons: declarative-only doctrine,
one more expiring SA token to rotate (a lapsed 90-day token cost 14 days of
cluster-agent blindness in Aug 2026), and widening what the dashboard
container may reach. Points at cluster-agent-readonly for whoever revisits."
```

---

## Task 6: `extra_hosts` for the newly probed hosts

**Files:**
- Modify: `apps/homepage/docker-compose.yaml` — the `extra_hosts:` block

- [ ] **Step 1: Add the four newly probed hosts**

The existing `extra_hosts` block is belt-and-braces against a DNS hiccup for
every host with a `siteMonitor`. Task 2 added four probed hosts that are not
in it. Append to the block, after the `# MinIO S3 data plane (direct)` group:

```yaml
      # Apps probed by siteMonitor (mgmt → prd/dev is accepted in fleet.yaml)
      - "admin-prd.giks.lv:10.10.10.20"
      - "admin-dev.giks.lv:10.10.15.20"
      - "dev.giks.lv:10.10.15.20"
      - "sms-gw-prd.w1.lv:10.10.10.9"
      - "sms-gw-dev.w1.lv:10.10.15.9"
```

Also delete the two now-unused S3 data-plane entries (`s3-prd.w1.lv`,
`s3-dev.w1.lv`) — Task 2 removed the only cards that probed them.

- [ ] **Step 2: Verify the compose still parses**

Run: `cd ~/github/truenas-infra && python3 -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('apps/homepage/docker-compose.yaml').read_text()); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add apps/homepage/docker-compose.yaml
git commit -m "fix(homepage): extra_hosts for the newly probed GIKS + SMS endpoints

The extra_hosts block is belt-and-braces so siteMonitor survives a DNS
hiccup. The catalog rebuild added five probed hosts on the prd/dev VLANs
that were not in it. Reachable per fleet.yaml's 'mgmt -> prd/dev' accepts.

Drops the two s3-*.w1.lv entries — the only cards that probed them were the
S3 data-plane cards, removed in the catalog rebuild."
```

---

## Task 7: Open the truenas-infra PR

- [ ] **Step 1: Push and open the PR**

```bash
cd ~/github/truenas-infra
git push -u origin feat/homepage-catalog-rebuild
gh pr create --repo guntars-rakitko/truenas-infra --base main \
  --title "feat(homepage): rebuild the service catalog — 19 cards to 37" \
  --body-file -
```

PR body must state: the 19→37 delta, the D1/D2/D3/D4/D6 decisions, that no
network change was needed and why, the `pxe.w1.lv` gap, and a link to the
companion wiki / mikrotik-infra / kube-infra PRs from Tasks 8–10.

---

## Task 8: Wiki mirror

**Files:**
- Modify: `wiki/docs/reference/links.md` — rebuild the dashboards + repos tables
- Verify: `wiki/docs/architecture/hostnames.md`

- [ ] **Step 1: Branch**

```bash
cd ~/github/wiki && git fetch origin && git checkout -b docs/homepage-catalog-rebuild origin/main
```

- [ ] **Step 2: Rewrite the "Dashboards (internal)" table**

It must carry every card from Task 2 — all 14 cluster URLs, Applications,
Network, `pxe.w1.lv` — plus a column noting which are Pocket-ID gated. Keep
the existing `!!! info` source-of-truth admonition but add
`apps/homepage/services.yaml` to its list of mirrored sources.

Add a short subsection **"Deliberately not on the dashboard"** listing the
data-plane names (`giks-db-*`, `w1-db-*`, `s3-*`, `gh-*`, `hc-*`, iperf3,
NTP, `contacts-*`) with a one-line reason — so the absence reads as a
decision rather than an oversight.

- [ ] **Step 3: Extend the "Sibling repos" table from 6 to 14 rows**

Same repo list and URLs as Task 4's bookmarks.

- [ ] **Step 4: Check `hostnames.md`**

Run:
```bash
cd ~/github/wiki && for h in grafana-prd prom-prd alertmanager-prd longhorn-prd \
  hubble-prd id-prd traefik-int-prd sms-gw-prd egress pxe w1-db-prd; do
  printf '%-18s %s\n' "$h" "$(grep -c "$h" docs/architecture/hostnames.md)"; done
```
Any host with count `0` is missing — add it to the correct plane table.

- [ ] **Step 5: Build and deploy**

Run: `cd ~/github/wiki && ./tools/deploy.sh --verify`
Expected: build succeeds, `--verify` reports the site reachable at wiki.w1.lv.

- [ ] **Step 6: Commit and PR**

```bash
git add docs/reference/links.md docs/architecture/hostnames.md
git commit -m "docs(links): mirror the home.w1.lv catalog rebuild

The dashboards table listed 13 URLs and predated the clusters entirely: no
Grafana/Prometheus/Alertmanager/Longhorn/Hubble/Pocket-ID/Traefik-admin for
either env, no GIKS apps, no SMS gateway, no egress appliance. The repos
table listed 6 of 14.

Adds a 'Deliberately not on the dashboard' section so the absence of the
data-plane names reads as a decision, not an oversight.

Required by the WIKI MIRROR rule in apps/homepage/services.yaml."
git push -u origin docs/homepage-catalog-rebuild
gh pr create --repo guntars-rakitko/wiki --base main --fill
```

---

## Task 9: Remove the dead `traefik-pub-*` DNS records

**Files:**
- Modify: `mikrotik-infra/configs/dns.yaml`

Evidence (re-verify before deleting): zero IngressRoutes matching
`traefik-pub-*` on either live cluster; the only occurrence in kube-infra is
a commented-out placeholder; no open PR would add them.

- [ ] **Step 1: Re-verify against the live clusters**

```bash
for ctx in prd dev; do echo "== $ctx =="; kubectl --context "$ctx" get ingressroute -A -o json \
  | grep -oE 'traefik-pub-[a-z]+\.w1\.lv' | sort -u; done
```
Expected: no output for either context. **If anything prints, STOP** — the
record is live and must not be removed.

- [ ] **Step 2: Branch and delete both records**

```bash
cd ~/github/mikrotik-infra && git fetch origin && git checkout -b fix/remove-dead-traefik-pub-dns origin/main
```

Delete these four lines from `configs/dns.yaml`:

```yaml
  - name: traefik-pub-prd.w1.lv
    address: 10.10.5.30
    comment: kube-prd traefik-public dashboard (proxied via traefik-admin)
  - name: traefik-pub-dev.w1.lv
    address: 10.10.5.40
    comment: kube-dev traefik-public dashboard (proxied via traefik-admin)
```

- [ ] **Step 3: Re-render the router config**

Run: `cd ~/github/mikrotik-infra && python3 tools/render_fleet.py`
Expected: `build/01-config-router.rsc` regenerates; `git diff --stat build/`
shows only the two removed DNS lines.

- [ ] **Step 4: Commit and PR**

```bash
git add configs/dns.yaml build/
git commit -m "fix(dns): remove dead traefik-pub-{prd,dev} records

Both resolve to a healthy Traefik that has no matching rule, so they return
404 from a working proxy — the most misleading possible failure.

Verified: zero IngressRoutes matching traefik-pub-* on either live cluster.
The only occurrence anywhere in kube-infra is a commented-out placeholder in
per-cluster/prd/admin-plane-ingress.yaml, labelled 'Kept commented until
traefik-public service exposes its api port' and never wired up since Day 1.
traefik-pub-dev never had even that. No open PR would add either.

The real gap this exposed — traefik-public serves public traffic (giks.lv)
and its dashboard is reachable only by kubectl port-forward, while
traefik-admin and traefik-internal both have OIDC-gated dashboards — is
filed as a kube-infra issue rather than left as a dead name."
git push -u origin fix/remove-dead-traefik-pub-dns
gh pr create --repo guntars-rakitko/mikrotik-infra --base main --fill
```

- [ ] **Step 5: Sync the router (MAIN SESSION ONLY — this is a live mutation)**

After the PR merges: `cd ~/github/mikrotik-infra && ./manage.sh` → option 6
("Sync DNS static records"). Confirm the two records are gone:

```bash
ssh <router> '/ip dns static print where name~"traefik-pub"'
```
Expected: no rows.

---

## Task 10: Remove the kube-infra placeholder + file the real gap

**Files:**
- Modify: `flux-cd/infrastructure/configs/per-cluster/prd/admin-plane-ingress.yaml`

- [ ] **Step 1: Branch off `dev`** (kube-infra promotes dev → main)

```bash
cd ~/github/kube-infra && git fetch origin && git checkout -b fix/remove-dead-traefik-public-placeholder origin/dev
```

- [ ] **Step 2: Delete the dead block**

Remove the commented-out `IngressRoute` placeholder (the `# apiVersion:` …
`#     namespace: traefik-admin` block, ~22 lines) together with the
explanatory paragraph above it that exists only to justify it. Per the
standing rule: remove dead legacy code, do not document it.

- [ ] **Step 3: Confirm nothing else references it**

Run: `cd ~/github/kube-infra && grep -rn "traefik-pub" . | grep -v '^\./\.git'`
Expected: no output.

- [ ] **Step 4: Confirm the kustomization still builds**

Run: `cd ~/github/kube-infra && kubectl kustomize flux-cd/infrastructure/configs/per-cluster/prd > /dev/null && echo ok`
Expected: `ok`

- [ ] **Step 5: File the real gap as an issue**

```bash
gh issue create --repo guntars-rakitko/kube-infra \
  --title "traefik-public has no reachable dashboard" \
  --body "traefik-public serves the public traffic (giks.lv, www.giks.lv) but its dashboard is reachable only via kubectl port-forward, while traefik-admin and traefik-internal both have OIDC-gated dashboards. Exposing it needs the api port on the traefik-public Service plus an OIDC-gated IngressRoute on traefik-internal, in both envs. Surfaced by the 2026-09-13 home.w1.lv catalog audit, which found the DNS names traefik-pub-{prd,dev}.w1.lv pointing at a route that never existed."
```

- [ ] **Step 6: Commit and PR against `dev`**

```bash
git add flux-cd/infrastructure/configs/per-cluster/prd/admin-plane-ingress.yaml
git commit -m "chore(traefik): remove the dead traefik-public dashboard placeholder

A commented-out IngressRoute for traefik-pub-prd.w1.lv, labelled 'Kept
commented until traefik-public service exposes its api port'. It was never
wired up, and the DNS names it implied 404 from a healthy proxy.

Removed rather than documented, per the standing rule. The real gap —
traefik-public has no reachable dashboard — is filed as an issue, and the
dead DNS records are removed in a companion mikrotik-infra PR."
git push -u origin fix/remove-dead-traefik-public-placeholder
gh pr create --repo guntars-rakitko/kube-infra --base dev --fill
```

---

## Task 11: Apply and verify on the NAS

**Runs in the MAIN SESSION only** — this is a live mutation of the NAS.
Do this after the truenas-infra PR from Task 7 merges to `main`.

- [ ] **Step 1: Pull main and apply**

```bash
cd ~/github/truenas-infra && git checkout main && git pull
./manage.sh   # phase: apps --apply
```

Expected: `homepage_config_ensured` log lines for each of
`bookmarks.yaml`, `docker.yaml`, `kubernetes.yaml`, `services.yaml`,
`settings.yaml`, `widgets.yaml` with `changed=True` for the five edited, and
a container recreate for the `extra_hosts` change.

- [ ] **Step 2: Confirm the app is healthy**

Run: `cd ~/github/truenas-infra && ./manage.sh` → verify phase, or
`uv run python -m truenas_infra verify` — `check_app(app_name="homepage")`
must report running.

- [ ] **Step 3: Load the dashboard and check group order**

Open `https://home.w1.lv/`. Confirm groups render in this order:
Infrastructure, Kubernetes · prd, Kubernetes · dev, Applications, NAS,
Storage, Network, Docs. A scrambled order means a group is missing from the
`settings.yaml` `layout:` block.

- [ ] **Step 4: Confirm the probe policy visually**

- Every card in Infrastructure / NAS / Storage / Docs / Applications shows a
  status dot, and it is green.
- **No** card in Kubernetes · prd or Kubernetes · dev shows a status dot at
  all. Absence is the expected state (D2). A dot there means a `siteMonitor`
  crept in — `tests/test_homepage_config.py` should have caught it.
- No card in Network shows a dot.

- [ ] **Step 5: Click-through spot check**

Open one card per group: `pxe.w1.lv` (new), `grafana-prd.w1.lv` (expect the
Pocket-ID login), `sms-gw-dev.w1.lv/admin`, `router.w1.lv`. Each must land on
the intended service rather than a 404.

- [ ] **Step 6: Report results honestly**

Record which probes are green, which are red, and why. A red dot on a card is
a finding about the service, not a reason to remove the card.

---

## Notes for the implementer

- **Do not touch `tests/test_apps.py` or `src/truenas_infra/modules/apps.py`.**
  Both have uncommitted in-flight work on branch
  `fix/rebuild-on-build-context-change` in the shared clone. This plan's
  truenas-infra work happens in a separate worktree on
  `feat/homepage-catalog-rebuild`.
- **Never `git add -A`** in these repos — a concurrent session's uncommitted
  files will be swallowed. Always stage explicit paths, as every commit step
  above does.
- **Task 9 Step 5 and Task 11 are live mutations** (router DNS sync, NAS
  apply). They run in the main session only, never in a subagent.
