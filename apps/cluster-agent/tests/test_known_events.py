"""Known events (modes/known_events.py): what the cluster went through.

The Prometheus answers below are shaped like the live ones read on
2026-10-10 (msa2-dev booted 14:21:02Z, its apiserver started 14:21:52Z;
the audit-trim apply on 10-01 restarted the apiserver alone at 18:06Z).
"""
from __future__ import annotations
import datetime as dt

import pytest

from cluster_agent.modes.known_events import gather_known_events

_UTC = dt.timezone.utc
_END = dt.datetime(2026, 10, 11, 3, 0, tzinfo=_UTC)
_START = _END - dt.timedelta(hours=24)


def _ts(h: int, m: int = 0, s: int = 0, day: int = 10) -> float:
    return dt.datetime(2026, 10, day, h, m, s, tzinfo=_UTC).timestamp()


def _matrix(metric: dict, values: list[float]) -> dict:
    """A query_range answer whose samples carry `values` in order."""
    t0 = _START.timestamp()
    return {"data": {"result": [{"metric": metric,
                                 "values": [[t0 + i * 300, str(v)] for i, v in enumerate(values)]}]}}


class _Prom:
    def __init__(self, boots=None, apiserver=None, raise_on=()):
        self.answers = {"node_boot_time_seconds": boots or {"data": {"result": []}},
                        "process_start_time_seconds": apiserver or {"data": {"result": []}}}
        self.raise_on = set(raise_on)
        self.calls: list[tuple[str, str, str, str]] = []

    def __call__(self, cluster, promql, *, start, end, step):
        self.calls.append((promql, start, end, step))
        for name, answer in self.answers.items():
            if name in promql:
                if name in self.raise_on:
                    raise TimeoutError("prometheus timed out")
                return answer
        raise AssertionError(promql)


class _Kube:
    def __init__(self, helmreleases=(), jobs=(), raise_on=()):
        self.items = {"helmreleases": list(helmreleases), "jobs": list(jobs)}
        self.raise_on = set(raise_on)
        self.calls: list[str] = []

    def __call__(self, cluster, resource, **kw):
        self.calls.append(resource)
        assert kw == {}, "read every namespace: no name, namespace or selector"
        if resource in self.raise_on:
            raise RuntimeError(f"kubectl get {resource} failed")
        return {"items": self.items[resource]}


def _gather(prom=None, kube=None, failures=None):
    return gather_known_events("dev", start=_START, end=_END,
                               prometheus_range_fn=prom or _Prom(), kubectl_get_fn=kube or _Kube(),
                               failures=failures)


def test_a_boot_in_the_window_is_one_event_and_the_uptime_before_is_none():
    boots = _matrix({"instance": "10.10.5.12:9100", "nodename": "msa2-dev-01"},
                    [_ts(11, 47, 10, day=3)] * 100 + [_ts(14, 21, 2)] * 188)
    (event,) = _gather(_Prom(boots=boots))
    assert event.kind == "node_boot"
    assert event.at == dt.datetime(2026, 10, 10, 14, 21, 2, tzinfo=_UTC)
    assert event.namespace is None
    assert "msa2-dev-01" in event.what


def test_a_boot_time_read_a_second_apart_is_still_one_boot():
    boots = _matrix({"nodename": "msa2-dev-01"}, [_ts(14, 21, 2), _ts(14, 21, 3), _ts(14, 21, 2)])
    assert len(_gather(_Prom(boots=boots))) == 1


def test_a_boot_in_the_hour_before_the_window_counts_one_after_does_not():
    boots = _matrix({"nodename": "msa2-dev-01"}, [_ts(2, 30), _ts(1, 59, day=10)])
    assert [e.at.hour for e in _gather(_Prom(boots=boots))] == [2]   # 01:59 is before the lead


def test_the_apiserver_start_of_a_boot_is_folded_into_the_boot():
    boots = _matrix({"nodename": "msa2-dev-01"}, [_ts(14, 21, 2)])
    apiserver = _matrix({"instance": "10.10.5.12:6443"}, [_ts(14, 21, 52), _ts(18, 6, 41)])
    events = _gather(_Prom(boots=boots, apiserver=apiserver))
    assert [(e.kind, e.at.strftime("%H:%M")) for e in events] == [
        ("node_boot", "14:21"), ("apiserver_restart", "18:06")]
    assert "without a reboot" in events[1].what


def test_the_queries_cover_the_lead_and_step_five_minutes():
    prom = _Prom()
    _gather(prom)
    assert len(prom.calls) == 2   # boots, then apiserver starts
    for _q, start, end, step in prom.calls:
        assert int(start) == int((_START - dt.timedelta(hours=1)).timestamp())
        assert int(end) == int(_END.timestamp())
        assert step == "300s"
    boot_query = next(c[0] for c in prom.calls if "node_boot_time_seconds" in c[0])
    assert "max by (instance, nodename)" in boot_query   # an exporter restart doubles the series


def _hr(name, target, history):
    return {"metadata": {"name": name, "namespace": "flux-system"},
            "spec": {"targetNamespace": target}, "status": {"history": history}}


def _snap(version, chart, deployed, status="deployed", action="upgrade", namespace=None):
    return {"version": version, "chartVersion": chart, "lastDeployed": deployed,
            "status": status, "action": action, "namespace": namespace}


def test_helm_upgrades_in_the_window_name_the_chart_change():
    kube = _Kube(helmreleases=[
        _hr("cloudnative-pg", "cnpg-system", [
            _snap(2, "0.29.1", "2026-10-10T13:33:08Z", namespace="cnpg-system"),
            _snap(1, "0.29.0", "2026-09-28T14:10:03Z", status="superseded", namespace="cnpg-system")]),
        _hr("kube-prometheus-stack", "monitoring", [
            _snap(9, "88.6.5", "2026-10-10T09:35:07Z", namespace="monitoring"),
            _snap(8, "88.6.5", "2026-10-03T09:59:42Z", status="superseded", namespace="monitoring")]),
        _hr("trust-manager", "cert-manager", [
            _snap(1, "v0.24.0", "2026-09-28T14:11:11Z", action="install")]),
    ])
    events = _gather(kube=kube)
    assert [(e.kind, e.namespace) for e in events] == [
        ("helm_upgrade", "monitoring"), ("helm_upgrade", "cnpg-system")]
    kps, cnpg = events
    assert "values changed, chart 88.6.5" in kps.what and "revision 9" in kps.what
    assert "chart 0.29.0 → 0.29.1" in cnpg.what and "deployed" in cnpg.what


def test_a_failed_upgrade_says_so():
    kube = _Kube(helmreleases=[_hr("cilium", "kube-system", [
        _snap(3, "1.19.9", "2026-10-10T10:00:00Z", status="failed", namespace="kube-system"),
        _snap(2, "1.19.8", "2026-10-06T10:50:15Z", status="superseded")])])
    (event,) = _gather(kube=kube)
    assert event.what.endswith("failed")


def _job(name, cronjob, start, completion=None, succeeded=None, failed=None, namespace="giks-db"):
    status = {"startTime": start}
    if completion:
        status["completionTime"] = completion
    if succeeded:
        status["succeeded"] = succeeded
    if failed:
        status["failed"] = failed
    return {"metadata": {"name": name, "namespace": namespace,
                         "ownerReferences": [{"kind": "CronJob", "name": cronjob}] if cronjob else []},
            "status": status}


def test_restore_drill_jobs_scheduled_or_by_hand_are_events_other_jobs_are_not():
    kube = _Kube(jobs=[
        _job("giks-restore-drill-29330880", "giks-restore-drill", "2026-10-10T04:00:00Z",
             "2026-10-10T04:00:48Z", succeeded=1),
        _job("w1-drill-manual", "w1-restore-drill", "2026-10-10T06:19:00Z", failed=1, namespace="w1-db"),
        _job("giks-db-offsite-dump-29330820", "giks-db-offsite-dump", "2026-10-10T03:00:00Z", succeeded=1),
        _job("etcd-snapshot-dev-1", "etcd-snapshot-dev", "2026-10-10T18:07:00Z", succeeded=1),
        _job("orphan", None, "2026-10-10T05:00:00Z"),
        _job("giks-restore-drill-old", "giks-restore-drill", "2026-10-08T04:00:00Z", succeeded=1),
    ])
    events = _gather(kube=kube)
    assert [(e.kind, e.namespace) for e in events] == [
        ("restore_drill", "giks-db"), ("restore_drill", "w1-db")]
    assert "succeeded at 04:00Z" in events[0].what
    assert "FAILED" in events[1].what


def test_events_come_oldest_first_across_sources():
    boots = _matrix({"nodename": "msa2-dev-01"}, [_ts(14, 21, 2)])
    kube = _Kube(
        helmreleases=[_hr("kyverno", "kyverno", [_snap(5, "3.9.1", "2026-10-10T10:13:42Z")])],
        jobs=[_job("giks-restore-drill-1", "giks-restore-drill", "2026-10-10T04:00:00Z")],
    )
    events = _gather(_Prom(boots=boots), kube)
    assert [e.kind for e in events] == ["restore_drill", "helm_upgrade", "node_boot"]
    assert "still running" in events[0].what


@pytest.mark.parametrize("prom_fail,kube_fail,expected,kinds", [
    (set(), set(), [], ["restore_drill", "helm_upgrade", "node_boot"]),
    ({"node_boot_time_seconds"}, set(), ["known events node boots: TimeoutError"],
     ["restore_drill", "helm_upgrade"]),
    ({"process_start_time_seconds"}, set(), ["known events apiserver restarts: TimeoutError"],
     ["restore_drill", "helm_upgrade", "node_boot"]),
    (set(), {"helmreleases"}, ["known events helm upgrades: RuntimeError"],
     ["restore_drill", "node_boot"]),
    (set(), {"jobs"}, ["known events restore drills: RuntimeError"],
     ["helm_upgrade", "node_boot"]),
])
def test_each_source_fails_on_its_own_and_is_recorded(prom_fail, kube_fail, expected, kinds):
    boots = _matrix({"nodename": "msa2-dev-01"}, [_ts(14, 21, 2)])
    kube = _Kube(helmreleases=[_hr("kyverno", "kyverno", [_snap(5, "3.9.1", "2026-10-10T10:13:42Z")])],
                 jobs=[_job("giks-restore-drill-1", "giks-restore-drill", "2026-10-10T04:00:00Z")],
                 raise_on=kube_fail)
    failures: list[str] = []
    events = _gather(_Prom(boots=boots, raise_on=prom_fail), kube, failures)
    assert failures == expected
    assert [e.kind for e in events] == kinds


def test_garbage_in_an_answer_is_skipped_not_fatal():
    boots = {"data": {"result": [{"metric": {}, "values": [[1, "NaN-ish"], [2, None], [3, "NaN"],
                                                           [4, "+Inf"]]}]}}
    kube = _Kube(helmreleases=[_hr("x", "y", [{"lastDeployed": "not a time"}, {}])],
                 jobs=[{"metadata": {"ownerReferences": [{"kind": "CronJob", "name": "a-restore-drill"}]},
                        "status": {}}])
    failures: list[str] = []
    assert _gather(_Prom(boots=boots), kube, failures) == []
    assert failures == []
