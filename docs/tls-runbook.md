# TLS runbook

The Let's Encrypt wildcard `*.w1.lv` is TrueNAS-ACME-managed with DNS-01
against CloudFlare. TrueNAS auto-renews; the hourly `tls-rotate`
cronjob propagates the new cert to `/mnt/tank/system/tls/` and
redeploys MinIO prd+dev. Traefik file-watches and hot-reloads on its
own.

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
#    - MinIO prd+dev redeployed (check `app.query` state)
#    - https://wiki.w1.lv/ (via Traefik) and https://s3-prd.w1.lv:9000
#      (MinIO direct) show the new cert fingerprint

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

Reissuing takes ~2 minutes for DNS-01. New cert id, UI rebinds, Traefik
hot-reloads, MinIO redeploys.

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
  Hot-reloads its cert from `/mnt/tank/system/tls` without restarting.
- **MinIO S3 direct on `10.10.{10,15}.10:9000`**: data plane. Native
  port kept — `:443` on data VLANs reserved for future services.
- **MinIO consoles on `10.10.5.10:{9001,9011}`**: bound to mgmt VLAN IP
  (distinct host ports) so prd/dev workloads literally can't route to
  the admin plane. Traefik routes `https://minio-{prd,dev}.w1.lv/` to
  these backends.
- **Hourly cronjob `tls-rotate`**: diffs `/etc/certificates/` vs pool
  copy by SHA-256; on change, copies (with both generic fullchain/
  privkey AND MinIO-conventional public.crt/private.key names) + calls
  `app.redeploy` on MinIO prd/dev. Traefik NOT redeployed — it
  file-watches.
