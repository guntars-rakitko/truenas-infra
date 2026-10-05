"""Which cluster each of the agent's keys points at.

`cluster` throughout the agent is a KEY — `dev` or `prd` — not a cluster: the
Doppler kubeconfig a run uses (KUBECONFIG_DEV / KUBECONFIG_PRD), the scheduler
job, the dedup scope, the Grafana annotation tag. The cluster behind each key
is named here, as kube-infra names it (cluster-env `CLUSTER_NAME`, which Alloy
stamps on every Loki stream as `cluster`), and that name is the GitHub label on
every finding and digest-summary issue the key files.

⚠ A key's cluster changes with its kubeconfig, and only with it:
`scripts/render-cluster-agent-kubeconfigs.sh` re-mints the key's kubeconfig
against the new cluster, and the key's name here changes in the same cycle. Both
keys moved to their msa2 clusters at the MS-A2 cutover (kube-infra cutover plan
2026-09-28, A5 / B5); the Q170S1 clusters they pointed at before are retired
(kube-infra#1443).

When a key's name changes, keep its old name in PREVIOUS_NAMES: the first run
under the new name then closes the last digest-summary issue filed under the old
one (`_close_previous_summaries`), instead of leaving it open for good. Once that
run has happened the entry has done its job; empty it.

GRAFANA_URLS is the same kind of fact: where the key's cluster serves Grafana to
this agent (kube-infra#1366). It is kube-infra's `grafana-nas` Service
(flux-cd/infrastructure/configs/base/grafana-nas-nodeport.yaml): NodePort 30030
on the node's MGMT address, admitted from the NAS's 10.10.5.10 only. The NAS
reaches a node address on its own /24 directly; it cannot reach a BGP LB IP
there (tools/grafana.py). It moves with the kubeconfig, in the same cycle as
CLUSTER_NAMES, and so does the key's Grafana token (Doppler
GRAFANA_SA_TOKEN_<KEY>): a service account in THAT cluster's Grafana.
"""
from __future__ import annotations

# key -> the cluster it points at today.
CLUSTER_NAMES: dict[str, str] = {
    "dev": "msa2-dev",  # since dev's MS-A2 cutover (kube-infra cutover plan B5)
    "prd": "msa2-prd",  # since prd's MS-A2 cutover (kube-infra cutover plan A5)
}

# key -> names it had before, newest first. Empty since the Q170S1 teardown:
# `kub-dev` / `kub-prd` closed their last digest-summary issues at the first runs
# after the cutover (2026-09-29), and no open one carries those labels.
PREVIOUS_NAMES: dict[str, tuple[str, ...]] = {}

# key -> base URL of that cluster's Grafana for this agent: the node's mgmt
# address (kube-infra talos-os/estates.yaml) and the `grafana-nas` NodePort.
# Plain HTTP: Grafana itself, no TLS in front (kube-infra accepts it for the
# one mgmt-VLAN hop; the Service's header says why). A key absent here has no
# Grafana, and post_annotation refuses it.
GRAFANA_URLS: dict[str, str] = {
    "dev": "http://10.10.5.12:30030",  # msa2-dev-01
    "prd": "http://10.10.5.11:30030",  # msa2-prd-01
}


def cluster_name(key: str) -> str:
    """The cluster behind `key`, for labels. A key with no cluster of its own
    (the schema's `nas`, `global`) is its own name."""
    return CLUSTER_NAMES.get(key, key)


def all_names(key: str) -> tuple[str, ...]:
    """The key's current name, then every name it had before."""
    return (cluster_name(key), *PREVIOUS_NAMES.get(key, ()))
