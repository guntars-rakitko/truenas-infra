# cluster-agent

LLM-driven SRE assistant for the homelab. Reads alerts / logs / state
from both K8s clusters, produces actionable GH issues, triages Renovate
PRs, runs scheduled backup verification + doctrine compliance scans.

## Design + spec

See [`truenas-infra/docs/superpowers/specs/2026-05-23-cluster-agent-design.md`](../../docs/superpowers/specs/2026-05-23-cluster-agent-design.md).

## Runbook

`wiki/docs/runbooks/cluster-agent-runbook.md` (lands with Task 22 of the P0 plan).

## P0 phase status

| Phase | Status |
|---|---|
| P0 — Foundation | in progress (Tasks 1-7 done — RBAC + Doppler keys + MinIO bucket + compose) |
| P1 — Mode A on dev sandbox repo | not started (target: June 15+) |
| P2 — Mode A on dev+prd, real issues | not started |
| P3-P7 | not started |
