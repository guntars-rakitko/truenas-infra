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
from .schema import Finding, Report
from .emit.metrics import (
    LLM_TOKENS_INPUT, LLM_TOKENS_OUTPUT, LLM_COST_USD,
    LLM_CACHE_READ_TOKENS, LLM_CACHE_CREATE_TOKENS,
)


# Anthropic prompt-caching multipliers (vs base input rate, per docs:
# https://docs.claude.com/en/docs/build-with-claude/prompt-caching).
# Writing to cache costs more than a normal token, but the breakeven
# is ~2 reads. Our 5-min cron with 5-min cache TTL gives us 1 read per
# write on average; longer-running clusters (alert keeps firing for 1h)
# get many reads per write, so the savings compound.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10


# The split boundary inside the alert_triage.md template. Everything
# above this header line (system role + cluster context + included
# house_style + output_schema) is identical across every Mode A call
# and is sent as a cached prefix. Everything below contains the
# per-call {{ alert_json }} / {{ kubectl_describe }} / ... substitutions
# and is sent uncached.
_CACHE_BOUNDARY_MARKER = "\n## Active alert\n"

# Same idea for the daily-digest prompt. Boundary is the `## Cluster:`
# header — everything above (role, estate context, alertname guidance,
# output rules, selection criteria, house_style include) is identical
# across every digest call. The cached prefix is ~2300 tokens — well
# above Anthropic's 1024-token minimum for caching to kick in.
_DIGEST_CACHE_BOUNDARY_MARKER = "\n## Cluster: `"


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
    # Two-content-block message (2026-05-26 cost-cut): the static prefix
    # of the prompt (system role + cluster context + house_style + output
    # schema — everything up to "## Active alert") is marked with
    # cache_control=ephemeral so Anthropic caches it for 5min. The variable
    # tail (the alert + its context excerpts) is sent uncached. With our
    # 5-min cron cadence we expect ~1 cache read per cache write per
    # cluster, falling to 0 reads/write when the cluster is quiet (cache
    # expires before next call) but climbing to many reads/write when the
    # cluster is busy and Mode A fires repeatedly.
    #
    # Caller passes prompt as either a string (legacy — wrap in single
    # uncached block) or a tuple (cached_prefix, uncached_suffix).
    if isinstance(prompt, tuple):
        cached_prefix, uncached_suffix = prompt
        content_blocks = [
            {"type": "text", "text": cached_prefix, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": uncached_suffix},
        ]
    else:
        content_blocks = [{"type": "text", "text": prompt}]
    body = {
        "model": options["model"],
        "max_tokens": options.get("max_tokens", 1024),
        # The rendered prompt IS the system content; we send it as the
        # user message because OAuth-bearer requests over the public
        # API are stricter about empty system slots.
        "messages": [{"role": "user", "content": content_blocks}],
    }
    async with httpx.AsyncClient(timeout=options.get("timeout", 60.0)) as client:
        r = await client.post(_API_URL, headers=headers, content=json.dumps(body))
        # On 429 (rate limit) or other 4xx/5xx, surface Anthropic's
        # rate-limit headers in the exception message. Without these,
        # 429s are indistinguishable from budget-exhaustion failures
        # and operator can't tell which bucket (requests/tokens) tripped.
        # First bit us 2026-05-26 when shared-Max-pool OAuth tripped
        # the per-minute limit while interactive Claude Code was busy.
        if r.status_code >= 400:
            rl_keys = [
                "anthropic-ratelimit-requests-limit",
                "anthropic-ratelimit-requests-remaining",
                "anthropic-ratelimit-requests-reset",
                "anthropic-ratelimit-tokens-limit",
                "anthropic-ratelimit-tokens-remaining",
                "anthropic-ratelimit-tokens-reset",
                "retry-after",
            ]
            rl = {k: r.headers[k] for k in rl_keys if k in r.headers}
            # Raise with the rate-limit info woven into the message —
            # captured by the audit log + propagates to Loki via the
            # mode runner's exception handler.
            raise httpx.HTTPStatusError(
                f"{r.status_code} {r.reason_phrase} — body={r.text[:300]} rate_limits={rl}",
                request=r.request,
                response=r,
            )
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

    # Split the rendered prompt at the "## Active alert" boundary so the
    # prefix (system role + cluster context + included house_style +
    # output_schema — all static across every Mode A call) can be cached.
    # If the marker isn't found (someone edited the template without
    # respecting the boundary contract), fall back to sending the whole
    # prompt uncached — correctness over cost optimization.
    split_at = filled.find(_CACHE_BOUNDARY_MARKER)
    if split_at == -1:
        prompt_payload: str | tuple[str, str] = filled
    else:
        prompt_payload = (filled[:split_at], filled[split_at:])

    # Call the API. _sdk_query returns the full response dict in the
    # new (REST) shape OR the legacy stub shape (raw string) for back-
    # compat with existing tests that monkeypatch it with a string return.
    # max_tokens=4096 — bumped from 1024 after observing truncation on
    # complex prd findings (KubeJobFailed alert produced a partial JSON
    # with `Unterminated string` mid-summary 2026-05-26). A complete
    # Finding is typically ~600-1200 output tokens; complex multi-evidence
    # findings can push 2000+. 4096 leaves headroom without meaningful
    # cost impact (output tokens dominate cost but Sonnet 4.6 caps the
    # actual generation at what the model decides — max_tokens is a
    # ceiling, not a target).
    raw_response = await _sdk_query(prompt_payload, {"model": model, "max_tokens": 4096})

    # Back-compat: tests still stub _sdk_query to return a JSON string
    # directly (the assistant-text body). Branch on type.
    cache_read = 0
    cache_create = 0
    if isinstance(raw_response, str):
        raw_text = raw_response
        usage_in = input_tokens_est
        usage_out = max(1, len(raw_text) // 4)
    else:
        # Real API response: {"content": [{"type":"text","text":"..."}],
        #                     "usage": {"input_tokens":..., "output_tokens":...,
        #                               "cache_creation_input_tokens":...,
        #                               "cache_read_input_tokens":...}}
        text_blocks = [
            b.get("text", "") for b in raw_response.get("content", [])
            if b.get("type") == "text"
        ]
        raw_text = "".join(text_blocks)
        usage = raw_response.get("usage", {})
        usage_in = usage.get("input_tokens", input_tokens_est)
        usage_out = usage.get("output_tokens", max(1, len(raw_text) // 4))
        cache_create = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

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

    # Metrics from real usage (or estimates if test-stubbed). Cache-tier
    # cost accounting follows Anthropic's pricing:
    #   - usage_in is BARE-input (uncached) tokens — counts at base rate
    #   - cache_create tokens count at 1.25x base rate (one-time write cost)
    #   - cache_read tokens count at 0.10x base rate (the big savings)
    LLM_TOKENS_INPUT.labels(mode="A").inc(usage_in)
    LLM_TOKENS_OUTPUT.labels(mode="A").inc(usage_out)
    LLM_CACHE_READ_TOKENS.labels(mode="A").inc(cache_read)
    LLM_CACHE_CREATE_TOKENS.labels(mode="A").inc(cache_create)
    base_input_rate, _ = _MODEL_RATES_PER_1M.get(model, (3.0, 15.0))
    cost = (
        _input_cost_usd(model, usage_in)
        + cache_create * base_input_rate * _CACHE_WRITE_MULT / 1_000_000
        + cache_read * base_input_rate * _CACHE_READ_MULT / 1_000_000
        + _output_cost_usd(model, usage_out)
    )
    LLM_COST_USD.labels(mode="A").inc(cost)

    return finding


async def triage_digest(
    *,
    cluster: str,
    alert_groups: list,            # list[AlertGroup] — typed loosely to avoid circular import
    open_issue_keys: list[str],
    context_excerpts: str,
    window_hours: int,
    model: str,
    budget_usd: float,
) -> Report:
    """Daily-digest LLM call → curated Report.

    ONE call per cluster per day. Input is pre-aggregated (AlertGroups
    summarize 24h of activity, not raw fire events) + open-issue dedup
    keys + selected log/metric excerpts for chronic groups only. Output
    is a `Report` containing 0..N actionable Findings.

    Cost shape vs per-alert triage:
      - Larger per-call input (~10-30K tokens vs ~5K) — bigger context
      - Output similar size per Finding emitted (~600-1500 tokens) but
        FEWER Findings per day (digest is curated, not exhaustive)
      - Static prefix ~2300 tokens — cleanly above the 1024 cache
        threshold, so the 2 daily calls (dev + prd) can share a cache
        read if scheduled close together
    """
    prompt_template = load_prompt("digest")
    groups_json = json.dumps(
        [g.model_dump(mode="json") for g in alert_groups],
        indent=2, default=str,
    )
    window_end = "now (UTC)"
    filled = jinja2.Template(prompt_template).render(
        cluster=cluster,
        window_hours=window_hours,
        window_end=window_end,
        alert_groups_json=groups_json,
        open_issue_keys_json=json.dumps(open_issue_keys, indent=2),
        context_excerpts=context_excerpts or "(no context excerpts gathered)",
    )

    # Pre-call budget gate. Digest input can be substantial, so we
    # compute the estimated cost (ignoring expected cache discount —
    # cache may not apply on the very first call after a deploy).
    input_tokens_est = _estimate_input_tokens(filled)
    est_cost = _input_cost_usd(model, input_tokens_est)
    if est_cost > budget_usd:
        raise LLMBudgetExceeded(
            f"digest estimated input cost ${est_cost:.4f} exceeds budget "
            f"${budget_usd:.2f} ({input_tokens_est} input tokens at model {model})"
        )

    # Split for caching at the per-call boundary
    split_at = filled.find(_DIGEST_CACHE_BOUNDARY_MARKER)
    prompt_payload: str | tuple[str, str]
    if split_at == -1:
        prompt_payload = filled
    else:
        prompt_payload = (filled[:split_at], filled[split_at:])

    # max_tokens=3000 — observed dev digest output ~1500 tokens; prd
    # ~2500 even on noisy days. 3000 gives ~20% headroom without
    # over-reserving against Tier-1 OTPM (10K/min) — bumping to 8192
    # triggered persistent 429s during 2026-05-26 manual smoke tests
    # because Anthropic charges the reservation against the per-minute
    # output budget. A real truncation will show up as JSON parse
    # failure (LLMBudgetExceeded won't fire on tokens, only $); we
    # can raise this back up if we ever see one.
    raw_response = await _sdk_query(prompt_payload, {"model": model, "max_tokens": 3000})

    cache_read = 0
    cache_create = 0
    if isinstance(raw_response, str):
        raw_text = raw_response
        usage_in = input_tokens_est
        usage_out = max(1, len(raw_text) // 4)
    else:
        text_blocks = [
            b.get("text", "") for b in raw_response.get("content", [])
            if b.get("type") == "text"
        ]
        raw_text = "".join(text_blocks)
        usage = raw_response.get("usage", {})
        usage_in = usage.get("input_tokens", input_tokens_est)
        usage_out = usage.get("output_tokens", max(1, len(raw_text) // 4))
        cache_create = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

    cleaned = _strip_code_fences(raw_text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Digest LLM output is not valid JSON: {e!r}\nRaw: {raw_text[:500]}"
        )

    # Inject runtime-known fields the LLM is told to placeholder
    data["mode"] = "A"
    data["cluster"] = cluster
    data["id"] = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")[:26]
    # Each Finding inside the Report gets the same runtime overrides
    for f in data.get("findings", []) or []:
        f["mode"] = "A"
        f["cluster"] = cluster
        f["id"] = base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=")[:26]
        # Same dedup-key cluster-suffix normalization the per-alert
        # triage applies — the LLM is non-deterministic about scope
        # naming, and we want consistent state.db keys.
        if "dedup_key" in f and isinstance(f["dedup_key"], str):
            parts = f["dedup_key"].rsplit(":", 1)
            if len(parts) == 2:
                f["dedup_key"] = f"{parts[0]}:{cluster}"

    try:
        report = Report(**data)
    except Exception as e:
        raise ValueError(f"Digest output failed schema validation: {e!r}\nRaw: {raw_text[:500]}")

    # Metrics (shared with per-alert triage — mode label still "A")
    LLM_TOKENS_INPUT.labels(mode="A").inc(usage_in)
    LLM_TOKENS_OUTPUT.labels(mode="A").inc(usage_out)
    LLM_CACHE_READ_TOKENS.labels(mode="A").inc(cache_read)
    LLM_CACHE_CREATE_TOKENS.labels(mode="A").inc(cache_create)
    base_input_rate, _ = _MODEL_RATES_PER_1M.get(model, (3.0, 15.0))
    cost = (
        _input_cost_usd(model, usage_in)
        + cache_create * base_input_rate * _CACHE_WRITE_MULT / 1_000_000
        + cache_read * base_input_rate * _CACHE_READ_MULT / 1_000_000
        + _output_cost_usd(model, usage_out)
    )
    LLM_COST_USD.labels(mode="A").inc(cost)

    return report
