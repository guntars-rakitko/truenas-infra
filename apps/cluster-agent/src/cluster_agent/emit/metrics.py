"""Prometheus metrics — see spec § 3.4 for the full list.

These 10 metrics are exposed via /metrics (Task 18) and scraped by both
clusters' Prometheus instances via an additionalScrapeConfigs entry
(Task 20). The Grafana dashboard (Task 21) queries them.

Metric naming follows Prometheus conventions:
  - _total suffix: counters
  - _seconds suffix: histograms (durations)
  - bare: gauges
  - all prefixed cluster_agent_ for unambiguous filtering
"""
from __future__ import annotations
from prometheus_client import (
    Counter, Gauge, Histogram, REGISTRY,
    generate_latest, CONTENT_TYPE_LATEST,
)


# Mode lifecycle
cluster_agent_run_total = Counter(
    "cluster_agent_run_total",
    "Total mode runs by status",
    ["mode", "status"],
)

cluster_agent_run_duration_seconds = Histogram(
    "cluster_agent_run_duration_seconds",
    "Time per mode run",
    ["mode"],
    # Buckets cover the realistic range: a quiet A-run (5s) through a
    # full proactive scan (10min) through a hard-cap exceeded run (30min).
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1800),
)

# Findings (the agent's output)
cluster_agent_finding_total = Counter(
    "cluster_agent_finding_total",
    "Findings produced",
    ["mode", "severity", "category"],
)

cluster_agent_open_findings = Gauge(
    "cluster_agent_open_findings",
    "Currently open findings (GH issues opened by agent)",
    ["severity"],
)

# PR triage (Mode I + J)
cluster_agent_pr_action_total = Counter(
    "cluster_agent_pr_action_total",
    "PR actions taken",
    ["action"],  # comment / auto_merge / skip_for_review
)

# LLM cost tracking (the cost ceiling alert at $75/mo fires off these)
cluster_agent_anthropic_tokens_total = Counter(
    "cluster_agent_anthropic_tokens_total",
    "Anthropic API token counts",
    ["kind"],   # input / output / cache_read
)

cluster_agent_anthropic_cost_usd_total = Counter(
    "cluster_agent_anthropic_cost_usd_total",
    "Cumulative Anthropic spend in USD",
)

# Liveness — alert if last_success > 2× cadence ago
cluster_agent_last_success_timestamp = Gauge(
    "cluster_agent_last_success_timestamp",
    "Unix epoch of last successful run per mode",
    ["mode"],
)

# Mode G outputs
cluster_agent_backup_verification_status = Gauge(
    "cluster_agent_backup_verification_status",
    "1=pass, 0=fail per target",
    ["target"],   # b2 / velero / litestream
)

# Mode H outputs
cluster_agent_doctrine_drift_count = Gauge(
    "cluster_agent_doctrine_drift_count",
    "Doctrine violations found in last scan",
    ["repo"],
)

# Dispatch failure tracking (P1+) — each surface is best-effort, so a
# failure on (say) Grafana annotation doesn't abort the run. But silent
# log-only failures hide pipeline degradation (apiserver-proxy RBAC
# drift, GH App token expiry, etc.), so we count them here and let
# Prometheus alert on a sustained nonzero rate.
DISPATCH_ERRORS = Counter(
    "cluster_agent_dispatch_errors_total",
    "Dispatch failures by output surface",
    ["surface"],  # grafana_annotation / gh_issue_create / gh_issue_comment / gh_issue_reopen_comment
)

# Mode A LLM accounting (P1+)
LLM_TOKENS_INPUT = Counter(
    "cluster_agent_llm_input_tokens_total",
    "Total input tokens sent to the LLM, per mode",
    ["mode"],
)
LLM_TOKENS_OUTPUT = Counter(
    "cluster_agent_llm_output_tokens_total",
    "Total output tokens returned from the LLM, per mode",
    ["mode"],
)
LLM_COST_USD = Counter(
    "cluster_agent_llm_cost_usd_total",
    "Approximate cumulative LLM cost in USD, per mode",
    ["mode"],
)


def render() -> str:
    """Render all metrics in Prometheus exposition format.

    FastAPI's /metrics handler returns this with CONTENT_TYPE_LATEST.
    """
    return generate_latest(REGISTRY).decode("utf-8")
