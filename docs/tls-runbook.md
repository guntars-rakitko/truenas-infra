# TLS runbook

The Let's Encrypt wildcard `*.w1.lv` is TrueNAS-ACME-managed with DNS-01
against CloudFlare. TrueNAS auto-renews; the hourly `tls-rotate`
cronjob propagates the new cert to `/mnt/tank/system/tls/` and
redeploys **traefik**. MinIO prd+dev pick the new files up on their own and
are **not** redeployed.

> ⚠ **This page had the two apps backwards until 2026-09-26.** It said Traefik
> hot-reloads and MinIO needs a redeploy. The 2026-09-14 renewal measured the
> opposite, because `tls-rotate.sh` redeployed nothing (a `set -e` bug killed
> it first) and so showed what each app does on its own:
>
> - **MinIO reloads by itself.** Both `s3-{prd,dev}.w1.lv:9000` served the new
>   cert on the 04:00:08 UTC blackbox sample, seconds after the 04:00 export,
>   with `probe_success=1` on every 30 s sample (no restart).
> - **Traefik does not.** Its file provider watches only `/etc/traefik/dynamic`
>   (where `routes.yaml` lives), not the cert in `/etc/traefik/certs`.
>   `wiki.w1.lv` served the pre-renewal cert for nine days, until the
>   2026-09-23 pool-rebuild restart reloaded it by accident
>   (`BlackboxCertExpiringWarn`, kube-infra #1252 / #1253).
>
> If an endpoint serves an old cert, see
> [§ Cert renewed but an endpoint still serves the old one](#cert-renewed-but-an-endpoint-still-serves-the-old-one).

## Routine ops

### Check current cert state

```sh
./manage.sh phase verify
# Expect every check green (currently 18: pool, datasets, 3 services,
# 1 per enabled app, cert, dns records, 6 TLS probes). Pay attention to:
#   - cert w1-wildcard: NN days left   (warning at <14, fail at <7)
#   - dns records: every record in mikrotik-infra configs/dns.yaml (origin/main
#     of ~/github/mikrotik-infra — fetch it first) resolves correctly
#   - tls <host>:<port> × 6 (nas, minio-prd, minio-dev, wiki, s3-prd:9000,
#     s3-dev:9000): issuer = the current LE intermediate (LE rotates them —
#     do not pin a name like "R12")
```

Or direct:

```sh
openssl s_client -connect nas.w1.lv:443 -servername nas.w1.lv </dev/null 2>/dev/null | \
  openssl x509 -noout -issuer -subject -dates
```

### Rotate the CloudFlare API token

CloudFlare tokens expire annually (or on demand). To rotate:

1. Create a new token at dash.cloudflare.com with the same scope:
   Zone:Zone:Read + Zone:DNS:Edit on **w1.lv AND giks.lv**. cert-manager's
   ClusterIssuer solves DNS-01 for both zones with this one token (kube-infra
   `flux-cd/infrastructure/configs/base/cert-manager-clusterissuer-letsencrypt.yaml`
   header), so a w1.lv-only token breaks the `*.giks.lv` renewals.
2. Edit Doppler `infrastructure/shr` → `SHARED_CLOUDFLARE_API_TOKEN`
   with the new value (cert-manager reads it via DopplerSecret).
   ⚠ **`manage.sh` does NOT read `shr`** — it fetches
   `SHARED_CLOUDFLARE_API_TOKEN` from **`infrastructure/ops`**, and that is
   what feeds the NAS ACME authenticator. In the Doppler dashboard, check
   whether the `ops` value is a secret reference to `shr`
   (`${infrastructure.shr.SHARED_CLOUDFLARE_API_TOKEN}`): if it is, the `shr`
   edit covers both; if it is a separate copy, set the same new value in
   `ops` too (overwriting a reference with a literal would turn it into a
   copy). Miss `ops` and the NAS ACME stays on the token you are about to
   revoke — renewal then fails silently until the wildcard nears expiry.
3. Run `./manage.sh phase tls --apply` — `ensure_acme_authenticator`
   detects the token drift and calls `acme.dns.authenticator.update`
   in place (no CSR/cert churn).
4. Revoke the old token in CloudFlare once confirmed.

All consumers read the value from Doppler, but from two configs: cert-manager
from `shr`, truenas-infra (`manage.sh` → NAS ACME) from `ops`. Unless `ops`
references `shr`, those are two copies to keep in lockstep.

### Force a renewal (no 60-day wait)

⚠ **Read [§ Let's Encrypt rate limit](#lets-encrypt-rate-limit-the-one-that-binds) first** — every re-issue here spends one of 5 per week, shared with the clusters. Do not do this during an msa2 cutover week.

TrueNAS auto-renews at `days_to_expiration < renew_days`. To exercise
the rotation pipeline without actually waiting:

```sh
# 1. Find the cert id
midclt call certificate.query '[["name","=","w1-wildcard"]]' | jq '.[0].id'

# 2. Bump renew_days above current days_left (e.g. if 89 days remain, use 90)
midclt call certificate.update <id> '{"renew_days": 90}'

# 3. Wait ~5 min for the TrueNAS daily renew-check to tick
#    (or force via: /var/lib/middleware/renew immediately — implementation-specific)
#
#    Observe:
midclt call certificate.query '[["id","=","<id>"]]' | jq '.[0].until'
#    — should show a new "until" date ~90 days out
#
#    /etc/certificates/w1-wildcard.crt mtime should update too.

# 4. Wait up to 60 min for the hourly tls-rotate cronjob, OR force it:
midclt call cronjob.query '[["description","=","tls-rotate"]]' | jq '.[0].id'
midclt call cronjob.run <id>

# 5. Verify propagation:
#    - /mnt/tank/system/tls/{fullchain,privkey,public,private}.* mtime updated
#    - /mnt/tank/system/tls/tls-rotate.log shows "cert rotated — redeploying
#      TLS consumers: traefik", "traefik: redeploy triggered", then "done"
#    - /mnt/tank/system/tls/.tls-redeploy-pending does NOT exist
#    - https://wiki.w1.lv/ (Traefik, redeployed) and https://s3-prd.w1.lv:9000
#      (MinIO direct, reloads on its own) show the new serial:
#        openssl s_client -connect wiki.w1.lv:443 -servername wiki.w1.lv \
#          </dev/null 2>/dev/null | openssl x509 -noout -serial -enddate

# 6. Restore renew_days to 30
midclt call certificate.update <id> '{"renew_days": 30}'
```

## Disaster scenarios

### Traefik is down — mgmt UIs return 5xx / connection refused

Symptom: `https://minio-prd.w1.lv/`, `https://minio-dev.w1.lv/`,
`https://wiki.w1.lv/` all unreachable; `https://nas.w1.lv/` still works.

TrueNAS UI is on `10.10.5.10:443` directly — never behind Traefik —
precisely so this scenario is recoverable. Log in to the UI, check the
Traefik Custom App state:

```
midclt call app.query '[["name","=","traefik"]]' | jq '.[0].state'
# If not RUNNING:
midclt call app.redeploy traefik
```

Root cause usually: cert file missing or syntactically wrong `routes.yaml`.
Check `/mnt/tank/system/apps-config/traefik/routes.yaml` and the cert
files under `/mnt/tank/system/tls/`.

### Cert expired and auto-renewal hasn't fired

⚠ **Read [§ Let's Encrypt rate limit](#lets-encrypt-rate-limit-the-one-that-binds) first** — every re-issue here spends one of 5 per week, shared with the clusters.

```sh
# Force re-issue — deletes the cert record + rebinds UI to default first
midclt call system.general.update '{"ui_certificate": 1}'
midclt call certificate.delete <wildcard-id> '{"job": true}'
./manage.sh phase tls --apply
```

Reissuing takes ~2 minutes for DNS-01. New cert id and the UI rebinds. The
next hourly `tls-rotate` (or a forced `cronjob.run`, § Force a renewal step 4)
exports it: MinIO picks it up on its own, and Traefik is redeployed onto it.

### Cert renewed but an endpoint still serves the old one

Symptom: `certificate.query` shows a fresh `until`, but
`openssl s_client … | openssl x509 -noout -serial -enddate` against
`wiki.w1.lv:443`, `minio-{prd,dev}.w1.lv:443` or `s3-{prd,dev}.w1.lv:9000`
still shows the old cert. TrueNAS renews at 30 days left, so a missed
redeploy surfaces as `BlackboxCertExpiringWarn` (<21 d) about nine days after
the renewal, and as `MinioCertExpiringSoon` (<14 d) about sixteen days after.

```sh
# 1. Did the export run, and what did the redeploys do?
tail -n 50 /mnt/tank/system/tls/tls-rotate.log
# 2. Anything still waiting for a retry? (one app name per line)
cat /mnt/tank/system/tls/.tls-redeploy-pending 2>/dev/null
# 3. Redeploy the stale app by hand (a few seconds of blip for traefik,
#    ~30 s of S3 for a MinIO)
midclt call app.redeploy traefik
```

A **MinIO** endpoint on an old cert is new information: MinIO reloaded on its
own at the 2026-09-14 renewal. Redeploy it by hand, then add it to
`TLS_CONSUMERS` in `apps/tls/tls-rotate.sh`. Also remove it from
`RELOADS_ON_ITS_OWN` in `tests/test_tls_rotate.py`, and record what changed
there (an AIStor upgrade is the likely suspect).

`tls-export.sh` reports a change exactly **once**. The next run finds the
pool copy already matching and exits 0. So `tls-rotate.sh` writes the apps it
still has to redeploy to `.tls-redeploy-pending` *before* the first redeploy,
drops each one that succeeds, and retries whatever is left every hour. The
script exits **3** while anything is still pending. A pending file that never
drains means `midclt call app.redeploy <app>` itself fails; run it by hand to
see why. ⚠ `midclt call app.redeploy` returns once the job is *queued*, so
"redeploy triggered" does not prove the new container came up. Check the
served serial.

### CloudFlare API token revoked or expired

Symptom: `phase tls --apply` fails with 401/403 during authenticator
registration. Replacement per "Rotate the CloudFlare API token" above.

### DNS broken (MikroTik down, `nas.w1.lv` doesn't resolve)

The Python client refuses to connect because `TRUENAS_HOST=nas.w1.lv`
can't be resolved.

Fallback: temporarily flip `TRUENAS_HOST` to the raw IP in Doppler.
`manage.sh` always fetches credentials live from `infrastructure/ops`,
so editing those keys is the single source-of-truth path:

```sh
# Switch to raw IP (and disable strict TLS — IP doesn't match cert SAN)
doppler secrets set TRUENAS_HOST=10.10.5.10 TRUENAS_VERIFY_SSL=false \
  --project infrastructure --config ops

# Run the operation as normal — manage.sh picks up the override.
./manage.sh phase tls --apply

# Restore once DNS is back.
doppler secrets set TRUENAS_HOST=nas.w1.lv TRUENAS_VERIFY_SSL=true \
  --project infrastructure --config ops
```

Doppler's audit log captures both edits, which is cleaner than a
gitignored `.env` that no one sees.

### Staging → Production switch (ever need to re-do it)

```sh
# 1. Edit config/tls.yaml, set acme_directory_uri to staging
# 2. Apply to get a staging cert — verify pipeline end-to-end
./manage.sh phase tls --apply

# 3. Unbind UI from staging cert, delete, flip config to prod, re-apply
midclt call system.general.update '{"ui_certificate": 1}'
midclt call certificate.delete <staging-id> '{"job": true}'
#    Edit config/tls.yaml: acme_directory_uri back to prod
./manage.sh phase tls --apply
```

### Let's Encrypt rate limit: the one that binds

⚠ The limit that binds is **NOT** the 50/week per registered domain. It is
**5 new certificates per rolling week for the exact identifier set
`[*.w1.lv, w1.lv]`** — and the NAS shares that set with every cluster wildcard
Certificate (kube-infra CLAUDE.md § Let's Encrypt rate limit; the msa2 plan's
rule 4 counts it at 3 of 5). The NAS cert is invisible to
`kubectl get certificate`. Every forced renewal, re-issue or staging→prod flip
here spends one, and renewals count too (cert-manager has no ARI enabled, and
the NAS's TrueNAS ACME client is not known to use it). If it blows, wiki.w1.lv
fails first — the runbooks you would be recovering from.

- Use staging (~30k/week) for any rework.
- Do **NOT** force-renew or re-issue the NAS cert during an msa2 cutover week
  (kube-infra msa2 plan, cutover row 18).

## Why things are where they are

- **Cert is wildcard `*.w1.lv` (+ `w1.lv`)**: covers every current and
  future internal service without re-issuance. DNS-01 is the only ACME
  challenge that supports wildcards.
- **TrueNAS UI direct on `10.10.5.10:443`**: bootstrap path. If Traefik
  dies, we can still log in to repair it.
- **Traefik on `10.10.5.20:443` (new sub-IP)**: fronts the mgmt-plane
  web UIs (minio-prd/dev consoles + wiki — `apps/traefik/routes.yaml`).
  It has no dashboard (every Traefik dashboard was removed 2026-09-13).
  Hot-reloads `routes.yaml` edits, but **not** its cert. The cert is
  mounted from `/mnt/tank/system/tls` at `/etc/traefik/certs`, which the file
  provider does not watch, so `tls-rotate` redeploys it.
- **MinIO S3 direct on `10.10.{10,15}.10:9000`**: data plane. Native
  port kept — `:443` on data VLANs reserved for future services.
- **MinIO consoles on `10.10.5.10:{9001,9011}`**: bound to mgmt VLAN IP
  (distinct host ports) so prd/dev workloads literally can't route to
  the admin plane. Traefik routes `https://minio-{prd,dev}.w1.lv/` to
  these backends.
- **Hourly cronjob `tls-rotate`**: diffs `/etc/certificates/` vs pool
  copy by SHA-256; on change, copies (with both generic fullchain/
  privkey AND MinIO-conventional public.crt/private.key names) + calls
  `app.redeploy` on every app in `TLS_CONSUMERS`, which today is only
  `traefik`. A failed redeploy is retried hourly from `.tls-redeploy-pending`.
  `tests/test_tls_rotate.py` requires every enabled app whose compose mounts
  `/mnt/tank/system/tls` to be either in `TLS_CONSUMERS` or in
  `RELOADS_ON_ITS_OWN` with evidence (minio-prd, minio-dev). Traefik broke
  exactly this invariant. Blast radius per rotation: a few seconds of Traefik
  (wiki + MinIO consoles), about once every 60 days. S3 is untouched.
