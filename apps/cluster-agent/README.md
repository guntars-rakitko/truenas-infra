# cluster-agent

LLM-driven SRE assistant for the homelab. One mode runs: **Mode A, the
daily digest**. Once a day per cluster (06:00 / 06:01 Europe/Riga) it reads
24 h of alerts, Loki log patterns and the cluster's known events (boots,
apiserver restarts, Helm upgrades, restore drills), makes ONE LLM call, files
0-N findings as issues in `kube-infra` and a full summary in
`cluster-agent-digest`. The other modes of the design (Renovate PR triage,
backup verification, doctrine scans) were never built.

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

[`wiki/docs/runbooks/cluster-agent-runbook.md`](https://wiki.w1.lv/runbooks/cluster-agent-runbook/).
Operations (deploy, tokens, Doppler keys, the digest's architecture):
truenas-infra `CLAUDE.md` § cluster-agent ops.

## Status

Mode A has run on both clusters since 2026-05-26 (P3: log mining; findings
graduated to `kube-infra` on 2026-07-06). (A P0-era phase table stood here
until 2026-10-10, still saying "P1 not started".)
