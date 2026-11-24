# Wiki sync reference

> Moved verbatim from CLAUDE.md § Wiki maintenance on 2026-11-22, truenas-infra#233.

## Wiki maintenance

The homelab wiki at https://wiki.w1.lv/ contains **hand-written topic
pages** that synthesize data across repos. They do not update
automatically. When you change any of the sources below in this repo,
edit the matching wiki page in the same commit set.

| Change in this repo | Update in `wiki/` |
|---|---|
| `CLAUDE.md` (this file) | _Auto-synced_ — `sync-repos.sh` pulls `truenas-infra/CLAUDE.md` → `docs/projects/truenas-infra.md` |
| `config/network.yaml` (NICs, sub-IPs, hostname) | `docs/architecture/ip-plan.md` (NAS static allocations table) |
| `config/apps.yaml` (new Custom App) | `docs/architecture/hostnames.md`, `docs/reference/links.md` |
| `apps/traefik/routes.yaml` (new admin UI route) | `docs/architecture/hostnames.md` (admin-plane table), `docs/architecture/tls-split-horizon.md` |
| `apps/traefik/routes.yaml` (a `devapi` data-plane route) | `docs/architecture/hostnames.md` (data-plane table), `docs/architecture/tls-split-horizon.md`, `docs/architecture/ip-plan.md` (the VLAN IP's row) |
| `config/tls.yaml` (cert config change) | `docs/architecture/tls-split-horizon.md` |
| `docs/*.md` (any runbook) | _Auto-synced_ — see `wiki/sync-map.yaml`. ⚠ Delete or rename a mapped file only together with its wiki mapping, `.gitignore` line and `.pages` entry, and merge the wiki side first: a deleted source with a live mapping makes `sync_repos.py` fail hard. |
| `docs/{bios-apply-pxe-setup,pxe-operator,talos-updater-setup}.md` | **Deleted 2026-10-06**, each with its wiki mapping: PXE's three retirement tombstones (PXE was retired 2026-09-23). `bios-apply-pxe-setup` went when bios-config was archived; `pxe-operator` and `talos-updater-setup` once the q170s1 estate left kube-infra (kube-infra#1514), since the Q170S1 re-image path was the last thing they described. The wiki keeps each last text in `docs/runbooks/_archive/` (`bios-apply-pxe-setup.md`, `pxe-operator.md`, `talos-pxe-updater.md`); the sources are in git, `git show 2f0f76b:docs/bios-apply-pxe-setup.md` and `git show c788fc1:docs/<file>` for the other two. With `apps/pxe/` and `config/talos.yaml` deleted 2026-09-26 (pool-rebuild plan Task 9a), no PXE file is left in this repo, only comments recording the retirement. |
| `docs/verification.md` | _Auto-synced_ → `docs/reference/verification-matrix.md` |
| Doppler `infrastructure/ops` (add/remove key) | `docs/reference/env-vars.md`, possibly `docs/architecture/secrets-flow.md` |
| "Policy for adding new services" section (above) | `docs/architecture/tls-split-horizon.md` decision tree |

**Deploy the wiki** after the edit:

```sh
cd ~/github/wiki && ./tools/deploy.sh --verify
```

The verify matrix (`./manage.sh phase verify`) catches structural drift
(DNS resolution, TLS SAN coverage, cert expiry, app state, snapshot tasks). Its DNS check
vets **every record the router declares**: it reads mikrotik-infra
`configs/dns.yaml` at `origin/main` from the sibling clone
(`~/github/mikrotik-infra`, override `MIKROTIK_INFRA_DIR` / `MIKROTIK_DNS_REF`)
and digs each name against `10.10.0.1`. `git fetch` that clone first — the
check prints the commit and date it read. A missing clone or ref is a
**failed** check, never a skip. It does **not** catch prose drift in the wiki
(stale IPs in commentary, outdated VLAN descriptions) — that's operator
responsibility.

> ⚠ **There is no DNS file in this repo since 2026-09-26.** A hand-mirrored
> `config/dns.yaml` used to feed that check. It held 19 records to the
> router's 47 (no msa2-*, no cluster admin UIs, no giks-db / w1-db, no
> sms-gw), so the matrix vetted a stale subset, and each MS-A2 re-address
> (kube-infra msa2 plan § Cutover inventory row 12) would have needed two
> edits. It was deleted rather than re-synced. A new DNS record goes in
> mikrotik-infra `configs/dns.yaml` only (+ its wiki `hostnames.md` row).

---
