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
# Expect 20/20 green. Pay attention to:
#   - cert w1-wildcard: NN days left   (warning at <14, fail at <7)
#   - dns records: 14/14 resolve correctly
#   - tls <host>:<port> × 8: all LE R12 issuer
```

Or direct:

```sh
openssl s_client -connect nas.w1.lv:443 -servername nas.w1.lv </dev/null 2>/dev/null | \
  openssl x509 -noout -issuer -subject -dates
```

### See the Traefik dashboard

Browser: https://traefik-nas.w1.lv/dashboard/

(The trailing slash matters — Traefik redirects `/dashboard` → `/dashboard/` but some clients don't follow that.)

### Rotate the CloudFlare API token

CloudFlare tokens expire annually (or on demand). To rotate:

1. Create a new token at dash.cloudflare.com with the same scope
   (Zone:Zone:Read + Zone:DNS:Edit on w1.lv).
2. `sops .env.sops` → replace `CLOUDFLARE_API_TOKEN=...`.
3. Run `./manage.sh phase tls --apply` — `ensure_acme_authenticator`
   detects the token drift and calls `acme.dns.authenticator.update`
   in place (no CSR/cert churn).
4. Revoke the old token in CloudFlare once confirmed.

The same token is referenced from `kube-infra` for cert-manager —
update both SOPS copies before revoking.

### Force a renewal (no 60-day wait)

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
#    - https://mc.w1.lv/ shows the new cert fingerprint

# 6. Restore renew_days to 30
midclt call certificate.update <id> '{"renew_days": 30}'
```

## Disaster scenarios

### Traefik is down — mgmt UIs return 5xx / connection refused

Symptom: `https://mc.w1.lv/`, `https://minio-prd.w1.lv/`, `https://pxe.w1.lv/`
all unreachable; `https://nas.w1.lv/` still works.

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

Fallback: create a local `.env` in the repo root (gitignored) with:
```
TRUENAS_HOST=10.10.5.10
TRUENAS_API_KEY=<existing key>
TRUENAS_VERIFY_SSL=false
```
— `manage.sh` prefers `.env` over `.env.sops`. Full strict-TLS check
won't pass (IP doesn't match cert SAN) which is why we flip
`VERIFY_SSL=false`. Revert when DNS comes back.

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

LE staging has much higher rate limits than prod (~30k certs/week vs
50/week), so always use staging for bring-up rework.

## Why things are where they are

- **Cert is wildcard `*.w1.lv` (+ `w1.lv`)**: covers every current and
  future internal service without re-issuance. DNS-01 is the only ACME
  challenge that supports wildcards.
- **TrueNAS UI direct on `10.10.5.10:443`**: bootstrap path. If Traefik
  dies, we can still log in to repair it.
- **Traefik on `10.10.5.20:443` (new sub-IP)**: fronts the mgmt-plane
  web UIs (mc / pxe / minio-prd/dev console / traefik-nas itself).
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
