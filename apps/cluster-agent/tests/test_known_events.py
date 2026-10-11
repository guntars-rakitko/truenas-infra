"""Known events (modes/known_events.py): what the cluster went through.

The Prometheus answers below are shaped like the live ones read on
2026-10-10 (msa2-dev booted 14:21:02Z, its apiserver started 14:21:52Z;
the audit-trim apply on 10-01 restarted the apiserver alone at 18:06Z).
"""
from __future__ import annotations
import datetime as dt

import pytest

from cluster_agent.modes.known_events import _short_image, gather_known_events, touches
from cluster_agent.schema import KnownEvent

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


# A healthy cluster's answers with nothing new in the window: the node and
# kube-apiserver have been up since 10-03 (an empty answer is a coverage gap).
_UP_SINCE_BOOT = _matrix({"instance": "10.10.5.12:9100", "nodename": "msa2-dev-01"}, [_ts(11, 47, 10, day=3)])
_UP_SINCE_API = _matrix({"instance": "10.10.5.12:6443"}, [_ts(11, 48, 0, day=3)])
_EMPTY = {"data": {"result": []}}


def _replicasets(*rows: tuple[str, str, str, float]) -> dict:
    """kube_replicaset_created joined with its Deployment: one series per
    (namespace, Deployment, ReplicaSet, creation time)."""
    t0 = _START.timestamp()
    return {"data": {"result": [
        {"metric": {"namespace": ns, "owner_name": deployment, "replicaset": rs},
         "values": [[t0 + i * 300, str(created)] for i in range(3)]}
        for ns, deployment, rs, created in rows]}}


_UP_SINCE_RS = _replicasets(("giks", "adminapp", "adminapp-57644dcd4f", _ts(7, 31, 51, day=3)))


class _Prom:
    def __init__(self, boots=None, apiserver=None, replicasets=None, raise_on=()):
        self.answers = {"node_boot_time_seconds": boots or _UP_SINCE_BOOT,
                        "process_start_time_seconds": apiserver or _UP_SINCE_API,
                        "kube_replicaset_created": replicasets or _UP_SINCE_RS}
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
    def __init__(self, helmreleases=None, jobs=(), cronjobs=(), replicasets=(), raise_on=()):
        # A cluster always has HelmReleases (an empty list is a gap): by
        # default one whose last deploy is long before the window.
        self.items = {"helmreleases": list(helmreleases) if helmreleases is not None else [_OLD_HR],
                      "jobs": list(jobs), "cronjobs": list(cronjobs),
                      "replicasets": list(replicasets)}
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
    assert len(prom.calls) == 3   # boots, apiserver starts, ReplicaSets
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


_OLD_HR = _hr("trust-manager", "cert-manager", [_snap(1, "v0.24.0", "2026-09-28T14:11:11Z", action="install")])


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
    ({"kube_replicaset_created"}, set(), ["known events deployment rollouts: TimeoutError"],
     ["restore_drill", "helm_upgrade", "node_boot"]),
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


# ── An empty answer is a gap; the drill outlives its Job (review, 2026-10-10)

@pytest.mark.parametrize("prom_kwargs,kube_kwargs,expected", [
    ({"boots": _EMPTY}, {}, ["known events node boots: LookupError"]),
    ({"apiserver": _EMPTY}, {}, ["known events apiserver restarts: LookupError"]),
    ({}, {"helmreleases": []}, ["known events helm upgrades: LookupError"]),
    ({"replicasets": _EMPTY}, {}, ["known events deployment rollouts: LookupError"]),
])
def test_an_empty_answer_where_a_cluster_always_has_one_is_a_gap(prom_kwargs, kube_kwargs, expected):
    """node-exporter, the apiserver scrape or kube-state-metrics down, or
    Flux gone: "no events" would be a false clean."""
    failures: list[str] = []
    assert _gather(_Prom(**prom_kwargs), _Kube(**kube_kwargs), failures) == []
    assert failures == expected


def test_no_jobs_at_all_is_a_quiet_answer_not_a_gap():
    failures: list[str] = []
    _gather(kube=_Kube(jobs=[], cronjobs=[]), failures=failures)
    assert failures == []


def test_a_boot_without_a_node_name_still_counts():
    """The `or on(instance)` arm: node_uname_info missing costs the name only."""
    boots = _matrix({"instance": "10.10.5.12:9100"}, [_ts(14, 21, 2)])
    (event,) = _gather(_Prom(boots=boots))
    assert "node 10.10.5.12:9100 booted" in event.what
    prom = _Prom()
    _gather(prom)
    boot_query = next(c[0] for c in prom.calls if "node_boot_time_seconds" in c[0])
    assert boot_query.endswith(" or on(instance) max by (instance) (node_boot_time_seconds)")


def _cronjob(name, last_schedule, last_success=None, namespace="giks-db"):
    status = {"lastScheduleTime": last_schedule}
    if last_success:
        status["lastSuccessfulTime"] = last_success
    return {"metadata": {"name": name, "namespace": namespace}, "status": status}


def test_a_drill_whose_job_is_already_reaped_comes_from_its_cronjob():
    """In winter the digest runs at 04:00Z, when Monday's 04:00Z drill Job
    (24 h TTL) is being reaped: the CronJob's status outlives it."""
    kube = _Kube(jobs=[], cronjobs=[
        _cronjob("giks-restore-drill", "2026-10-10T04:00:00Z", "2026-10-10T04:00:48Z"),
        _cronjob("giks-db-offsite-dump", "2026-10-10T03:00:00Z", "2026-10-10T03:00:55Z"),
        _cronjob("w1-restore-drill", "2026-10-05T05:00:00Z", namespace="w1-db"),     # before the window
    ])
    (event,) = _gather(kube=kube)
    assert (event.kind, event.namespace) == ("restore_drill", "giks-db")
    assert event.at == dt.datetime(2026, 10, 10, 4, 0, tzinfo=_UTC)
    assert "its Job is gone; last success 10-10 04:00Z" in event.what


def test_a_drill_with_its_job_present_is_listed_once():
    kube = _Kube(
        jobs=[_job("giks-restore-drill-29330880", "giks-restore-drill", "2026-10-10T04:00:03Z",
                   "2026-10-10T04:00:48Z", succeeded=1)],
        cronjobs=[_cronjob("giks-restore-drill", "2026-10-10T04:00:00Z", "2026-10-10T04:00:48Z")],
    )
    (event,) = _gather(kube=kube)
    assert "succeeded at 04:00Z" in event.what


def test_a_scheduled_drill_that_never_succeeded_says_so():
    kube = _Kube(cronjobs=[_cronjob("giks-restore-drill", "2026-10-10T04:00:00Z",
                                    last_success="2026-10-03T04:00:50Z")])
    (event,) = _gather(kube=kube)
    assert "no success recorded since" in event.what


# ── Deployment rollouts (2026-10-11: kube-infra#1577 was one readiness
# failure during an sms-gateway rollout) ─────────────────────────────────

def _rs(ns, deployment, name, revision, *images):
    return {"metadata": {"name": name, "namespace": ns,
                         "annotations": {"deployment.kubernetes.io/revision": str(revision)},
                         "ownerReferences": [{"kind": "Deployment", "name": deployment}]},
            "spec": {"template": {"spec": {"containers": [
                {"name": f"c{i}", "image": image} for i, image in enumerate(images)]}}}}


_GHCR = "ghcr.io/guntars-rakitko/"
# prd on 2026-10-10: the v0.36.0 promotion and the sms-gateway image bump
# came together at 07:31; cloudnative-pg's upgrade at 13:49:33 rolled its
# own Deployment (seconds later) and, through the new operator, giks-db's
# pgbouncer Pooler, which is not the release's.
_PRD_ROLLOUTS = _replicasets(
    ("sms-gateway", "app", "app-744557c454", _ts(7, 31, 50)),
    ("giks", "adminapp", "adminapp-57644dcd4f", _ts(7, 31, 51)),
    ("giks", "jobserver", "jobserver-fd577f65b", _ts(7, 31, 51)),
    ("cnpg-system", "cloudnative-pg", "cloudnative-pg-66fd48f8c7", _ts(13, 49, 34)),
    ("giks-db", "giks-rw-pooler", "giks-rw-pooler-77ff66dc", _ts(13, 49, 56)),
    ("giks", "adminapp", "adminapp-6b4d8c9f7", _ts(14, 0, 0, day=9)),        # before the window
)
_CNPG_HR = _hr("cloudnative-pg", "cnpg-system", [
    _snap(2, "0.29.1", "2026-10-10T13:49:33Z", namespace="cnpg-system"),
    _snap(1, "0.29.0", "2026-09-28T14:10:03Z", status="superseded", namespace="cnpg-system")])


def _rollouts(**kube_kwargs):
    events = _gather(_Prom(replicasets=_PRD_ROLLOUTS), _Kube(helmreleases=[_CNPG_HR], **kube_kwargs))
    return [e for e in events if e.kind == "deployment_rollout"]


def test_rollouts_come_from_prometheus_one_event_per_namespace_and_moment():
    events = _rollouts()
    assert [(e.at.strftime("%H:%M:%S"), e.namespace) for e in events] == [
        ("07:31:50", "sms-gateway"), ("07:31:51", "giks"), ("13:49:56", "giks-db")]
    assert events[1].what.startswith("rollout in giks: adminapp; jobserver")


def test_a_helm_upgrades_own_rollout_is_not_a_second_event_but_one_elsewhere_is():
    """cloudnative-pg's Deployment rolled out 1 s after its upgrade (the
    upgrade's event says it); giks-db's Pooler is a separate change."""
    namespaces = [e.namespace for e in _rollouts()]
    assert "cnpg-system" not in namespaces and "giks-db" in namespaces


def test_a_rollout_says_what_changed_when_its_replicasets_are_still_there():
    kube_rs = [
        _rs("giks", "adminapp", "adminapp-57644dcd4f", 5, _GHCR + "giks-adminapp:v0.36.0@sha256:fecb04"),
        _rs("giks", "adminapp", "adminapp-5f8d9c7b6d", 4, _GHCR + "giks-adminapp:v0.35.0@sha256:980aad"),
        _rs("giks", "jobserver", "jobserver-fd577f65b", 5, _GHCR + "giks-jobserver:v0.36.0"),
        _rs("giks", "jobserver", "jobserver-7c9b5d8f4", 4, _GHCR + "giks-jobserver:v0.36.0"),
        _rs("giks-db", "giks-rw-pooler", "giks-rw-pooler-77ff66dc", 2,
            "ghcr.io/cloudnative-pg/pgbouncer:1.25.2"),
    ]
    events = {e.namespace: e.what for e in _rollouts(replicasets=kube_rs)}
    assert events["giks"].startswith(
        "rollout in giks: adminapp (giks-adminapp v0.35.0 → v0.36.0); "
        "jobserver (same image, pod template changed)")
    # revision 1 is gone: the new image alone; sms-gateway's ReplicaSet is gone too
    assert events["giks-db"].startswith("rollout in giks-db: giks-rw-pooler (pgbouncer:1.25.2)")
    assert events["sms-gateway"].startswith("rollout in sms-gateway: app (new pods")


def test_the_live_replicasets_failing_costs_the_detail_not_the_events():
    failures: list[str] = []
    events = [e for e in _gather(_Prom(replicasets=_PRD_ROLLOUTS),
                                 _Kube(helmreleases=[_CNPG_HR], raise_on={"replicasets"}), failures)
              if e.kind == "deployment_rollout"]
    assert len(events) == 3
    assert failures == ["known events rollout detail: RuntimeError"]


def test_no_rollout_in_the_window_reads_no_replicasets():
    kube = _Kube()
    _gather(kube=kube)
    assert "replicasets" not in kube.calls


@pytest.mark.parametrize("ref,expected", [
    ("ghcr.io/guntars-rakitko/giks-adminapp:v0.36.0@sha256:fecb045281", ("giks-adminapp", "v0.36.0")),
    ("registry.local:5000/team/app:1.2", ("app", "1.2")),
    ("dopplerhq/kubernetes-operator:1.7.2", ("kubernetes-operator", "1.7.2")),
    ("ghcr.io/x/app@sha256:3aaa7d9948f2fa85551b", ("app", "3aaa7d9948f2")),
    ("ghcr.io/o/sms-gateway-app:a9b6a524d8b8a10015c0a99d1449ba8b9cf0e9d5@sha256:3aaa7d",
     ("sms-gateway-app", "a9b6a524d8b8")),
    ("nginx", ("nginx", "?")),
])
def test_short_image(ref, expected):
    assert _short_image(ref) == expected


def test_a_boot_touches_every_namespace_a_rollout_only_its_own():
    at = dt.datetime(2026, 10, 10, 14, 21, tzinfo=_UTC)
    boot = KnownEvent(kind="node_boot", at=at, what="node booted")
    rollout = KnownEvent(kind="deployment_rollout", at=at, what="rollout", namespace="giks")
    assert touches(boot, "giks") and touches(boot, "kube-system")
    assert touches(rollout, "giks") and not touches(rollout, "giks-db")
