"""Prometheus metrics emit module: every metric declared is one the agent sets."""
import os
import re
import subprocess
import sys
from pathlib import Path

from cluster_agent.emit.metrics import (
    DISPATCH_SURFACES,
    cluster_agent_run_total,
    render,
)


def test_counter_increments():
    before = cluster_agent_run_total.labels(mode="A", status="success")._value.get()
    cluster_agent_run_total.labels(mode="A", status="success").inc()
    after = cluster_agent_run_total.labels(mode="A", status="success")._value.get()
    assert after == before + 1


def test_render_includes_the_metrics_kube_infra_reads():
    """kube-infra's dashboard and ClusterAgent* rules read these names."""
    output = render()
    for metric_name in [
        "cluster_agent_run_total",
        "cluster_agent_run_duration_seconds",
        "cluster_agent_last_success_timestamp",
        "cluster_agent_dispatch_errors_total",
        "cluster_agent_llm_input_tokens_total",
        "cluster_agent_llm_output_tokens_total",
        "cluster_agent_llm_cost_usd_total",
        "cluster_agent_llm_cache_read_tokens_total",
        "cluster_agent_llm_cache_create_tokens_total",
    ]:
        assert metric_name in output, f"missing metric: {metric_name}"


def test_every_declared_metric_is_set_somewhere():
    """A declared metric nothing sets is a permanent 0 or no series, and a
    dashboard reading it is a false clean (2026-10-10: seven such were
    removed, one of them behind the spend tile)."""
    src = Path(__file__).resolve().parents[1] / "src" / "cluster_agent"
    module = (src / "emit" / "metrics.py").read_text()
    declared = re.findall(r"^(\w+) = (?:Counter|Gauge|Histogram)\(", module, re.MULTILINE)
    assert len(declared) >= 9, declared
    others = "\n".join(p.read_text() for p in src.rglob("*.py") if p.name != "metrics.py")
    for name in declared:
        assert re.search(rf"\b{name}\b", others), f"{name} is declared but never set"


def test_dispatch_error_series_exist_at_zero_before_any_failure():
    """A fresh process exposes every dispatch surface's series at 0.

    kube-infra's ClusterAgentDispatchErrors is `increase(...[6h]) > 0`. A
    series born at 1 by the first failure has no earlier sample, so
    `increase()` sees no rise and that first failure never fires the alert.
    A subprocess, because this process's other tests increment the counter.
    The surfaces are spelled out here, not read from DISPATCH_SURFACES, so
    dropping one from the module fails this test."""
    out = subprocess.run(
        [sys.executable, "-c", "from cluster_agent.emit.metrics import render; print(render())"],
        check=True, capture_output=True, text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    ).stdout
    for surface in (
        "grafana_annotation",
        "gh_issue_create",
        "gh_issue_comment",
        "gh_issue_reopen_comment",
    ):
        line = f'cluster_agent_dispatch_errors_total{{surface="{surface}"}} 0.0'
        assert line in out.splitlines(), f"no zero series for {surface!r}"


def test_every_counted_dispatch_surface_is_pre_initialised():
    """Tripwire: each `DISPATCH_ERRORS.labels(surface=...)` in the source names
    a surface in DISPATCH_SURFACES, and every pre-initialised surface is one
    the source counts. A new surface without its zero would be invisible to
    the alert on its first failure."""
    src = Path(__file__).resolve().parents[1] / "src" / "cluster_agent"
    counted: set[str] = set()
    for path in src.rglob("*.py"):
        counted |= set(re.findall(
            r'DISPATCH_ERRORS\.labels\(\s*surface\s*=\s*"([^"]+)"', path.read_text()))
    assert counted, "found no DISPATCH_ERRORS call sites: the scan is broken"
    assert counted == set(DISPATCH_SURFACES)
