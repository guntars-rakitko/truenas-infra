"""Known events: what the cluster went through in the digest window (2026-10-10).

A planned change makes noise. A reboot restarts every pod, a Talos
config apply restarts kube-apiserver, a Helm upgrade or a rollout
replaces pods, and a restore drill starts a recovery Postgres that says
"starting up". Without knowing that, the LLM filed that noise as findings
(kube-infra #1437, #1509, #1535, the reboot findings of 2026-10-02..07,
and #1577: readiness failures during two sms-gateway rollouts). These
are read from the cluster itself and shown to the LLM and in the
digest-summary:

  node_boot           distinct `node_boot_time_seconds` values (Prometheus)
  apiserver_restart   distinct `process_start_time_seconds{job="apiserver"}`
                      values, except the one within 15 min after a boot
  helm_upgrade        HelmRelease `status.history` revisions deployed in the
                      window (Flux keeps the last few, newest first)
  deployment_rollout  ReplicaSets created in the window (kube-state-metrics'
                      `kube_replicaset_created`, Prometheus), and older ones
                      scaled up from 0 in it (a rollback to a kept
                      ReplicaSet creates none); one event per namespace for
                      the Deployments rolled out together, except a Helm
                      upgrade's own. What changed comes from the live
                      ReplicaSets when they are still there
  restore_drill       Jobs of a `*-restore-drill` CronJob that started in the
                      window, scheduled or created by hand; and the CronJob's
                      own `lastScheduleTime` when its Job is already gone (a
                      Job lives 24 h, `ttlSecondsAfterFinished: 86400`, and in
                      winter the 04:00Z digest starts as Monday's 04:00Z drill
                      Job is reaped)

The window starts `lead` (1 h) before the digest window: a boot at 02:55
makes noise after the 03:00 window start. Each source fails on its own,
recorded in `failures` like the log-mining queries: a missing source is
a coverage gap, never "nothing happened". So is an EMPTY answer where a
healthy cluster always has one (a node's boot time, kube-apiserver's
start, its HelmReleases, its ReplicaSets).

`touches` and `noise_window` say where and when an event makes noise; the
log-pattern miner (digest_aggregator.py) uses them to mark the patterns
that are that noise.
"""
from __future__ import annotations
import datetime as dt
import logging
import math
import re
from typing import Any, Callable

from ..schema import KnownEvent


log = logging.getLogger(__name__)

# Same series twice after a node-exporter or kube-state-metrics restart:
# `max by` folds the old and the new pod's copies. The node name comes from
# `node_uname_info`; `or on(instance)` keeps the boot time when that join
# finds nothing, so a missing info series costs the name, not the event.
_BOOT_QUERY = (
    "max by (instance, nodename) (node_boot_time_seconds"
    " * on(instance) group_left(nodename) max by (instance, nodename) (node_uname_info))"
    " or on(instance) max by (instance) (node_boot_time_seconds)"
)
_APISERVER_QUERY = 'max by (instance) (process_start_time_seconds{job="apiserver"})'
# Each ReplicaSet's creation time with its Deployment. A rollout creates a
# ReplicaSet, and a Deployment deletes its old ones past its
# revisionHistoryLimit: GIKS keeps 3, and msa2-dev rolled it out 8 times on
# 2026-10-10, so by the 03:00Z digest the live list had lost 4 of them.
# Prometheus still had all 8.
_OF_A_DEPLOYMENT = (
    " * on(namespace, replicaset) group_left(owner_name)"
    ' max by (namespace, replicaset, owner_name) (kube_replicaset_owner{owner_kind="Deployment"}))'
)
_ROLLOUT_QUERY = "max by (namespace, replicaset, owner_name) (kube_replicaset_created" + _OF_A_DEPLOYMENT
# A rollback to a template a kept ReplicaSet still has creates no
# ReplicaSet: the Deployment scales that one up from 0 again. A new
# ReplicaSet never showed a 0 before its first replica (7 days on both
# clusters, 2026-10-11), so the two sources do not overlap.
_SCALE_QUERY = "max by (namespace, replicaset, owner_name) (kube_replicaset_spec_replicas" + _OF_A_DEPLOYMENT

# A boot time read twice can differ by a second (btime is recomputed from
# the clock); two real boots are minutes apart at least.
_SAME_START = dt.timedelta(seconds=120)
# kube-apiserver starts ~50 s after its node boots (measured on both MS-A2
# boxes, 2026-10-01..10): that start is part of the boot, not an event.
_APISERVER_AFTER_BOOT = dt.timedelta(minutes=15)
# A Helm upgrade's own rollouts follow it by seconds (1-8 s on both
# clusters, 2026-10-10); its event already says what changed.
_ROLLOUT_AFTER_HELM = dt.timedelta(minutes=10)
# Deployments rolled out together are one event: GIKS's adminapp and
# jobserver come 1-2 s apart.
_ROLLOUT_TOGETHER = dt.timedelta(minutes=5)
_REVISION = "deployment.kubernetes.io/revision"
_HEX_TAG = re.compile(r"[0-9a-f]{13,}")
_STEP_SECONDS = 300

# The events that restart the code a cluster already runs. The others (an
# upgrade, a rollout, a drill) bring new code or config, which can be at
# fault when something dies after them.
RESTART_KINDS = frozenset({"node_boot", "apiserver_restart"})

# Where and when a known event makes noise. A restart stops the old
# processes first (a node drains before it reboots, prd's GIKS logged its
# shutdown errors 4 min before its 10-10 boot), so its noise starts up to
# 15 min before its time; anything else starts at its time. Pods restart,
# probes pass and operators reconcile within 45 min after.
NOISE_BEFORE = dt.timedelta(minutes=15)
NOISE_AFTER = dt.timedelta(minutes=45)


def touches(event: KnownEvent, namespace: str) -> bool:
    """A node boot or an apiserver restart touches every namespace; a Helm
    upgrade, a rollout or a drill the namespace it ran in. Its clients in
    other namespaces (a database's apps, Traefik in front of a rollout)
    are not touched: their noise stays unexplained."""
    return event.namespace is None or event.namespace == namespace


def noise_window(event: KnownEvent) -> tuple[dt.datetime, dt.datetime]:
    """From when to when the event makes noise."""
    before = NOISE_BEFORE if event.kind in RESTART_KINDS else dt.timedelta(0)
    return event.at - before, event.at + NOISE_AFTER


def _utc(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)


def _parse_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def _distinct_starts(resp: dict[str, Any], start: dt.datetime, end: dt.datetime
                     ) -> list[tuple[dict[str, str], dt.datetime]]:
    """Each series' distinct values that are a start time inside [start, end].

    A gauge whose value IS a start time (boot, process start) changes value
    once per restart, so its distinct values are the restarts; one older
    than the window is the uptime the window began with.
    """
    out: list[tuple[dict[str, str], dt.datetime]] = []
    for series in resp.get("data", {}).get("result", []):
        metric = series.get("metric", {})
        values: list[float] = []
        for _ts, raw in series.get("values", []):
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):     # Prometheus can answer "NaN" or "+Inf"
                values.append(value)
        kept: list[dt.datetime] = []
        for v in sorted(set(values)):
            t = _utc(v)
            if not (start <= t <= end):
                continue
            if kept and t - kept[-1] <= _SAME_START:
                continue
            kept.append(t)
        out.extend((metric, t) for t in kept)
    return out


def _range(prometheus_range_fn: Callable[..., dict[str, Any]], cluster: str, query: str,
           start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
    """The query's answer; no series at all raises (LookupError): both
    series exist on any cluster whose node-exporter and apiserver Prometheus
    scraped, so an empty answer is a blind spot, not a quiet day."""
    resp = prometheus_range_fn(
        cluster, query,
        start=str(int(start.timestamp())), end=str(int(end.timestamp())),
        step=f"{_STEP_SECONDS}s",
    )
    if not resp.get("data", {}).get("result"):
        raise LookupError(f"no series for {query.split('(', 2)[-1][:60]}")
    return resp


def _node_boots(resp: dict[str, Any], start: dt.datetime, end: dt.datetime) -> list[KnownEvent]:
    events = []
    for metric, t in _distinct_starts(resp, start, end):
        node = metric.get("nodename") or metric.get("instance") or "the node"
        events.append(KnownEvent(
            kind="node_boot", at=t,
            what=f"node {node} booted (every pod on it restarted)",
        ))
    return events


def _apiserver_restarts(resp: dict[str, Any], start: dt.datetime, end: dt.datetime,
                        boots: list[KnownEvent]) -> list[KnownEvent]:
    events = []
    for _metric, t in _distinct_starts(resp, start, end):
        if any(dt.timedelta(0) <= t - b.at <= _APISERVER_AFTER_BOOT for b in boots):
            continue
        events.append(KnownEvent(
            kind="apiserver_restart", at=t,
            what=("kube-apiserver restarted without a reboot (a Talos config "
                  "apply, a Kubernetes upgrade, or a crash)"),
        ))
    return events


def _helm_upgrades(items: list[dict[str, Any]], start: dt.datetime, end: dt.datetime
                   ) -> list[KnownEvent]:
    events = []
    for hr in items:
        meta = hr.get("metadata", {})
        name = meta.get("name", "?")
        history = hr.get("status", {}).get("history") or []
        for i, snap in enumerate(history):
            t = _parse_time(snap.get("lastDeployed"))
            if t is None or not (start <= t <= end):
                continue
            target = snap.get("namespace") or hr.get("spec", {}).get("targetNamespace")
            chart = snap.get("chartVersion", "?")
            prev = history[i + 1] if i + 1 < len(history) else None
            if prev is None:
                change = f"chart {chart}"
            elif prev.get("chartVersion") != chart:
                change = f"chart {prev.get('chartVersion', '?')} → {chart}"
            else:
                change = f"values changed, chart {chart}"
            status = snap.get("status", "?")
            events.append(KnownEvent(
                kind="helm_upgrade", at=t, namespace=target,
                what=(f"HelmRelease {meta.get('namespace', '?')}/{name} "
                      f"(into {target or '?'}) {snap.get('action') or 'upgrade'}: "
                      f"{change}, revision {snap.get('version', '?')}, {status}"),
            ))
    return events


def _short_image(ref: str) -> tuple[str, str]:
    """(name, tag) of an image reference: `ghcr.io/o/giks-adminapp:v0.36.0@sha256:…`
    is ("giks-adminapp", "v0.36.0"). A git-SHA tag (sms-gateway's) or a
    digest-only reference gives 12 hex digits."""
    ref, _, digest = ref.partition("@")
    name, _, tag = ref.rpartition("/")[2].partition(":")
    if _HEX_TAG.fullmatch(tag):
        tag = tag[:12]
    return name, tag or digest.removeprefix("sha256:")[:12] or "?"


def _images(rs: dict[str, Any]) -> dict[str, str]:
    """Container name → image, init containers included (one pod's names
    are unique across both)."""
    spec = rs.get("spec", {}).get("template", {}).get("spec", {})
    containers = (spec.get("initContainers") or []) + (spec.get("containers") or [])
    return {c.get("name", "?"): c.get("image", "?") for c in containers}


def _now_running(rs: dict[str, Any]) -> str:
    return ", ".join(":".join(_short_image(ref)) for ref in _images(rs).values())


def _template_change(new: dict[str, Any], old: dict[str, Any] | None) -> str:
    """What a rollout changed: each container image that changed, or that
    only the rest of the pod template did (config, env, a restart)."""
    now = _images(new)
    if old is None:
        return _now_running(new)
    before = _images(old)
    changed = []
    for container, ref in now.items():
        prev = before.get(container)
        if prev is None or prev == ref:
            continue
        (name, tag), (prev_name, prev_tag) = _short_image(ref), _short_image(prev)
        changed.append(f"{name} {prev_tag} → {tag}" if name == prev_name
                       else f"{prev_name}:{prev_tag} → {name}:{tag}")
    return "; ".join(changed) or "same image, pod template changed"


def _rollout_details(replicasets: list[dict[str, Any]]
                     ) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], str]]:
    """Per (namespace, ReplicaSet): what its rollout changed, against the
    Deployment's previous revision when that ReplicaSet is still there;
    and the images it runs."""
    by_deployment: dict[tuple[str, str], list[tuple[int, dict[str, Any]]]] = {}
    for rs in replicasets:
        meta = rs.get("metadata", {})
        owner = next((o.get("name") for o in meta.get("ownerReferences") or []
                      if o.get("kind") == "Deployment"), None)
        try:
            revision = int((meta.get("annotations") or {}).get(_REVISION, ""))
        except ValueError:
            continue
        if owner:
            by_deployment.setdefault((meta.get("namespace", ""), owner), []).append((revision, rs))
    changes: dict[tuple[str, str], str] = {}
    running: dict[tuple[str, str], str] = {}
    for (ns, _owner), revisions in by_deployment.items():
        revisions.sort(key=lambda r: r[0])
        for i, (_rev, rs) in enumerate(revisions):
            key = (ns, rs["metadata"].get("name", ""))
            changes[key] = _template_change(rs, revisions[i - 1][1] if i else None)
            running[key] = _now_running(rs)
    return changes, running


def _scale_ups(resp: dict[str, Any], start: dt.datetime, end: dt.datetime,
               created: set[tuple[str, str]]) -> list[tuple[dt.datetime, str, str, str]]:
    """(at, namespace, Deployment, ReplicaSet) for each ReplicaSet scaled up
    from 0 in the window, except one created in it. `at` is the last sample
    at 0: the scale-up came within one step after it."""
    out = []
    for series in resp.get("data", {}).get("result", []):
        metric = series.get("metric", {})
        key = (metric.get("namespace", ""), metric.get("replicaset", ""))
        if key in created:
            continue
        prev: tuple[float, float] | None = None
        for ts, raw in series.get("values", []):
            try:
                t, v = float(ts), float(raw)
            except (TypeError, ValueError):
                continue
            if prev is not None and prev[1] == 0 and v > 0 and start <= _utc(t) <= end:
                out.append((_utc(prev[0]), key[0], metric.get("owner_name", "?"), key[1]))
            prev = (t, v)
    return out


def _deployment_rollouts(created_resp: dict[str, Any], scale_resp: dict[str, Any] | None,
                         start: dt.datetime, end: dt.datetime, helm: list[KnownEvent],
                         replicasets_fn: Callable[[], list[dict[str, Any]]]) -> list[KnownEvent]:
    created = [(t, m.get("namespace", ""), m.get("owner_name", "?"), m.get("replicaset", ""))
               for m, t in _distinct_starts(created_resp, start, end)]
    scaled = _scale_ups(scale_resp, start, end, {(ns, rs) for _t, ns, _d, rs in created}) \
        if scale_resp is not None else []
    rollouts = [(t, ns, deployment, rs, (t, ns, deployment, rs) in scaled)
                for t, ns, deployment, rs in created + scaled
                if not any(h.namespace == ns and dt.timedelta(0) <= t - h.at <= _ROLLOUT_AFTER_HELM
                           for h in helm)]
    if not rollouts:
        return []
    changes: dict[tuple[str, str], str] | None = None
    running: dict[tuple[str, str], str] = {}
    try:
        changes, running = _rollout_details(replicasets_fn())
    except Exception as e:      # the events stand without what changed
        log.warning("known events: the ReplicaSets for rollout detail failed: %r", e)
    together: list[list[tuple[dt.datetime, str, str, str, bool]]] = []
    for r in sorted(rollouts):
        group = next((g for g in together
                      if g[0][1] == r[1] and r[0] - g[0][0] <= _ROLLOUT_TOGETHER), None)
        if group is None:
            together.append([r])
        else:
            group.append(r)
    events = []
    for group in together:
        at, ns = group[0][0], group[0][1]
        parts = []
        for _t, _ns, deployment, rs, from_zero in group:
            if from_zero:
                now = running.get((ns, rs))
                parts.append(f"{deployment} (scaled up from 0, a rollback or a scale-up"
                             + (f": now {now})" if now else ")"))
            else:
                change = (changes or {}).get((ns, rs))
                parts.append(f"{deployment} ({change})" if change else deployment)
        unknown = "; what changed is unknown, the ReplicaSet read failed" if changes is None else ""
        events.append(KnownEvent(
            kind="deployment_rollout", at=at, namespace=ns,
            what=f"rollout in {ns}: {'; '.join(parts)} (new pods replace the old){unknown}",
        ))
    return events


def _restore_drills(items: list[dict[str, Any]], cronjobs: list[dict[str, Any]],
                    start: dt.datetime, end: dt.datetime) -> list[KnownEvent]:
    events = []
    seen: dict[tuple[str, str], list[dt.datetime]] = {}   # (ns, cronjob) → Job start times
    for job in items:
        meta = job.get("metadata", {})
        owners = [o.get("name", "") for o in meta.get("ownerReferences") or []
                  if o.get("kind") == "CronJob"]
        drill = next((o for o in owners if o.endswith("-restore-drill")), None)
        if drill is None:
            continue
        status = job.get("status", {})
        t = _parse_time(status.get("startTime"))
        if t is None or not (start <= t <= end):
            continue
        done = _parse_time(status.get("completionTime"))
        if status.get("succeeded"):
            outcome = f"succeeded at {done:%H:%M}Z" if done else "succeeded"
        elif status.get("failed"):
            outcome = "FAILED"
        else:
            outcome = "still running"
        ns = meta.get("namespace")
        seen.setdefault((ns, drill), []).append(t)
        events.append(KnownEvent(
            kind="restore_drill", at=t, namespace=ns,
            what=(f"restore drill {ns}/{drill} ({meta.get('name', '?')}), {outcome}: a "
                  "temporary recovery Postgres starts, restores and is deleted"),
        ))
    # A scheduled run whose Job is already gone: the CronJob still says when.
    for cj in cronjobs:
        meta = cj.get("metadata", {})
        name, ns = meta.get("name", ""), meta.get("namespace")
        if not name.endswith("-restore-drill"):
            continue
        t = _parse_time(cj.get("status", {}).get("lastScheduleTime"))
        if t is None or not (start <= t <= end):
            continue
        if any(abs(t - s) <= dt.timedelta(minutes=5) for s in seen.get((ns, name), [])):
            continue
        last_ok = _parse_time(cj.get("status", {}).get("lastSuccessfulTime"))
        outcome = (f"last success {last_ok:%m-%d %H:%M}Z" if last_ok and last_ok >= t
                   else "no success recorded since")
        events.append(KnownEvent(
            kind="restore_drill", at=t, namespace=ns,
            what=(f"restore drill {ns}/{name} scheduled (its Job is gone; {outcome}): a "
                  "temporary recovery Postgres starts, restores and is deleted"),
        ))
    return events


def gather_known_events(
    cluster: str,
    *,
    start: dt.datetime,
    end: dt.datetime,
    prometheus_range_fn: Callable[..., dict[str, Any]],
    kubectl_get_fn: Callable[..., dict[str, Any]],
    failures: list[str] | None = None,
    lead: dt.timedelta = dt.timedelta(hours=1),
) -> list[KnownEvent]:
    """Every known event from `start - lead` to `end`, oldest first.

    `prometheus_range_fn` is tools.prometheus.prometheus_query_range and
    `kubectl_get_fn` tools.kubectl.kubectl_get (read-only; the agent's
    ClusterRole reads HelmReleases, ReplicaSets, Jobs and CronJobs). A
    source that raises, or answers empty where a healthy cluster cannot,
    is recorded in `failures` as "known events <source>: <ExceptionType>".
    """
    failures = failures if failures is not None else []
    since = start - lead
    events: list[KnownEvent] = []

    def _failed(source: str, e: Exception) -> None:
        log.warning("known events %s %s failed: %r", cluster, source, e)
        failures.append(f"known events {source}: {type(e).__name__}")

    boots: list[KnownEvent] = []
    try:
        boots = _node_boots(_range(prometheus_range_fn, cluster, _BOOT_QUERY, since, end),
                            since, end)
    except Exception as e:
        _failed("node boots", e)
    events += boots
    try:
        events += _apiserver_restarts(
            _range(prometheus_range_fn, cluster, _APISERVER_QUERY, since, end), since, end, boots)
    except Exception as e:
        _failed("apiserver restarts", e)
    helm: list[KnownEvent] = []
    try:
        releases = kubectl_get_fn(cluster, "helmreleases").get("items", [])
        if not releases:   # every cluster here runs Flux's HelmReleases
            raise LookupError("no HelmRelease at all")
        helm = _helm_upgrades(releases, since, end)
    except Exception as e:
        _failed("helm upgrades", e)
    events += helm
    try:
        created = _range(prometheus_range_fn, cluster, _ROLLOUT_QUERY, since, end)
        scaled = None
        try:
            scaled = _range(prometheus_range_fn, cluster, _SCALE_QUERY, since, end)
        except Exception as e:     # rollouts still stand: only rollbacks are unknown
            _failed("rollbacks", e)
        events += _deployment_rollouts(
            created, scaled, since, end, helm,
            lambda: kubectl_get_fn(cluster, "replicasets").get("items", []))
    except Exception as e:
        _failed("deployment rollouts", e)
    try:
        events += _restore_drills(kubectl_get_fn(cluster, "jobs").get("items", []),
                                  kubectl_get_fn(cluster, "cronjobs").get("items", []),
                                  since, end)
    except Exception as e:
        _failed("restore drills", e)

    events.sort(key=lambda e: (e.at, e.kind, e.what))
    return events
