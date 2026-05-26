# cluster-agent P3 — log-pattern mining (Mode A extension)

**Status:** 📝 Draft — open design questions for operator below.
Implementation gated on resolving them.

## 1. Goal

Close the gap identified by operator 2026-05-26: Mode A daily digest
currently examines only **alerts that fired**. It does NOT examine
**logs that contain errors/warnings but never triggered an alert**.

Many real production problems live in this gap:

- Repeated deprecation warnings (1000×/hour) — breakage incoming, no
  alert
- TLS handshake retries — no alert until cert fully invalid
- Slow-query patterns in MSSQL — no alert until visible latency hit
- OAuth signature errors leading up to token expiry
- Cache miss storms before performance degrades

P3 extends Mode A's daily digest to also examine the **last 24h of
Loki logs** for outlier patterns and pass the top-N to the LLM as
an additional context block.

## 2. Non-goals

- **Not** real-time log streaming or anomaly detection — daily batch
  is sufficient (matches Mode A cadence)
- **Not** a separate mode — extends Mode A's existing LLM call rather
  than spinning up a new one (cheaper, simpler)
- **Not** a generic SIEM — solo-op homelab, not a security product
- **Not** automated remediation — just surfaces patterns; operator
  decides

## 3. Architecture

```
Existing Mode A digest pipeline
─────────────────────────────────
1. Prom history (24h ALERTS metric)
2. Aggregate alerts → AlertGroups
3. Enrich currently-firing with AM annotations
4. For chronic/flapping/active: fetch kubectl + Loki excerpts
5. Look up open issue dedup_keys
6. ── ONE LLM call ─────────────► Report (0..N Findings)
7. Dispatch each Finding

P3 inserts a new step 5.5 before the LLM call:
5.5 Log-pattern mining
    a. For each namespace in cluster:
       Query Loki: count of error/warn lines per 1h bucket, 7d window
    b. Compute baseline (rolling mean) per namespace+pod
    c. Find outliers vs baseline (24h actual vs 7d expected)
    d. Aggregate into LogPattern objects (top-N per cluster)
    e. Pass alongside AlertGroups in the LLM prompt
```

## 4. Data flow + new schemas

### 4.1 `LogPattern` (new in `schema.py`)

```python
class LogPattern(BaseModel):
    """One notable log pattern surfaced from 24h of Loki history.

    Produced by `digest_aggregator.aggregate_log_patterns()`; consumed
    by the digest prompt. NOT a Finding — the LLM decides whether a
    given LogPattern is worth elevating to a Finding.
    """
    namespace: str
    pod_prefix: str | None       # e.g. "mssql-giks-prd" (deployment-level)
    level: Literal["error", "warn"]
    count_24h: int
    baseline_mean_24h: float     # 7d rolling mean of 24h counts
    ratio_vs_baseline: float     # count_24h / max(baseline_mean_24h, 1)
    sample_lines: list[str]      # up to 3 representative log lines, redacted
    first_seen_at: dt.datetime
    last_seen_at: dt.datetime
```

### 4.2 Aggregator function

```python
def aggregate_log_patterns(
    cluster: str,
    *,
    window_hours: int = 24,
    baseline_days: int = 7,
    ratio_threshold: float = 3.0,
    max_patterns: int = 10,
) -> list[LogPattern]:
    """Query Loki for error/warn line counts per namespace, compute
    baseline, return top-N outliers."""
```

### 4.3 Prompt extension

`prompts/digest.md` gets a new section AFTER the alert-groups block:

```markdown
### Notable log patterns (24h vs 7d baseline)

These are log volume outliers — namespaces / pods generating
significantly more error/warn log lines in the last 24h vs their
typical baseline. NOT alerts, just patterns. Use your judgment:
some indicate real degradation worth a Finding; others are normal
chatty pods getting a bit chattier.

If a pattern correlates with an alert in the section above, surface
it in the Finding's evidence. If it's independent and significant,
consider a standalone Finding (severity:info or :low — these are
PRE-alert signals, not active outages).

```json
{{ log_patterns_json }}
```
```

## 5. Loki query design

Loki's metric-style query: `sum(count_over_time({namespace="X"} |~
"(?i)level=error|level=warn|panic|fatal"[1h]))` returns a time series
of error+warn counts per 1h bucket.

We do this per namespace × 168 hours (7 days) → one number per hour,
~5000 data points per cluster. Then:

1. Last 24 hours: sum → `count_24h`
2. Previous 6 days: mean of daily sums → `baseline_mean_24h`
3. Ratio: `count_24h / max(baseline_mean_24h, 1)`
4. Outliers: ratio ≥ 3.0 AND count_24h ≥ 50 (de-noise; namespaces
   with 5→15 errors are still small)
5. Top-N by ratio (capped at `max_patterns=10`)
6. For each outlier, fetch up to 3 sample log lines via separate Loki
   query, redact secrets (see § 7), return as `sample_lines`

## 6. Cost impact

| Component | Today | Post-P3 | Delta |
|---|---|---|---|
| Input tokens per digest | ~30K | ~35K | +5K |
| Output tokens per digest | ~2K | ~2-3K | +1K (more findings possible) |
| Loki tool calls per digest | ~3 (per chronic alert) | ~3 + ~30 (one per namespace) | +30 |
| Loki latency per query | ~50ms | ~50ms | same |
| Total digest latency | ~5-10s | ~10-20s | +5-10s |
| Per-digest cost | ~$0.20 | ~$0.25-0.30 | +$0.05-0.10 |
| Daily total (2 clusters) | $0.40-0.60 | $0.50-0.70 | negligible |

Conclusion: P3 increases per-day Mode A cost by ~$3-6/month. Worth it.

## 7. Privacy / secret-leak risk

**Concern**: log lines can contain secrets (passwords, tokens, bearer
auth, signing keys). The digest's LLM output goes to a public-ish GH
repo (the sandbox today, possibly kube-infra in P6+). Sample lines
must be redacted before passing to the LLM.

**Redaction regex** (in the aggregator, before LLM):

```python
_SECRET_PATTERNS = [
    r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|"
    r"bearer|authorization)\s*[:=]\s*\S+",
    r"sk-[a-zA-Z0-9-]{30,}",                       # API key shapes
    r"eyJ[a-zA-Z0-9_-]{20,}\.[a-zA-Z0-9_-]+\.\S+", # JWTs
    r"[a-f0-9]{40,}",                              # long hex (hashes, sigs)
]
def _redact(line: str) -> str:
    for p in _SECRET_PATTERNS:
        line = re.sub(p, "[REDACTED]", line)
    return line[:200]  # also cap length
```

False positives acceptable (over-redact > under-redact).

## 8. Test plan

- `test_digest_aggregator_log_patterns`: stubbed Loki response →
  expected LogPattern list with correct ratios/sorting
- `test_aggregator_redacts_secrets`: lines containing
  `password=foo` / `Bearer xyz` / JWTs → redacted in `sample_lines`
- `test_daily_digest_passes_log_patterns_to_llm`: integration test
  that the digest runner builds the log-patterns block and includes
  it in the prompt
- `test_aggregator_quiet_cluster`: cluster with zero outlier
  namespaces → empty list (no false positives forcing the LLM to
  invent findings)

## 9. Rollout

1. PR: schema + aggregator + tests (no dispatch impact yet)
2. PR: wire into `daily_digest.run_async` + extend `prompts/digest.md`
3. Merge + apply
4. Wait for next scheduled fire OR manual smoke test
5. Observe quality of LLM findings: do log patterns surface real
   issues or noise?
6. Tune `ratio_threshold` / `max_patterns` / regex over 1-2 weeks

## 10. Open design questions (operator input needed)

I have reasonable defaults for each, but these benefit from operator
confirmation:

1. **Baseline window** — 7 days reasonable? Shorter (3 days) reacts
   faster to recent changes but easier to be fooled by a busy week.
   Longer (14 days) smoother but slow to forget transient anomalies.

2. **Ratio threshold** — start at 3.0× baseline? Lower (2.0×) catches
   more, surfaces noise. Higher (5.0×) misses subtle creep.

3. **Minimum count floor** — currently proposed `count_24h ≥ 50`.
   Should it be higher (e.g. 200 for a noisy namespace) or
   per-namespace adaptive?

4. **Always-interesting patterns** — should the aggregator have a
   hard-coded list of patterns that ALWAYS get surfaced regardless
   of ratio? e.g. `panic`, `OOMKilled`, `crashloopbackoff`,
   `certificate expired`, `connection refused`. Reduces LLM judgment
   variability for known-critical signals.

5. **Severity default** — when LLM elevates a log-pattern to a
   Finding, what severity default? `info` (catches operator
   attention but doesn't page) or `low` (slightly more visible)?

6. **Granularity** — start with namespace-level only, or include
   pod-prefix breakdown immediately? Pod-prefix adds cardinality
   (~3-5× more patterns to consider) but better signal.

7. **Should P3 also include drift detection** (the original Mode B
   territory — manual `kubectl edit` not reflected in Flux)? Or
   keep that for P5 as planned?

8. **Cost ceiling** — should `DAILY_DIGEST_BUDGET_USD` (currently
   $0.50) be raised to $0.75 to accommodate the larger prompt? Or
   keep tight and let the budget gate force re-tuning if we
   over-extend?

Operator: pick / override any of the above. Defaults shown in §§
4-5 are what I'd use if you have no preference.
