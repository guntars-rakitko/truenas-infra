# Secrets reference

> Moved verbatim from CLAUDE.md § Secrets — Doppler `infrastructure/ops` on 2026-11-22, truenas-infra#233.

## Secrets — Doppler `infrastructure/ops`

Credentials live in Doppler (project `infrastructure`, config `ops`).
`manage.sh` fetches the keys it needs at startup via per-key
`doppler secrets get --plain` calls; per-app secrets used by the apps
deploy flow are fetched at deploy time by `_load_doppler_for_app` in
`src/truenas_infra/modules/apps.py`.

**Per-script keys** (read by `manage.sh` top-level + Python config):

- `TRUENAS_HOST`, `TRUENAS_API_KEY`, `TRUENAS_VERIFY_SSL`
- `TRUENAS_NUT_MONPWD` — NUT `upsmon` (monitor) user password. TrueNAS writes it into upsd.users at service start, and it is **required**: without it upsd exits silently (`config/services.yaml` § nut `monuser`). ⚠ **It is a MASTER credential, not a read-only one.** TrueNAS renders the account as `upsmon master`, which NUT turns into LOGIN + MASTER + **FSD**. FSD on `apc1` runs the Path B orchestrator, which powers off every node and then the NAS. **Its only user is the NAS's own upsmon.** No remote client holds it: the clusters' nut-exporter reads anonymously since 2026-09-28 (kube-infra `flux-cd/infrastructure/configs/base/nut-exporter.yaml` § Auth), and the K8s nodes stopped being NUT clients on 2026-06-02 (Path B). The cluster copy (Doppler `infrastructure/shr` → `SHARED_TRUENAS_NUT_MONPWD`, rendered as DopplerSecret `nut-credentials` on all four clusters) is retired. Every cluster's `shr` token could read it. ⚠ Never put this password, or any copy of it, into `shr` or a cluster again. A remote NUT client that needs a login gets its own `nut.extra_users` entry.
- `TRUENAS_NUT_ADMINPWD` — NUT `upsadmin` user password (operator-side, used for `upsrw` + `upscmd` writes). Role: **SET + INSTCMD ALL**, configured via TrueNAS UI → Services → UPS → Edit → **"Extra Users"** field (pasted from the doctrine block in `wiki/docs/runbooks/ups-operations.md`). Live since 2026-05-28. Apple Passwords mirror: `TrueNAS NUT upsadmin`. Add to **NOPASSWD allowlist** when extending NUT automation — see SSH section below.
- `SHARED_CLOUDFLARE_API_TOKEN` (aliased to `CLOUDFLARE_API_TOKEN` after fetch — CloudFlare SDK convention)

**Emergency / break-glass credentials** (operator-typed-only paths
where Apple Passwords is the canonical store; Doppler holds the
machine-readable copy):

- **TrueNAS admin (Web UI Shell + emergency console)** — username +
  password rotated 2026-05-12 (closes
  [kube-infra#94](https://github.com/guntars-rakitko/kube-infra/issues/94)).
  No longer shares value with `AMT_PASSWORD` (the anti-pattern that
  motivated the rotation; `AMT_PASSWORD` itself was deleted 2026-10-06,
  kube-infra#1443 § 3). Lives in:
    * Doppler `infrastructure/ops` → `TRUENAS_ADMIN_USER` +
      `TRUENAS_ADMIN_PASSWORD` (machine-readable copy for future DR
      scripts + as the canonical source the operator pulls from)
    * Apple Passwords → `TrueNAS root` entry (operator-typed mirror for
      browser autofill on the Web UI Shell login). Update both in
      lockstep on rotation.
  Note: SSH password auth is disabled at the sshd daemon (key-only),
  so this credential is for **Web UI Shell** access — for any
  destructive operation that the API can't do (see
  `wiki/docs/runbooks/_archive/rotate-amt-credentials.md` for an example of
  using the API+cronjob workaround to call `rm` as root).
- **SSH service account name** — `svc-automation` (the user the API
  key is bound to; also the SSH username for ad-hoc operator shell
  work, when configured). It's a convention, not a credential —
  documented here, not stored in Doppler. The API key is in Doppler
  as `TRUENAS_API_KEY`.

### SSH + sudo on NAS — what works without operator typing a password

Verified 2026-05-28. Useful to know up-front so you don't waste cycles
debugging "permission denied" responses:

**SSH login** — `ssh truenas_admin@10.10.5.10` works via SSH
**publickey** for any client whose key is in the user's authorized_keys.
The operator's laptop key (`gunrak@mac-giks-migration-20260419`
ED25519) is loaded. `BatchMode=yes` (which disables interactive
password fallback) succeeds → publickey is the only method the server
will accept for this user. Lands as `uid=950(truenas_admin)
groups=950(truenas_admin),544(builtin_administrators)`. `whoami`,
`hostname`, `id`, file reads in `/home/truenas_admin`, anything not
needing root — all work non-interactively.

**`sudo` from a non-interactive SSH session — REQUIRES password** by
default. `truenas_admin` has TrueNAS UI **"Allowed sudo commands: ALL"**
(broad sudoers entry) BUT **"Allowed Sudo Commands (No Password):"
empty** by default. So any `ssh truenas_admin@... 'sudo …'` without a
TTY (no `-t`) returns `sudo: a password is required`. Even `sudo -n`
(non-interactive) fails. To run sudo from automation, the operator
must add specific command paths to the "No Password" field via the
TrueNAS UI → Credentials → Users → `truenas_admin` → Edit.

The API does the same, and it is what worked on 2026-10-17, when a UI save of
this field did not take. Look up the user's id, write the WHOLE new list (it
replaces the field), then read it back:

```sh
# In an SSH session on the NAS (ssh truenas_admin@10.10.5.10); midclt is NOPASSWD.
sudo midclt call user.query '[["username","=","truenas_admin"]]' '{"select":["id"]}'
sudo midclt call user.update <id> '{"sudo_commands_nopasswd": [...]}' >/dev/null
sudo midclt call user.query '[["username","=","truenas_admin"]]' '{"select":["sudo_commands_nopasswd"]}'
```

⚠ Keep the `>/dev/null`: `user.update` prints the whole user record, password
hashes included, so its stdout must never reach a terminal or a log. (The
recipe's first version, in truenas-infra#211, showed the command without the
redirect and the warning only in the text.)

**Granted NOPASSWD sudo commands** (in TrueNAS UI under "Allowed Sudo
Commands (No Password)"):

```
/usr/bin/midclt
/usr/bin/upscmd
/usr/bin/upsrw
```

(Until 2026-10-17 the list also held `/usr/bin/docker restart cluster-agent`
and `/usr/bin/docker exec cluster-agent *`. Both were removed on 2026-10-17,
when the agent left the NAS.)

This lets passwordless:
- **midclt** — any TrueNAS API call from a non-TTY SSH session. Used
  for `ups.config` reads, `service.control`, etc. Without this,
  every API-driven check requires a TTY + password prompt.
- **upscmd / upsrw** — NUT instcmd + variable-write operations
  against the local UPS (e.g. `upscmd test.battery.start.quick`,
  `upsrw -s ups.delay.shutdown=300`). Added 2026-05-28 alongside
  the `upsadmin` NUT user — gives the operator's laptop end-to-end
  ability to tune UPS HID thresholds via Doppler-driven scripts
  without typing the upsadmin password each time.

Scope is intentionally tight — only the specific binaries listed,
nothing else. To extend, add a new line per command — never blanket
`/usr/bin/docker *` or wildcard everything.

**What is NOT granted** (still requires interactive password):

- `sudo docker …` for any container (minio-prd, minio-dev, traefik, wiki, etc.)
- `sudo` for file ops outside docker (`vi`, `systemctl`, package install, etc.)
- `sudo` for inspecting any app's source/data

For those, fall back to `ssh -t truenas_admin@... 'sudo …'` from the
operator's terminal — TTY lets sudo prompt for password.

**Future expansion pattern:** when adding a new app that needs
automation-driven docker ops, add a corresponding NOPASSWD line for
`/usr/bin/docker restart <app>` + `/usr/bin/docker exec <app> *`. Same
principle as the per-app Doppler key separation: minimum scope, no
wildcards.

**Per-app keys** (`_DOPPLER_KEYS_PER_APP` in `modules/apps.py`):

- `minio-prd` → `MINIO_ROOT_USER_PRD`, `MINIO_ROOT_PASSWORD_PRD`, `MINIO_KMS_SECRET_KEY_PRD`, `MINIO_AISTOR_LICENSE`
- `minio-dev` → `MINIO_ROOT_USER_DEV`, `MINIO_ROOT_PASSWORD_DEV`, `MINIO_KMS_SECRET_KEY_DEV`, `MINIO_AISTOR_LICENSE` (one shared, org-scoped license key)

**Inspect / edit:**

```sh
# Show all NAS-related keys
doppler secrets --project infrastructure --config ops --only-names | grep -E "TRUENAS_|MINIO_|SHARED_CLOUDFLARE"

# Get one value (revealed)
doppler secrets get TRUENAS_API_KEY --project infrastructure --config ops --plain

# Set a value
doppler secrets set TRUENAS_API_KEY=newvalue --project infrastructure --config ops
```

**Recovery if Doppler unreachable:** the operator-side Phase 1 backup
(age-encrypted tarball in iCloud + MinIO `disaster-recovery` bucket)
holds the same values. See [`kube-infra/docs/disaster-recovery.md`](https://github.com/guntars-rakitko/kube-infra/blob/main/docs/disaster-recovery.md).
Migration tracking: kube-infra #92.

---
