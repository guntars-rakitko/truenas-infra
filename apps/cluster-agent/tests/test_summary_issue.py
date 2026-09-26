"""Tests for the daily summary body (modes/summary_issue.py)."""
from __future__ import annotations

from cluster_agent.modes.summary_issue import render_summary_body


def _body(**kw) -> str:
    return render_summary_body(
        cluster="dev", groups=[], log_patterns=[], findings=[],
        dispatch_refs={}, window_hours=24, model="m", digest_summary="",
        **kw,
    )


def test_summary_names_log_mining_coverage_gaps():
    """An unchecked tripwire must not read like a clean one: the summary
    lists every Loki query that failed."""
    body = _body(log_mining_failures=[
        "tripwire OOMKilled: ReadTimeout",
        "ratio_outlier: RuntimeError",
    ])
    assert "Log-mining coverage gaps (2)" in body
    assert "- `tripwire OOMKilled: ReadTimeout`" in body
    assert "- `ratio_outlier: RuntimeError`" in body
    assert "not checked" in body


def test_summary_has_no_gap_section_when_every_query_ran():
    assert "coverage gaps" not in _body()
    assert "coverage gaps" not in _body(log_mining_failures=[])
