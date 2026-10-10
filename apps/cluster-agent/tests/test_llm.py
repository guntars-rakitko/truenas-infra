"""LLM wrapper — direct Anthropic /v1/messages call + budget + JSON parse.

The digest is the only caller since the per-alert `triage_alert` went
(2026-10-10); its parse, budget, cache and metric tests moved here.
"""
from __future__ import annotations
import datetime as dt
import json

import pytest

from cluster_agent.llm import triage_digest, LLMBudgetExceeded
from cluster_agent.schema import AlertGroup, KnownEvent, LogPattern, Report


_T = dt.datetime(2026, 10, 10, 14, 0, tzinfo=dt.timezone.utc)


def _group() -> AlertGroup:
    return AlertGroup(
        alertname="KubePodCrashLooping", fingerprint="abc123",
        labels={"alertname": "KubePodCrashLooping", "namespace": "pocket-id", "pod": "pocket-id-0"},
        fire_count=1, total_firing_seconds=7200, chronicity="chronic",
        first_seen_at=_T, last_seen_at=_T, currently_firing=True,
        signal_id="A1", tracked_by="guntars-rakitko/kube-infra#1600",
    )


def _report_json(**finding_overrides) -> str:
    finding = {
        "severity": "medium",
        "title": "Pocket-ID pod restarted 4x in 30min",
        "summary": "Repeated OOMKills after the Litestream sidecar started.",
        "evidence": [
            {"type": "alert", "ref": "Alertmanager/KubePodCrashLooping@2026-10-10T14:00:00Z"},
        ],
        "root_cause_hypothesis": "Memory limit too low.",
        "confidence": 0.7,
        "recommended_action": "Raise the pocket-id memory limit to 1Gi.",
        "runbook_ref": None,
        "dedup_key": "alert:KubePodCrashLooping:pocket-id:dev",
        "signals": ["A1"],
    }
    finding.update(finding_overrides)
    return json.dumps({
        "id": "x" * 26, "cluster": "dev", "digest_window_hours": 24,
        "summary": "One chronic crash loop.", "quiet_period": False,
        "findings": [finding],
        "total_alerts_24h": 1, "chronic_count": 1, "transient_count": 0, "self_healed_count": 0,
    })


async def _digest(**overrides) -> Report:
    kwargs = dict(
        cluster="dev", alert_groups=[_group()], log_patterns=[], known_events=[],
        open_issue_keys=[], context_excerpts="", window_hours=24,
        model="claude-sonnet-4-6", budget_usd=0.50,
    )
    kwargs.update(overrides)
    return await triage_digest(**kwargs)


@pytest.mark.asyncio
async def test_triage_digest_returns_a_validated_report_from_the_real_api_shape(monkeypatch):
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return {
            "content": [{"type": "text", "text": _report_json()}],
            "usage": {"input_tokens": 1200, "output_tokens": 300},
        }
    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    report = await _digest()
    assert isinstance(report, Report)
    assert report.cluster == "dev"
    assert len(report.id) == 26 and report.id != "x" * 26   # the runtime's id, not the LLM's
    (finding,) = report.findings
    assert finding.mode == "A" and finding.cluster == "dev"
    assert finding.signals == ["A1"]


@pytest.mark.asyncio
async def test_triage_digest_invalid_json_raises(monkeypatch):
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return "this is not json"
    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    with pytest.raises(ValueError, match="not valid JSON"):
        await _digest()


@pytest.mark.asyncio
async def test_triage_digest_budget_exceeded_raises_before_any_call(monkeypatch):
    from cluster_agent import llm

    async def boom(prompt, options):
        raise AssertionError("the LLM must not be called over budget")
    monkeypatch.setattr(llm, "_sdk_query", boom)
    with pytest.raises(LLMBudgetExceeded):
        await _digest(budget_usd=0.000001)


@pytest.mark.asyncio
async def test_triage_digest_handles_fenced_output(monkeypatch):
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return "```json\n" + _report_json() + "\n```"
    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    report = await _digest()
    assert len(report.findings) == 1


@pytest.mark.asyncio
async def test_triage_digest_forces_the_cluster_suffix_of_the_llm_key(monkeypatch):
    """The LLM's key is the fallback (the runner builds keys from signals),
    and its cluster suffix is still forced to the real cluster."""
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return _report_json(dedup_key="alert:KubePodCrashLooping:pocket-id:prd")
    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    report = await _digest()
    assert report.findings[0].dedup_key == "alert:KubePodCrashLooping:pocket-id:dev"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected", [
    (None, []),
    (7, []),
    ("A1, l2", ["A1", "L2"]),
    (["a1", " L2 ", 3, ""], ["A1", "L2"]),
])
async def test_a_malformed_signals_list_never_drops_the_report(monkeypatch, raw, expected):
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return _report_json(signals=raw)
    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    report = await _digest()
    assert report.findings[0].signals == expected


@pytest.mark.asyncio
async def test_triage_digest_splits_the_prompt_at_the_cluster_header(monkeypatch):
    """The static prefix (everything above `## Cluster:`) is the cached
    block; every per-cluster substitution lands in the uncached suffix."""
    from cluster_agent import llm
    captured = {}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        return _report_json()
    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    await _digest()
    prefix, suffix = captured["prompt"]
    assert "## Cluster: `" not in prefix
    assert suffix.lstrip().startswith("## Cluster: `dev`")
    # The alert group's own data (its fingerprint) is in the suffix only.
    assert "abc123" in suffix and "abc123" not in prefix


@pytest.mark.asyncio
async def test_signal_ids_tracked_issues_and_known_events_reach_the_prompt(monkeypatch):
    from cluster_agent import llm
    captured = {}

    async def fake_query(prompt, options):
        captured["suffix"] = prompt[1]
        return _report_json()
    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    pattern = LogPattern(
        namespace="giks-db", level="fatal", chronicity="tripwire", count_24h=3,
        matched_tripwire="fatal", first_seen_at=_T, last_seen_at=_T, signal_id="L1",
    )
    event = KnownEvent(kind="node_boot", at=_T, what="node msa2-dev-01 booted", signal_id="E1")
    await _digest(log_patterns=[pattern], known_events=[event],
                  known_event_failures=["known events helm upgrades: ToolError"])

    suffix = captured["suffix"]
    groups = json.loads(suffix.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert groups[0]["signal_id"] == "A1"
    assert groups[0]["tracked_by"] == "guntars-rakitko/kube-infra#1600"
    assert '"signal_id": "L1"' in suffix
    assert '"signal_id": "E1"' in suffix and "node msa2-dev-01 booted" in suffix
    assert "known events helm upgrades: ToolError" in suffix


@pytest.mark.asyncio
async def test_no_known_events_and_no_failures_render_cleanly(monkeypatch):
    from cluster_agent import llm
    captured = {}

    async def fake_query(prompt, options):
        captured["suffix"] = prompt[1]
        return _report_json()
    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    await _digest(known_events=None)
    assert "{{" not in captured["suffix"] and "{%" not in captured["suffix"]
    assert "Sources that FAILED" not in captured["suffix"]


@pytest.mark.asyncio
async def test_triage_digest_tracks_cache_token_usage_and_cost(monkeypatch):
    """Cache reads count at 0.10x the input rate, writes at 1.25x."""
    from cluster_agent import llm
    from cluster_agent.emit.metrics import LLM_CACHE_READ_TOKENS, LLM_COST_USD

    async def fake_query(prompt, options):
        return {
            "content": [{"type": "text", "text": _report_json()}],
            "usage": {"input_tokens": 200, "output_tokens": 500,
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 1500},
        }
    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    read_before = LLM_CACHE_READ_TOKENS.labels(mode="A")._value.get()
    cost_before = LLM_COST_USD.labels(mode="A")._value.get()
    await _digest()
    assert LLM_CACHE_READ_TOKENS.labels(mode="A")._value.get() - read_before == 1500
    expected = (200 * 3.0 + 1500 * 3.0 * 0.10 + 500 * 15.0) / 1_000_000
    assert abs(LLM_COST_USD.labels(mode="A")._value.get() - cost_before - expected) < 1e-9


# ── Code fence stripping ─────────────────────────────────────────────────

def test_strip_code_fences_json_block():
    """The LLM sometimes wraps JSON in ```json...``` fences despite the
    prompt saying not to. Parser strips them leniently."""
    from cluster_agent.llm import _strip_code_fences
    raw = '```json\n{"k": 1}\n```'
    assert _strip_code_fences(raw) == '{"k": 1}'


def test_strip_code_fences_bare_backticks():
    """Bare ``` fences (no language tag) also stripped."""
    from cluster_agent.llm import _strip_code_fences
    raw = '```\n{"k": 1}\n```'
    assert _strip_code_fences(raw) == '{"k": 1}'


def test_strip_code_fences_no_fences_passthrough():
    """Output without fences passes through unchanged."""
    from cluster_agent.llm import _strip_code_fences
    raw = '{"k": 1}'
    assert _strip_code_fences(raw) == '{"k": 1}'


def test_strip_code_fences_with_surrounding_whitespace():
    """Leading/trailing whitespace around fences is tolerated."""
    from cluster_agent.llm import _strip_code_fences
    raw = '  \n```json\n{"k": 1}\n```  \n'
    # The function caller .strip()s first, so test that flow:
    assert _strip_code_fences(raw.strip()) == '{"k": 1}'


@pytest.mark.asyncio
async def test_sdk_query_marks_cache_control_on_prefix(monkeypatch):
    """When _sdk_query receives a (prefix, suffix) tuple, the outgoing
    HTTP request body must have exactly TWO content blocks, the first
    one carrying cache_control={'type':'ephemeral'}."""
    from cluster_agent import llm
    import httpx

    captured = {}

    class FakeResponse:
        status_code = 200
        headers: dict[str, str] = {}
        text = ""
        reason_phrase = "OK"

        def json(self):
            return {
                "content": [{"type": "text", "text": '{"k":1}'}],
                "usage": {"input_tokens": 100, "output_tokens": 10,
                          "cache_creation_input_tokens": 800,
                          "cache_read_input_tokens": 0},
            }

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, headers=None, content=None):
            captured["body"] = json.loads(content)
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-test")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    await llm._sdk_query(("STATIC PREFIX", "VARIABLE SUFFIX"), {"model": "claude-sonnet-4-6"})

    content = captured["body"]["messages"][0]["content"]
    assert isinstance(content, list)
    assert len(content) == 2
    assert content[0]["text"] == "STATIC PREFIX"
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert content[1]["text"] == "VARIABLE SUFFIX"
    assert "cache_control" not in content[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "", "   "])
async def test_a_finding_without_a_key_does_not_drop_the_report(monkeypatch, key):
    """The prompt says the runtime replaces the key; a finding that leaves it
    out gets a placeholder of its own, never one shared with another."""
    from cluster_agent import llm
    payload = json.loads(_report_json())
    if key is None:
        del payload["findings"][0]["dedup_key"]
    else:
        payload["findings"][0]["dedup_key"] = key

    async def fake_query(prompt, options):
        return json.dumps(payload)
    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    (finding,) = (await _digest()).findings
    assert finding.dedup_key == f"llm:unkeyed:{finding.id}:dev"
