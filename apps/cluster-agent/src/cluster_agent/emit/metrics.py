"""Prometheus metrics, exposed on /metrics and scraped by both clusters'
Prometheus (kube-infra). The kube-infra `cluster-agent` dashboard and the
ClusterAgent* rules read them.

Every metric here is SET by the agent; tests/test_metrics.py fails on one
that nothing sets. Until 2026-10-10 seven declared ones were never set
(finding/open-findings, PR actions, "anthropic" tokens and cost, backup
verification, doctrine drift: modes that were never built), and a
dashboard tile read the always-0 cost counter (kube-infra MS-A2 plan Task
D6c moved it to `cluster_agent_llm_cost_usd_total`).

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

# Liveness — alert if last_success > 2× cadence ago
cluster_agent_last_success_timestamp = Gauge(
    "cluster_agent_last_success_timestamp",
    "Unix epoch of last successful run per mode",
    ["mode"],
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

# Every surface `dispatch.py` counts, pre-initialised at 0 so its series
# exists from the container's start. kube-infra's ClusterAgentDispatchErrors
# is `increase(...[6h]) > 0`, and a labelled counter has no series until its
# first `.inc()`: a series born at 1 has no earlier sample, `increase()` sees
# no rise, and the FIRST failure of a surface after each container restart
# never fired the alert (a restart comes with every deploy). With the 0 in
# place, that first failure is the rise. A surface added to `dispatch.py`
# goes here too (tests/test_metrics.py checks the two agree).
DISPATCH_SURFACES = (
    "grafana_annotation",
    "gh_issue_create",
    "gh_issue_comment",
    "gh_issue_reopen_comment",
)
for _surface in DISPATCH_SURFACES:
    DISPATCH_ERRORS.labels(surface=_surface)
del _surface

# Mode A LLM accounting. The cost counter is an estimate from the
# response's `usage` block and llm._MODEL_RATES_PER_1M; no alert enforces a
# monthly ceiling (the design's $75 is only the dashboard tile's threshold).
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

# Prompt caching (2026-05-26 cost-cut) — Anthropic charges 0.10× normal
# input rate on cache reads, 1.25× on cache creation writes. With the
# rendered prompt prefix marked cache_control=ephemeral, repeated mode
# runs within 5min reuse the cached prefix. Track separately so the
# operator can see the cache hit ratio in Grafana.
LLM_CACHE_READ_TOKENS = Counter(
    "cluster_agent_llm_cache_read_tokens_total",
    "Input tokens served from cache (0.10x rate)",
    ["mode"],
)
LLM_CACHE_CREATE_TOKENS = Counter(
    "cluster_agent_llm_cache_create_tokens_total",
    "Input tokens written to cache (1.25x rate)",
    ["mode"],
)

# (MODE_A_TICKS_SKIPPED was introduced + removed same day 2026-05-26
# with the daily-digest pivot — the 5-min polling model it instrumented
# no longer exists. The cluster_agent_mode_a_ticks_skipped_total series
# stops growing at the redeploy; old samples persist in Prometheus.)


def render() -> str:
    """Render all metrics in Prometheus exposition format.

    FastAPI's /metrics handler returns this with CONTENT_TYPE_LATEST.
    """
    return generate_latest(REGISTRY).decode("utf-8")
