# Bootstrap — one-time UI steps

Scripts in this repo assume the NAS has:
- A static IP on NIC2 (`10.10.5.10/24`, VLAN 5 management).
- An API key for a non-root automation user.
- SSH enabled.

None of those can be self-bootstrapped by the scripts themselves (scripts need an API key to run, and the first key has to be minted somewhere). Do these steps **once**, in the TrueNAS web UI, before running `./manage.sh` for the first time.

Everything from phase 1 onward is idempotent and API-driven, so nothing here needs to be repeated if you reinstall — just rerun this checklist on the fresh install.

---

## Step 1 — Open the TrueNAS UI

1. Browser → `https://10.10.5.10/` (self-signed cert warning is expected; accept for now; we install real TLS in phase 3).
2. Log in as `root` with the password you set during install.

---

## Step 2 — Set admin password (if installer left a blank/default)

1. Top right → **System Settings → General** (or the account menu).
2. Set a strong `root` / `admin` password.
3. Store it in a password manager; it's only needed for recovery once automation takes over.

---

## Step 3 — Confirm NIC2 is static `10.10.5.10/24`

1. **Network → Interfaces**.
2. Find NIC2 (the one with 10.10.5.10 — the interface currently reaching you).
3. If it's DHCP: edit → uncheck DHCP → add alias `10.10.5.10/24` → **Test Changes** (the 60-second rollback safety window).
4. Still connected after 60 s → **Save Changes**. Otherwise do nothing; it'll roll back.
5. Set the default gateway to `10.10.0.1` (router) under **Network → Global Configuration**.
6. Set DNS to `10.10.0.1` (router handles internal DNS).

> NIC1 is intentionally left unconfigured at this point. Phase 2 will create the VLAN 10/15/20 sub-interfaces on it.

---

## Step 4 — Create the automation user + API key

1. **Credentials → Local Users → Add**.
2. Username: `svc-automation`
   - Full name: `Automation service account`
   - Password: generate a strong random string (it won't be used day-to-day; API key is what scripts use)
   - Home directory: `/home/svc-automation` (or `/nonexistent`)
   - Shell: `nologin`
   - Allowed sudo commands: none
3. **Credentials → Local Users → svc-automation → API Keys → Add**.
4. Name: `truenas-infra-scripts`
5. Copy the key **immediately** — it's shown only once.
6. Edit `.env.sops` and add:
   ```
   TRUENAS_HOST=10.10.5.10
   TRUENAS_API_KEY=<paste key here>
   ```
7. Save `.env.sops` (SOPS will re-encrypt on save — confirm the file is encrypted with `head -5 .env.sops`).

---

## Step 5 — Enable SSH service

Needed as a fallback in case the API becomes unreachable mid-phase.

1. **System Settings → Services → SSH → Configure**.
2. Enable **Log in as root with password**: **disabled** (password login off; use keys later).
3. Enable **Allow password authentication**: **enabled** for now (we'll switch to key-only in phase 1).
4. Toggle the service **ON** and set **Start Automatically** = yes.
5. Test: from your laptop,
   ```
   ssh svc-automation@10.10.5.10
   ```
   (Expects the password you set in Step 4. Swap to SSH keys in phase 1.)

---

## Step 6 — Accept EULA

If the installer didn't already prompt. There's a banner at the top of the UI if it's pending.

---

## Sanity check before running scripts

From your laptop:

```bash
# Encrypted env file looks encrypted
head -5 .env.sops | grep -q "ENC\["

# API key works
curl -sk -H "Authorization: Bearer $(sops decrypt .env.sops | grep API_KEY | cut -d= -f2)" \
  https://10.10.5.10/api/v2.0/system/info | jq .version
```

Expected: version string like `"25.10.3"`.

If that works, you're ready for phase 1 (`./manage.sh`).

---

## Recovery — if you lose management access

- Console access (keyboard + monitor) is the escape hatch. The Beelink ME Mini 2 has HDMI.
- From console: log in as `root` → network settings are plain text in `/etc/netplan/` or accessible via the TrueNAS config CLI (`midclt call interface.query`).
- `midclt call interface.commit` / `interface.checkin` are the safety net — a 60-second auto-rollback on every applied change.
