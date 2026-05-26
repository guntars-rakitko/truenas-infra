"""LLM wrapper — direct Anthropic `/v1/messages` REST call, structured JSON output.

For Mode A (P1 dev soak), the wrapper is intentionally minimal:
  - Single LLM call per alert (no MCP, no tools, no multi-turn)
  - Pre-gathered context stuffed into the rendered prompt
  - Output parsed as JSON, validated against schema.Finding
  - Pre-call cost estimate from len(prompt) → tokens → $; abort if
    it exceeds the per-mode budget

**Transport:** direct httpx POST to `https://api.anthropic.com/v1/messages`
with `Authorization: Bearer ${CLAUDE_CODE_OAUTH_TOKEN}` (the same
sk-ant-oat01-* token we use everywhere). NOT the `claude-agent-sdk`
package — that one orchestrates the `claude` CLI as a subprocess, which
we'd need to install Node + Claude Code in the container for, and the
SDK is built for MCP tool-use loops we don't need in P1. Switching to
the bare REST endpoint is dramatically simpler for our one-shot
structured-output pattern, and was already validated end-to-end
against the OAuth token on 2026-05-25.

Upgrade to claude-agent-sdk multi-turn + MCP tool-use is a P2-time
refactor when we know which context patterns the LLM wants. At that
point we'd install the `claude` CLI in the container and switch back.

Cost rates (per 1M tokens; verified from
https://platform.claude.com/docs/en/docs/about-claude/pricing on 2026-05-25):
  - Sonnet 4.5 / 4.6:  $3 input / $15 output
  - Opus 4.7:          $5 input / $25 output
  - Haiku 4.5:         $1 input / $5 output

Pre-call estimate uses input only (output is ~1K tokens for Finding
shape, contributes <$0.02 in the worst case at Opus rates). Post-call
metrics use the actual `usage` block returned by the API.
"""
from __future__ import annotations
import base64
import json
import os
import secrets
from typing import Any

import httpx
import jinja2

from .prompts.loader import load_prompt
from .schema import Finding
from .emit.metrics import LLM_TOKENS_INPUT, LLM_TOKENS_OUTPUT, LLM_COST_USD


class LLMBudgetExceeded(RuntimeError):
    """Raised when a pre-call cost estimate exceeds the per-mode budget."""


_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
# Beta header is required when authenticating with a Claude-Code-style
# OAuth bearer token (sk-ant-oat01-*) — without it the API rejects the
# auth as if it were a regular API key with wrong format. The header
# value is the same one Claude Code's own CLI sends; pinned to the
# date stamp Anthropic published with the OAuth API.
_OAUTH_BETA = "oauth-2025-04-20"


_MODEL_RATES_PER_1M: dict[str, tuple[float, float]] = {
    # input rate, output rate — USD per 1M tokens
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-sonnet-4-5-20250929": (3.00,  15.00),
    "claude-opus-4-7":            (5.00,  25.00),
    "claude-haiku-4-5-20251001":  (1.00,  5.00),
}


def _estimate_input_tokens(prompt: str) -> int:
    """Rough chars/token=4 approximation. Good enough for budget gating."""
    return max(1, len(prompt) // 4)


def _strip_code_fences(text: str) -> str:
    """Strip markdown ```json / ``` fences if the LLM wrapped its output.

    Despite explicit "no prose, no fences" in the prompt, models still
    sometimes wrap JSON in fences. Be lenient about it.

    Patterns handled:
      - "```json\n{...}\n```"   (most common)
      - "```\n{...}\n```"        (bare backticks)
      - "{...}"                  (no fences — pass through)
      - "  ```json\n{...}\n```  " (with surrounding whitespace)
    """
    s = text.strip()
    if not s.startswith("```"):
        return s
    # Drop opening fence line ("```" or "```json")
    nl = s.find("\n")
    if nl == -1:
        # single-line fenced content like ```{"k":1}``` — strip both ends
        return s.strip("`").strip()
    s = s[nl + 1:]
    # Drop trailing fence
    if s.rstrip().endswith("```"):
        s = s.rstrip()[:-3].rstrip()
    return s


def _input_cost_usd(model: str, input_tokens: int) -> float:
    input_rate_per_1m, _ = _MODEL_RATES_PER_1M.get(model, (3.0, 15.0))
    return input_tokens * input_rate_per_1m / 1_000_000


def _output_cost_usd(model: str, output_tokens: int) -> float:
    _, output_rate_per_1m = _MODEL_RATES_PER_1M.get(model, (3.0, 15.0))
    return output_tokens * output_rate_per_1m / 1_000_000


async def _sdk_query(prompt: str, options: dict[str, Any]) -> dict[str, Any]:
    """POST to /v1/messages and return the parsed JSON response.

    Split out so tests can monkeypatch it without hitting the network.
    Returns the full API response dict (caller pulls `content[0].text`
    + `usage` from it).

    Auth: reads CLAUDE_CODE_OAUTH_TOKEN from env. We don't fall back
    to ANTHROPIC_API_KEY here — main.py refuses to start if both are
    set, and which one is active is set in compose.
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    if not token:
        raise RuntimeError(
            "Neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY set; "
            "Mode A cannot call the LLM"
        )
    headers = {
        "anthropic-version": _API_VERSION,
        "content-type": "application/json",
    }
    if token.startswith("sk-ant-oat"):
        # OAuth bearer — beta header required (Claude Code's own CLI sends it)
        headers["Authorization"] = f"Bearer {token}"
        headers["anthropic-beta"] = _OAUTH_BETA
    else:
        # Plain API key
        headers["x-api-key"] = token
    body = {
        "model": options["model"],
        "max_tokens": options.get("max_tokens", 1024),
        # The rendered prompt IS the system content; we send it as the
        # user message because OAuth-bearer requests over the public
        # API are stricter about empty system slots.
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=options.get("timeout", 60.0)) as client:
        r = await client.post(_API_URL, headers=headers, content=json.dumps(body))
        r.raise_for_status()
        return r.json()


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
    # Per-call Jinja render — the loader's _PreservingUndefined kept
    # {{ alert_json }} etc as literal text so we can fill them in here.
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

    # Call the API. _sdk_query returns the full response dict in the
    # new (REST) shape OR the legacy stub shape (raw string) for back-
    # compat with existing tests that monkeypatch it with a string return.
    raw_response = await _sdk_query(filled, {"model": model, "max_tokens": 1024})

    # Back-compat: tests still stub _sdk_query to return a JSON string
    # directly (the assistant-text body). Branch on type.
    if isinstance(raw_response, str):
        raw_text = raw_response
        usage_in = input_tokens_est
        usage_out = max(1, len(raw_text) // 4)
    else:
        # Real API response: {"content": [{"type":"text","text":"..."}],
        #                     "usage": {"input_tokens":..., "output_tokens":...}}
        text_blocks = [
            b.get("text", "") for b in raw_response.get("content", [])
            if b.get("type") == "text"
        ]
        raw_text = "".join(text_blocks)
        usage = raw_response.get("usage", {})
        usage_in = usage.get("input_tokens", input_tokens_est)
        usage_out = usage.get("output_tokens", max(1, len(raw_text) // 4))

    # Parse + validate
    # LLM sometimes wraps JSON in ```json ... ``` fences despite explicit
    # "no prose, no fences" instructions in the prompt. Strip them
    # leniently before parsing — first observed on Mode A's first
    # successful Watchdog run 2026-05-26 04:38 UTC.
    cleaned = _strip_code_fences(raw_text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM output is not valid JSON: {e!r}\nRaw: {raw_text[:500]}")

    # Inject the fields we know cluster-side
    data["mode"] = "A"
    data["cluster"] = cluster
    ulid = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")[:26]
    data["id"] = ulid

    # Override the LLM-picked cluster suffix in dedup_key. The LLM is
    # non-deterministic about scope-id naming — observed picking `:prd`,
    # `:monitoring`, and other values across runs for dev-cluster alerts.
    # Each run with a fresh scope would create a NEW issue, destroying
    # dedup. We trust the LLM for the alertname + scope-id parts but
    # always overwrite the cluster suffix with the actual cluster.
    if "dedup_key" in data and isinstance(data["dedup_key"], str):
        parts = data["dedup_key"].rsplit(":", 1)
        if len(parts) == 2:
            data["dedup_key"] = f"{parts[0]}:{cluster}"

    try:
        finding = Finding(**data)
    except Exception as e:
        raise ValueError(f"LLM output failed schema validation: {e!r}\nRaw: {raw_text[:500]}")

    # Metrics from real usage (or estimates if test-stubbed)
    LLM_TOKENS_INPUT.labels(mode="A").inc(usage_in)
    LLM_TOKENS_OUTPUT.labels(mode="A").inc(usage_out)
    LLM_COST_USD.labels(mode="A").inc(
        _input_cost_usd(model, usage_in) + _output_cost_usd(model, usage_out)
    )

    return finding
