"""Prometheus metrics emit module — declares the 10 metrics from spec § 3.4."""
from cluster_agent.emit.metrics import (
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
