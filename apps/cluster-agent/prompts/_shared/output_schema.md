## Output schema (mandatory)

Respond with a **single JSON object** matching this exact shape. No
prose before or after — the response is parsed with `json.loads()`
and any text outside the JSON breaks the agent loop.

```json
{
  "severity": "high|medium|low|info",
  "title": "Short title (≤ 200 chars). Becomes the GH issue title.",
  "summary": "One paragraph human-readable explanation of what's happening and why it matters.",
  "evidence": [
    {
      "type": "alert|log|metric|commit|helmrelease|pr|issue|doc",
      "ref": "stable reference (e.g. 'Alertmanager/<name>@<timestamp>', 'loki:{ns=...}|<window>', 'kube-infra@<sha>')",
      "excerpt": "Optional short verbatim snippet for logs/metrics. Omit for refs alone."
    }
  ],
  "root_cause_hypothesis": "Best current guess at root cause, or null if you genuinely don't have one. Don't speculate — null is preferred over fiction.",
  "confidence": 0.75,
  "recommended_action": "Concrete next step the operator should take. Reference a runbook if applicable.",
  "runbook_ref": "Optional path like 'wiki/docs/runbooks/foo.md#section', or null.",
  "dedup_key": "Stable key for this scenario. Format: 'alert:<alertname>:<scope-id>:<cluster>'. Pick a scope-id that's stable across re-firings of the same underlying issue (pod name if pod-scoped, namespace if ns-scoped, 'global' otherwise)."
}
```

### Field rules

- **severity** — pick the lowest level that's still accurate. `info` for "I noticed this, but it's not urgent". `high` only for "this is breaking customer-impacting things now". Don't inflate for attention.
- **confidence** — 0.0–1.0, your honest self-rating. 0.5 means "could go either way". Below 0.4 → the operator probably shouldn't act on this; just leave it as observation.
- **dedup_key** — re-firings of the same alert MUST produce the same `dedup_key`. If you can't see a stable scope, fall back to `alert:<alertname>:global:<cluster>`.
