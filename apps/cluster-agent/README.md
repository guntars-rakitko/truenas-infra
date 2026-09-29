# cluster-agent

LLM-driven SRE assistant for the homelab. Reads alerts / logs / state
from both K8s clusters, produces actionable GH issues, triages Renovate
PRs, runs scheduled backup verification + doctrine compliance scans.

## Design + spec

See [`truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md`](../../docs/superpowers/specs/2026-05-23-cluster-agent-design.md).

## Dependencies

`pyproject.toml` → `uv.lock` → `requirements.lock.txt` → the container's
`/venv`. The compose startup script installs exactly
`requirements.lock.txt` (`pip install --require-hashes --only-binary=:all:`)
and rebuilds `/venv` only when that file's sha256 (or the image's Python
minor) changes. After any dependency change, from this directory:

```sh
uv lock                      # or: uv lock --upgrade-package <name>
uv export --frozen --no-dev --no-emit-project --format requirements-txt > requirements.lock.txt
uv run --extra dev pytest    # tests/test_requirements_lock.py catches a stale export
```

`./manage.sh phase apps` also runs that `uv export` and refuses to deploy
(before any upload) if the committed file differs, so a Renovate lock PR
merged without the re-export fails loudly instead of shipping the old pins.

A lock-only change does not recreate the container. After
`./manage.sh phase apps --only cluster-agent --apply`, run
`sudo docker restart cluster-agent`. Details: truenas-infra `CLAUDE.md`
§ cluster-agent ops, "Runtime deps come from `uv.lock`".

## Runbook

`wiki/docs/runbooks/cluster-agent-runbook.md` (lands with Task 22 of the P0 plan).

## P0 phase status

| Phase | Status |
|---|---|
| P0 — Foundation | in progress (Tasks 1-7 done — RBAC + Doppler keys + MinIO bucket + compose) |
| P1 — Mode A on dev sandbox repo | not started (target: June 15+) |
| P2 — Mode A on dev+prd, real issues | not started |
| P3-P7 | not started |
