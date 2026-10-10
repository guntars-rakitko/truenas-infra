"""Known events: what the cluster went through in the digest window (2026-10-10).

A planned change makes noise. A reboot restarts every pod, a Talos
config apply restarts kube-apiserver, a Helm upgrade rolls its workloads,
and a restore drill starts a recovery Postgres that says "starting up".
Without knowing that, the LLM filed that noise as findings (kube-infra
#1437, #1509, #1535 and the reboot findings of 2026-10-02..07). These
are read from the cluster itself and shown to the LLM and in the
digest-summary:

  node_boot          distinct `node_boot_time_seconds` values (Prometheus)
  apiserver_restart  distinct `process_start_time_seconds{job="apiserver"}`
                     values, except the one within 15 min after a boot
  helm_upgrade       HelmRelease `status.history` revisions deployed in the
                     window (Flux keeps the last few, newest first)
  restore_drill      Jobs of a `*-restore-drill` CronJob that started in the
                     window, scheduled or created by hand (Jobs live 24 h:
                     `ttlSecondsAfterFinished: 86400`)

The window starts `lead` (1 h) before the digest window: a boot at 02:55
makes noise after the 03:00 window start. Each source fails on its own,
recorded in `failures` like the log-mining queries: a missing source is
a coverage gap, never "nothing happened".
"""
from __future__ import annotations
import datetime as dt
import logging
import math
from typing import Any, Callable

from ..schema import KnownEvent


log = logging.getLogger(__name__)

# Same series twice after a node-exporter or kube-state-metrics restart:
# `max by` folds the old and the new pod's copies.
_BOOT_QUERY = (
    "max by (instance, nodename) (node_boot_time_seconds"
    " * on(instance) group_left(nodename) max by (instance, nodename) (node_uname_info))"
)
_APISERVER_QUERY = 'max by (instance) (process_start_time_seconds{job="apiserver"})'

# A boot time read twice can differ by a second (btime is recomputed from
# the clock); two real boots are minutes apart at least.
_SAME_START = dt.timedelta(seconds=120)
# kube-apiserver starts ~50 s after its node boots (measured on both MS-A2
# boxes, 2026-10-01..10): that start is part of the boot, not an event.
_APISERVER_AFTER_BOOT = dt.timedelta(minutes=15)
_STEP_SECONDS = 300


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
    return prometheus_range_fn(
        cluster, query,
        start=str(int(start.timestamp())), end=str(int(end.timestamp())),
        step=f"{_STEP_SECONDS}s",
    )


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


def _restore_drills(items: list[dict[str, Any]], start: dt.datetime, end: dt.datetime
                    ) -> list[KnownEvent]:
    events = []
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
        events.append(KnownEvent(
            kind="restore_drill", at=t, namespace=ns,
            what=(f"restore drill {ns}/{drill} ({meta.get('name', '?')}), {outcome}: a "
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
    ClusterRole reads HelmReleases and Jobs). A source that raises is
    recorded in `failures` as "known events <source>: <ExceptionType>".
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
    try:
        events += _helm_upgrades(kubectl_get_fn(cluster, "helmreleases").get("items", []),
                                 since, end)
    except Exception as e:
        _failed("helm upgrades", e)
    try:
        events += _restore_drills(kubectl_get_fn(cluster, "jobs").get("items", []), since, end)
    except Exception as e:
        _failed("restore drills", e)

    events.sort(key=lambda e: (e.at, e.kind, e.what))
    return events
