"""Which cluster each of the agent's keys points at.

`cluster` throughout the agent is a KEY — `dev` or `prd` — not a cluster: the
Doppler kubeconfig a run uses (KUBECONFIG_DEV / KUBECONFIG_PRD), the scheduler
job, the dedup scope, the Grafana annotation tag. The cluster behind each key
is named here, as kube-infra names it (cluster-env `CLUSTER_NAME`, which Alloy
stamps on every Loki stream as `cluster`), and that name is the GitHub label on
every finding and digest-summary issue the key files.

⚠ A key's cluster changes with its kubeconfig, and only with it. At the MS-A2
cutover each key moves to its msa2 cluster (kube-infra cutover plan 2026-09-28,
A5 / B5; Cutover inventory row 16): `scripts/render-cluster-agent-kubeconfigs.sh`
re-mints that key's kubeconfig against the new cluster, and the key's name here
changes in the same cycle. One key at a time: in a mixed period one key can be
msa2 while the other is still a Q170S1 cluster. A rollback reverts both.

When a key's name changes, keep its old name in PREVIOUS_NAMES: the first run
under the new name then closes the last digest-summary issue filed under the old
one (`_close_previous_summaries`), instead of leaving it open for good.
"""
from __future__ import annotations

# key -> the cluster it points at today.
CLUSTER_NAMES: dict[str, str] = {
    "dev": "kub-dev",
    "prd": "msa2-prd",  # since prd's MS-A2 cutover (kube-infra cutover plan A5)
}

# key -> names it had before, newest first. Emptied at the Q170S1 teardown.
PREVIOUS_NAMES: dict[str, tuple[str, ...]] = {
    "dev": (),
    "prd": ("kub-prd",),
}


def cluster_name(key: str) -> str:
    """The cluster behind `key`, for labels. A key with no cluster of its own
    (the schema's `nas`, `global`) is its own name."""
    return CLUSTER_NAMES.get(key, key)


def all_names(key: str) -> tuple[str, ...]:
    """The key's current name, then every name it had before."""
    return (cluster_name(key), *PREVIOUS_NAMES.get(key, ()))
