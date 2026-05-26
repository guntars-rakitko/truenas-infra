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
