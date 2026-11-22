# CLAUDE.md — truenas-infra

Guidance for Claude Code when working in this repository.

---

## Overview

API-driven TrueNAS configuration for a homelab NAS. Almost all configuration is stored in Git and applied through the TrueNAS API (JSON-RPC 2.0 over WebSocket), so the NAS can be rebuilt from scripts. **Two things are deliberately managed out-of-band in the UI** and are NOT reproduced by the tool: (1) the `truenas_admin` operator identity itself (its login + the "Allowed Sudo Commands (No Password)" allowlist that the automation depends on — see § SSH + sudo on NAS), and (2) a small set of NUT bits noted below (Extra Users, alert classes, the Init/Shutdown hook registration). A clean rebuild therefore re-runs the phases **and** re-applies those UI-managed pieces from their documented recipes.

The NAS serves the Kubernetes clusters defined in `guntars-rakitko/kube-infra` and sits on the network managed by `guntars-rakitko/mikrotik-infra`.

---

## File Structure

```
CLAUDE.md
manage.sh             # Top-of-file fetches secrets from Doppler ops
config/
  apps.yaml           # App registry (no secrets path; Doppler keys
                      # mapped per-app in modules/apps.py)
src/                  # Python CLI implementation
scripts/
  setup-minio-{buckets,users,lifecycle,encryption}.sh  # one-shot MinIO bootstrap
                                              # (buckets/encryption/lifecycle: guarded by
                                              #  tests/test_minio_setup_scripts.py)
```

---
