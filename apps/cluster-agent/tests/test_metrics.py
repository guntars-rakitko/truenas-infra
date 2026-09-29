"""Prometheus metrics emit module — declares the 10 metrics from spec § 3.4."""
import os
import re
import subprocess
import sys
from pathlib import Path

from cluster_agent.emit.metrics import (
    DISPATCH_SURFACES,
    cluster_agent_run_total,
    cluster_agent_finding_total,
    cluster_agent_anthropic_cost_usd_total,
    cluster_agent_last_success_timestamp,
    render,
)


def test_counter_increments():
    before = cluster_agent_run_total.labels(mode="A", status="success")._value.get()
    cluster_agent_run_total.labels(mode="A", status="success").inc()
    after = cluster_agent_run_total.labels(mode="A", status="success")._value.get()
    assert after == before + 1


def test_render_includes_all_required_metrics():
    output = render()
    for metric_name in [
        "cluster_agent_run_total",
        "cluster_agent_run_duration_seconds",
        "cluster_agent_finding_total",
        "cluster_agent_open_findings",
        "cluster_agent_pr_action_total",
        "cluster_agent_anthropic_tokens_total",
        "cluster_agent_anthropic_cost_usd_total",
        "cluster_agent_last_success_timestamp",
        "cluster_agent_backup_verification_status",
        "cluster_agent_doctrine_drift_count",
    ]:
        assert metric_name in output, f"missing metric: {metric_name}"


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
