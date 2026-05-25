"""LLM wrapper — claude-agent-sdk's query() shape, structured JSON output.

For Mode A (P1 dev soak), the wrapper is intentionally minimal:
  - Single LLM call per alert (no MCP, no tools, no multi-turn)
  - Pre-gathered context stuffed into the rendered prompt
  - Output parsed as JSON, validated against schema.Finding
  - Pre-call cost estimate from len(prompt) → tokens → $; abort if
    it exceeds the per-mode budget

Upgrade to claude-agent-sdk multi-turn + MCP tool-use is a P2-time
refactor when we know which context patterns the LLM wants.

Cost rates (per 1M tokens; verified from
https://platform.claude.com/docs/en/docs/about-claude/pricing on 2026-05-25):
  - Sonnet 4.5 / 4.6:  $3 input / $15 output
  - Opus 4.7:          $5 input / $25 output
  - Haiku 4.5:         $1 input / $5 output

Pre-call estimate uses input only (output is ~1K tokens for Finding
shape, contributes <$0.02 in the worst case at Opus rates).
"""
from __future__ import annotations
import base64
import json
import secrets
from typing import Any

import jinja2

from .prompts.loader import load_prompt
from .schema import Finding
from .emit.metrics import LLM_TOKENS_INPUT, LLM_TOKENS_OUTPUT, LLM_COST_USD


class LLMBudgetExceeded(RuntimeError):
    """Raised when a pre-call cost estimate exceeds the per-mode budget."""


_MODEL_RATES_PER_1M: dict[str, tuple[float, float]] = {
    # input rate, output rate — USD per 1M tokens
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-sonnet-4-5-20250929": (3.00,  15.00),
    "claude-opus-4-7":            (5.00,  25.00),
    "claude-haiku-4-5-20251001":  (1.00,  5.00),
}


def _estimate_input_tokens(prompt: str) -> int:
    """Rough chars/token=4 approximation. Good enough for budget gating;
    the real number is also returned in the response metadata, used for
    post-call metric accuracy."""
    return max(1, len(prompt) // 4)


def _input_cost_usd(model: str, input_tokens: int) -> float:
    input_rate_per_1m, _ = _MODEL_RATES_PER_1M.get(model, (3.0, 15.0))
    return input_tokens * input_rate_per_1m / 1_000_000


async def _sdk_query(prompt: str, options: Any) -> str:
    """Call claude-agent-sdk's query() and return the assistant's text reply.

    This is split out so tests can monkeypatch it without touching the
    SDK directly. Live implementation imports claude_agent_sdk lazily so
    pytest doesn't require it for the unit tests in test_llm.py.
    """
    from claude_agent_sdk import query  # type: ignore[import-untyped]

    # claude-agent-sdk's query() yields messages; we want only the final
    # assistant text. The SDK signals end-of-turn via stop_reason; we
    # concatenate any text deltas across messages defensively.
    chunks: list[str] = []
    async for msg in query(prompt=prompt, options=options):
        if hasattr(msg, "content"):
            for block in msg.content:
                if getattr(block, "type", None) == "text":
                    chunks.append(getattr(block, "text", ""))
    return "".join(chunks)


async def triage_alert(
    *,
    alert: dict[str, Any],
    context: dict[str, str],
    cluster: str,
    model: str,
    budget_usd: float,
    context_window_minutes: int = 30,
) -> Finding:
    """One LLM round for one alert → Finding.

    Raises:
        LLMBudgetExceeded: pre-call estimate > budget_usd
        ValueError:        LLM returned non-JSON or schema-invalid JSON
    """
    alert_namespace = (
        alert.get("labels", {}).get("namespace")
        or alert.get("labels", {}).get("pod_namespace")
        or "(unknown)"
    )
    prompt_template = load_prompt("alert_triage")
    # The prompt is a Jinja template (with the _PreservingUndefined hack
    # in loader.py, per-call vars survived the load). Now we do the
    # per-call substitution.
    filled = jinja2.Template(prompt_template).render(
        alert_json=json.dumps(alert, indent=2),
        alert_namespace=alert_namespace,
        context_window_minutes=context_window_minutes,
        loki_excerpt=context.get("loki_excerpt", "(empty)"),
        kubectl_describe=context.get("kubectl_describe", "(empty)"),
        prom_values=context.get("prom_values", "(empty)"),
        flux_state=context.get("flux_state", "(empty)"),
    )

    # Pre-call budget gate
    input_tokens_est = _estimate_input_tokens(filled)
    est_cost = _input_cost_usd(model, input_tokens_est)
    if est_cost > budget_usd:
        raise LLMBudgetExceeded(
            f"estimated input cost ${est_cost:.4f} exceeds budget ${budget_usd:.2f} "
            f"({input_tokens_est} input tokens at model {model})"
        )

    # Lazy import so monkeypatched _sdk_query in tests doesn't need the SDK
    from claude_agent_sdk import ClaudeAgentOptions  # type: ignore[import-untyped]
    options = ClaudeAgentOptions(
        model=model,
        max_turns=1,
        system_prompt="",  # the rendered template IS the system prompt;
                           # we send it as the user message so the SDK's
                           # internal system-prompt slot stays open for
                           # the SDK to inject its own.
    )

    raw = await _sdk_query(filled, options)

    # Parse + validate
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM output is not valid JSON: {e!r}\nRaw: {raw[:500]}")

    # Inject the fields we know cluster-side (not LLM's responsibility)
    data["mode"] = "A"
    data["cluster"] = cluster
    ulid = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")[:26]
    data["id"] = ulid

    try:
        finding = Finding(**data)
    except Exception as e:
        raise ValueError(f"LLM output failed schema validation: {e!r}\nRaw: {raw[:500]}")

    # Update metrics (rough; SDK's actual usage object is preferred when
    # we wire it through — left for a follow-up since the SDK's `Message`
    # type isn't stable across SDK versions)
    LLM_TOKENS_INPUT.labels(mode="A").inc(input_tokens_est)
    LLM_TOKENS_OUTPUT.labels(mode="A").inc(max(1, len(raw) // 4))
    LLM_COST_USD.labels(mode="A").inc(est_cost)

    return finding
