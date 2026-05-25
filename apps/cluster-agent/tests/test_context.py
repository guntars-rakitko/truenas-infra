"""Context-gathering for Mode A — pre-fetches what the LLM will need."""
from cluster_agent.modes.context import gather_context_for_alert


def test_gather_context_returns_required_keys(monkeypatch):
    """gather_context_for_alert returns dict with the 4 context fields the
    prompt template expects."""
    from cluster_agent.modes import context as ctx

    monkeypatch.setattr(ctx, "_fetch_loki_excerpt", lambda *a, **k: "loki-stub-output")
    monkeypatch.setattr(ctx, "_fetch_kubectl_describe", lambda *a, **k: "describe-stub-output")
    monkeypatch.setattr(ctx, "_fetch_prom_values", lambda *a, **k: "prom-stub-output")
    monkeypatch.setattr(ctx, "_fetch_flux_state", lambda *a, **k: "flux-stub-output")

    alert = {
        "labels": {"alertname": "KubePodCrashLooping", "namespace": "pocket-id", "pod": "pocket-id-0"},
        "startsAt": "2026-05-25T17:00:00Z",
    }
    result = gather_context_for_alert(alert, cluster="dev")
    assert set(result.keys()) >= {"loki_excerpt", "kubectl_describe", "prom_values", "flux_state"}
    assert result["loki_excerpt"] == "loki-stub-output"
    assert result["kubectl_describe"] == "describe-stub-output"


def test_gather_context_handles_alert_with_no_namespace_label(monkeypatch):
    """Some alerts (cluster-wide) have no namespace label — context-gather
    shouldn't crash; loki query falls back to a cluster-wide window or
    empty result."""
    from cluster_agent.modes import context as ctx

    monkeypatch.setattr(ctx, "_fetch_loki_excerpt", lambda *a, **k: "")
    monkeypatch.setattr(ctx, "_fetch_kubectl_describe", lambda *a, **k: "")
    monkeypatch.setattr(ctx, "_fetch_prom_values", lambda *a, **k: "")
    monkeypatch.setattr(ctx, "_fetch_flux_state", lambda *a, **k: "")

    alert = {"labels": {"alertname": "ClusterWideThing"}}
    result = gather_context_for_alert(alert, cluster="dev")
    assert "loki_excerpt" in result
