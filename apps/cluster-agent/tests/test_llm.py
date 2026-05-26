"""LLM wrapper — direct Anthropic /v1/messages call + budget + JSON parse."""
from __future__ import annotations
import json

import pytest

from cluster_agent.llm import triage_alert, LLMBudgetExceeded
from cluster_agent.schema import Finding


def _good_finding_json() -> str:
    return json.dumps({
        "severity": "medium",
        "title": "Pocket-ID pod restarted 4x in 30min",
        "summary": "Repeated OOMKills suggest the chart-default memory limit (512Mi) is too tight after the Litestream sidecar started.",
        "evidence": [
            {"type": "alert", "ref": "Alertmanager/KubePodCrashLooping@2026-05-25T17:00:00Z"},
            {"type": "log", "ref": "loki:{namespace='pocket-id'}|2026-05-25T16:30..17:00", "excerpt": "OOMKilled"},
        ],
        "root_cause_hypothesis": "Memory limit too low post-Litestream sidecar addition.",
        "confidence": 0.7,
        "recommended_action": "Bump pocket-id.values.resources.limits.memory from 512Mi to 1Gi in flux-cd/infrastructure/helmreleases/pocket-id.yaml.",
        "runbook_ref": None,
        "dedup_key": "alert:KubePodCrashLooping:pocket-id-0:dev",
    })


@pytest.mark.asyncio
async def test_triage_alert_returns_validated_finding(monkeypatch):
    """Happy path — stubbed SDK returns valid JSON, triage_alert returns a Finding."""
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return _good_finding_json()

    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    finding = await triage_alert(
        alert={"labels": {"alertname": "KubePodCrashLooping"}, "startsAt": "2026-05-25T17:00:00Z"},
        context={"loki_excerpt": "OOMKilled", "kubectl_describe": "...", "prom_values": "...", "flux_state": "..."},
        cluster="dev",
        model="claude-sonnet-4-6",
        budget_usd=0.50,
    )
    assert isinstance(finding, Finding)
    assert finding.severity == "medium"
    assert finding.dedup_key == "alert:KubePodCrashLooping:pocket-id-0:dev"
    assert finding.cluster == "dev"
    assert finding.mode == "A"


@pytest.mark.asyncio
async def test_triage_alert_invalid_json_raises(monkeypatch):
    """LLM returns non-JSON → ValueError surfaces with a useful message."""
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return "I think the issue is the pod ran out of memory."

    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    with pytest.raises(ValueError, match="not valid JSON"):
        await triage_alert(
            alert={"labels": {"alertname": "X"}},
            context={"loki_excerpt": "", "kubectl_describe": "", "prom_values": "", "flux_state": ""},
            cluster="dev",
            model="claude-sonnet-4-6",
            budget_usd=0.50,
        )


@pytest.mark.asyncio
async def test_triage_alert_handles_real_api_response_shape(monkeypatch):
    """When _sdk_query returns the real API dict shape (content/usage blocks),
    triage_alert extracts the text correctly + uses usage tokens for metrics."""
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return {
            "id": "msg_01TEST",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [
                {"type": "text", "text": _good_finding_json()}
            ],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 4321,
                "output_tokens": 456,
            },
        }

    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    finding = await triage_alert(
        alert={"labels": {"alertname": "KubePodCrashLooping"}, "startsAt": "2026-05-25T17:00:00Z"},
        context={"loki_excerpt": "OOMKilled", "kubectl_describe": "...", "prom_values": "...", "flux_state": "..."},
        cluster="dev",
        model="claude-sonnet-4-6",
        budget_usd=0.50,
    )
    assert isinstance(finding, Finding)
    assert finding.dedup_key == "alert:KubePodCrashLooping:pocket-id-0:dev"


@pytest.mark.asyncio
async def test_triage_alert_budget_exceeded_raises(monkeypatch):
    """The wrapper computes a token-based cost estimate and raises if a
    pre-call estimate (input tokens × model rate) exceeds budget."""
    from cluster_agent import llm

    async def fake_query(prompt, options):
        return _good_finding_json()

    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    # Massive context → high input-token estimate → budget exceeded
    with pytest.raises(LLMBudgetExceeded):
        await triage_alert(
            alert={"labels": {"alertname": "X"}},
            context={"loki_excerpt": "x" * 1_000_000, "kubectl_describe": "", "prom_values": "", "flux_state": ""},
            cluster="dev",
            model="claude-sonnet-4-6",
            budget_usd=0.01,
        )


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
async def test_triage_alert_handles_fenced_llm_output(monkeypatch):
    """Real-world: LLM wraps the Finding JSON in ```json...```. Parser
    survives, Finding is produced normally."""
    from cluster_agent import llm

    async def fake_query(prompt, options):
        # Wrap the good JSON in markdown fences like the LLM did on 2026-05-26
        return "```json\n" + _good_finding_json() + "\n```"

    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    finding = await triage_alert(
        alert={"labels": {"alertname": "KubePodCrashLooping"}, "startsAt": "2026-05-25T17:00:00Z"},
        context={"loki_excerpt": "x", "kubectl_describe": "x", "prom_values": "x", "flux_state": "x"},
        cluster="dev",
        model="claude-sonnet-4-6",
        budget_usd=0.50,
    )
    assert isinstance(finding, Finding)
    assert finding.severity == "medium"


@pytest.mark.asyncio
async def test_triage_alert_splits_prompt_at_cache_boundary(monkeypatch):
    """Cost-cut (2026-05-26): triage_alert must split the rendered prompt
    at the '## Active alert' header and pass it to _sdk_query as a
    (cached_prefix, uncached_suffix) tuple. The static prefix carries
    the system role + house_style + output_schema and is cached by
    Anthropic for the 5-min TTL → cache reads charged at 0.10x rate."""
    from cluster_agent import llm
    captured = {}

    async def fake_query(prompt, options):
        captured["prompt"] = prompt
        return _good_finding_json()

    monkeypatch.setattr(llm, "_sdk_query", fake_query)
    await triage_alert(
        alert={"labels": {"alertname": "KubePodCrashLooping"}, "startsAt": "2026-05-25T17:00:00Z"},
        context={"loki_excerpt": "x", "kubectl_describe": "x", "prom_values": "x", "flux_state": "x"},
        cluster="dev",
        model="claude-sonnet-4-6",
        budget_usd=0.50,
    )
    # Prompt arrived as a 2-tuple
    assert isinstance(captured["prompt"], tuple)
    prefix, suffix = captured["prompt"]
    # The static portion ends at the boundary marker; the variable
    # portion BEGINS with it (which is where the {{ alert_json }} and
    # context excerpts live).
    assert "## Active alert" not in prefix
    assert suffix.lstrip().startswith("## Active alert")
    # The variable portion contains the rendered alert payload
    assert "KubePodCrashLooping" in suffix


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
async def test_triage_alert_tracks_cache_token_usage(monkeypatch):
    """When the API response carries cache_creation_input_tokens /
    cache_read_input_tokens (i.e. caching is live), triage_alert
    increments the LLM_CACHE_CREATE_TOKENS / LLM_CACHE_READ_TOKENS
    counters AND applies the 1.25x/0.10x cache pricing to the cost
    metric. Without this, the operator sees cache hits in the API
    response but no cost-savings reflection in /metrics."""
    from cluster_agent import llm
    from cluster_agent.emit.metrics import (
        LLM_CACHE_READ_TOKENS, LLM_CACHE_CREATE_TOKENS, LLM_COST_USD,
    )

    async def fake_query(prompt, options):
        return {
            "content": [{"type": "text", "text": _good_finding_json()}],
            "usage": {
                "input_tokens": 200,           # bare input (uncached)
                "output_tokens": 500,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1500,   # cache hit
            },
        }

    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    cache_read_before = LLM_CACHE_READ_TOKENS.labels(mode="A")._value.get()
    cost_before = LLM_COST_USD.labels(mode="A")._value.get()

    await triage_alert(
        alert={"labels": {"alertname": "KubePodCrashLooping"}, "startsAt": "2026-05-25T17:00:00Z"},
        context={"loki_excerpt": "x", "kubectl_describe": "x", "prom_values": "x", "flux_state": "x"},
        cluster="dev",
        model="claude-sonnet-4-6",
        budget_usd=0.50,
    )

    # 1500 cache-read tokens went into the counter
    assert LLM_CACHE_READ_TOKENS.labels(mode="A")._value.get() - cache_read_before == 1500
    # Cost = 200 input @ $3/M + 1500 cache-read @ $3 * 0.10/M + 500 output @ $15/M
    #      = 0.0006 + 0.00045 + 0.0075 = 0.00855
    expected = (200 * 3.0 / 1_000_000) + (1500 * 3.0 * 0.10 / 1_000_000) + (500 * 15.0 / 1_000_000)
    actual_delta = LLM_COST_USD.labels(mode="A")._value.get() - cost_before
    assert abs(actual_delta - expected) < 1e-9


@pytest.mark.asyncio
async def test_triage_alert_overrides_llm_cluster_suffix(monkeypatch):
    """LLM is non-deterministic about the cluster suffix in dedup_key
    (observed picking ':prd' and ':monitoring' for dev-cluster alerts
    on consecutive runs during the 2026-05-26 P1 soak). triage_alert
    overwrites the suffix with the actual cluster to keep dedup
    consistent across runs."""
    from cluster_agent import llm
    import json as _json

    # LLM returns dedup_key with WRONG cluster suffix (:prd)
    bad_json = _json.loads(_good_finding_json())
    bad_json["dedup_key"] = "alert:KubePodCrashLooping:pocket-id-0:prd"

    async def fake_query(prompt, options):
        return _json.dumps(bad_json)

    monkeypatch.setattr(llm, "_sdk_query", fake_query)

    # Run for cluster=dev — should normalize suffix to :dev
    finding = await triage_alert(
        alert={"labels": {"alertname": "KubePodCrashLooping"}, "startsAt": "2026-05-25T17:00:00Z"},
        context={"loki_excerpt": "x", "kubectl_describe": "x", "prom_values": "x", "flux_state": "x"},
        cluster="dev",
        model="claude-sonnet-4-6",
        budget_usd=0.50,
    )
    assert finding.dedup_key == "alert:KubePodCrashLooping:pocket-id-0:dev"
    # The non-cluster parts (alertname + scope) preserved
    assert finding.dedup_key.startswith("alert:KubePodCrashLooping:pocket-id-0:")
