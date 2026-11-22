# cluster-agent on the NAS, history

> Moved verbatim from CLAUDE.md § cluster-agent: moved to its own repo (2026-10-17) on 2026-11-22, truenas-infra#233.

### cluster-agent: moved to its own repo (2026-10-17)

The cluster-agent, the LLM SRE assistant that writes a daily digest of each cluster's alerts and
logs, lives in [`guntars-rakitko/cluster-agent`](https://github.com/guntars-rakitko/cluster-agent)
since 2026-10-17, with its git history and design documents. Its `CLAUDE.md` is now the guide: the
digest pipeline, the daily summary, the labels, the settings and secrets, and how a change is
deployed. It runs in each cluster, deployed by kube-infra `flux-cd/apps/cluster-agent`.

It was a NAS app here from 2026-05 to 2026-10-17. Its NAS side was retired on 2026-10-17: the
TrueNAS app, its directories, the `cluster-agent` bucket on both MinIO instances, the Doppler
project `cluster-agent` and the NAS-era ServiceAccount tokens are all gone. The NAS-era
operations this section held until then are in git: `git show ad7ec0d:CLAUDE.md`.

---
