# Homepage service-catalog rebuild — design

**Date:** 2026-09-13
**Status:** approved (design), not yet implemented
**Repos touched:** `truenas-infra` (primary), `wiki`, `mikrotik-infra`, `kube-infra`

---

## Problem

`home.w1.lv` (Homepage, `apps/homepage/`) was built in April 2026, before the
Kubernetes clusters, the SMS-gateway appliances, the GIKS applications and the
egress appliance existed. It has not been re-inventoried since. Today it lists
**19 cards covering the NAS mgmt plane only**, and its own source comments still
describe the clusters as future work:

| File | Stale assertion |
|---|---|
| `apps/homepage/kubernetes.yaml` | "INTENTIONALLY EMPTY until kube-infra stands up the clusters" — both clusters have been live for months |
| `apps/homepage/services.yaml` (header) | "When actual K8s clusters come up, a separate Kubernetes group will handle pod/node/ingress state" — never happened |

Concretely missing from the dashboard:

- **18 cluster admin URLs** (Grafana / Prometheus / Alertmanager / Longhorn /
  Hubble / Traefik-admin / Pocket-ID, × prd and dev) — zero cards
- **`pxe.w1.lv`** — has a DNS record *and* a NAS Traefik route, has never had a card
- **SMS gateway** consoles (`sms-gw-{prd,dev}.w1.lv/admin`)
- **GIKS** applications (`admin-{prd,dev}.giks.lv`, `dev.giks.lv`, `admin.giks.lv`)
- **Network devices** (`router`, `sw-data`, `sw-mgmt`, `wifi`, `lte`, `egress`) —
  present in the wiki table, never on the dashboard
- **8 of 14 GitHub repos**, and every external ops console except Cloudflare

The wiki mirror (`wiki/docs/reference/links.md`) has the same gaps, which is a
violation of the ⚠ WIKI MIRROR rule stated at the top of `services.yaml` itself.

## Feasibility

The constraint that killed the MikroTik widget cards in 2026-04-24 does **not**
apply to this work:

- The Homepage container binds `10.10.5.10` on **mgmt-vlan `10.10.5.0/24`**.
  The cluster admin LB IPs are `10.10.5.30` (prd) and `10.10.5.40` (dev), served
  by Cilium LB-IPAM on the *same* /24. Same L2 — no routing, **no firewall
  carve-out, no widened attack surface**.
- `mikrotik-infra/configs/fleet.yaml` forward chain already accepts
  `mgmt -> prd` and `mgmt -> dev` (state new, unrestricted:
  `"mgmt -> prd (kubectl/talosctl)"`). So probes toward GIKS (`10.10.10.20` /
  `10.10.15.20`) and the SMS appliances (`.9` per env) also need no change.

**No network or firewall change is required anywhere in this work.**

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Human-clickable only.** A card is earned by a URL a person opens in a browser. | Keeps the dashboard a *launcher*. Data-plane endpoints (`giks-db-*`, `w1-db-*`, `s3-*:9000`, `gh-*`, `hc-*`, iperf3, NTP) stay documented in `wiki/docs/architecture/hostnames.md`, which is the inventory surface. |
| D2 | **No `siteMonitor` on Pocket-ID ForwardAuth-gated cluster UIs.** | A probe against a gated URL gets `302 → id-*.w1.lv` and resolves green. That proves traefik-admin routed and Pocket-ID answered — *not* that Grafana is alive. A crashed Grafana would show green. No false green is worth more than a decorative dot. |
| D3 | **`siteMonitor` IS kept for apps that serve their own login page** (GIKS AdminApp, SMS-gateway console) and for every unauthenticated NAS-plane service. | Here a `200` is served by the application itself, so it genuinely proves the app is up. The distinction from D2 is the *identity of the responder*, not the presence of auth. |
| D4 | **No Kubernetes auto-discovery** (`kubernetes.yaml` stays disabled). | Matches the declarative-only doctrine that also refuses the Docker socket. It would need a long-lived read-only SA token per cluster, and a silently-lapsed 90-day SA token is exactly what cost 14 days of cluster-agent blindness in Aug 2026. The file's comment is rewritten from "not yet" to "by choice, and here is why". |
| D5 | **Network devices get link-only cards, no widget.** | Plain `http://` links need no REST API on the device. The 2026-04-24 removal of the `mikrotik` widget (which required `/ip service www` + mgmt→base firewall carve-outs) **stands untouched**. The file comment must say so explicitly so a future reader does not "restore" the widget. |
| D6 | **GitHub repo links move from cards to bookmarks.** | 14 repos as cards would swamp the launcher. The side rail is the right density for links with no health state. |

## Target structure

| Group | Cards | Probe | Notes |
|---|---|---|---|
| Infrastructure | 7 | yes | existing AMT node widgets (prd/dev), amtctl, MeshCentral, NAS Traefik, stress — **plus `pxe.w1.lv` (new)** |
| Kubernetes · prd | 7 | no (D2) | Grafana, Prometheus, Alertmanager, Longhorn, Hubble, Traefik-admin, Pocket-ID |
| Kubernetes · dev | 7 | no (D2) | same seven |
| Applications | 6 | yes (D3) | GIKS AdminApp prd + dev, GIKS member portal dev, `admin.giks.lv`, SMS gateway prd + dev consoles |
| NAS | 1 | yes | TrueNAS + existing `truenas` widget |
| Storage | 2 | yes | MinIO prd + dev consoles |
| Network | 6 | no (D5) | router, sw-data, sw-mgmt, wifi, lte, egress |
| Docs | 1 | yes | Internal wiki |

Roughly **19 → 37 cards**.

### Removals

- **`Media (planned)` group** — two dead `href: '#'` cards, plus its entry in the
  `settings.yaml` `layout:` block. `plex` / `qbittorrent` remain
  `enabled: false` in `config/apps.yaml`; when the media tier lands it gets real
  cards. A card that links to `#` is not information.
- **MinIO S3 data-plane cards** (`s3-prd`, `s3-dev`) — per D1, an API endpoint on
  `:9000` is not something a human clicks. Stays in `hostnames.md`.
- **`Beelink support` bookmark** — RMA closed.

### Bookmarks (rebuilt)

- **Repos** — all 14 GitHub repos in the workspace.
- **Ops consoles** — GitHub, Cloudflare, Doppler, Backblaze B2,
  **AWS SES (pinned to `eu-north-1`)**. The region is pinned in the URL because
  the wrong SES region returns an *empty* result rather than an error, which
  reads as "all clean".
- **Vendor docs** — existing four, plus Talos, Flux, CNPG, Cilium, Longhorn.

## Dead-name cleanup (separate PRs)

`traefik-pub-prd.w1.lv` and `traefik-pub-dev.w1.lv` both have MikroTik DNS
records and **neither has ever had a live route**:

- Verified on both live clusters: zero IngressRoutes matching `traefik-pub-*`.
- The only occurrence in `kube-infra` is a **commented-out placeholder** at
  `flux-cd/infrastructure/configs/per-cluster/prd/admin-plane-ingress.yaml`,
  labelled *"Kept commented until traefik-public service exposes its api port"* —
  never wired up since Day 1. `traefik-pub-dev` has no route at all, not even a
  commented one.
- No open PR would add them (kube-infra's open PRs are 8 Renovate + 1 docs).

They resolve to a healthy Traefik that has no matching rule, so they return a
404 from a working proxy — the most misleading possible failure.

Actions, per the standing "remove dead legacy code, don't document it" rule:

1. `mikrotik-infra` — drop both records from `configs/dns.yaml`, re-render via
   `tools/render_fleet.py`, sync router.
2. `kube-infra` — delete the ~22-line commented placeholder block.
3. File a `kube-infra` issue for the **real** gap this exposed:
   `traefik-public` is the instance serving public traffic (`giks.lv`,
   `www.giks.lv`) and its dashboard is reachable only by `kubectl port-forward`,
   while `traefik-admin` and `traefik-internal` both have OIDC-gated dashboards.
   The record was premature; the gap is real. Tracked, not silently dropped.

## Doc mirror (same review cycle)

Required by the ⚠ WIKI MIRROR rule in `services.yaml` and `dns.yaml`:

- `wiki/docs/reference/links.md` — rebuild the dashboards table (all cluster
  URLs, SMS, GIKS, network, egress); extend the repo table from 6 to 14.
- `wiki/docs/architecture/hostnames.md` — verify the admin/data/infra plane
  tables carry every name, including the ones deliberately *not* carded.
- `wiki/tools/deploy.sh --verify` after.

Out of scope, noted for its own commit: `~/github/CLAUDE.md` (symlink into
`dotfiles`) lists 8 repos where 14 exist — `AdTracker`, `GIKS`, `GIKS-v1`,
`latvian-registers`, `rss-website-monitor`, `web-tracker` are absent.

## Verification

1. `manage.sh` phase `apps --apply` uploads the six Homepage YAMLs and restarts
   the container (pre-upload ordering per the compose comment).
2. Load `https://home.w1.lv/` — confirm group order matches `settings.yaml`
   `layout:` (Homepage auto-orders anything absent from that block).
3. Every probed card resolves green; **no** cluster card shows a status dot
   (absence is the expected state under D2).
4. Click-through spot check: one card per group.
5. `wiki/tools/deploy.sh --verify`.

## Risks

- **Group order** — `settings.yaml` `layout:` pins order and columns. Adding
  groups to `services.yaml` without adding them there yields a scrambled
  dashboard. Both files change together.
- **Card volume** — 37 cards is a dense page. If it reads as cluttered, the
  fallback is collapsing `Kubernetes · prd` / `· dev` into one group with
  env-suffixed card names, not dropping services.
- **`admin.giks.lv`** currently serves the maintenance overlay via cloudflared,
  not the application. The card description must say so, or it will read as a
  broken app. (v2 `prd` is pre-production; production is v1.)
