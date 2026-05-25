"""Context-gathering for Mode A.

For each active alert, pre-fetches the four context blocks the prompt
template expects:
  - loki_excerpt      — recent log lines from the affected namespace
  - kubectl_describe  — describe of the affected pod/resource
  - prom_values       — recent values for metrics referenced in the alert
  - flux_state        — recent Kustomization/HelmRelease state in the ns

Each fetch is best-effort: an exception (auth flake, missing label,
empty result) produces an empty string for that block rather than
failing the whole context-gather. The prompt template renders
'(empty)' for missing blocks so the LLM doesn't trip on missing keys.
"""
from __future__ import annotations
import datetime as dt
import logging
from typing import Any

from ..tools.loki import loki_query
from ..tools.kubectl import kubectl_describe
from ..tools.prometheus import prometheus_query


log = logging.getLogger(__name__)


def _fetch_loki_excerpt(cluster: str, alert: dict[str, Any], window_min: int) -> str:
    labels = alert.get("labels", {})
    namespace = labels.get("namespace") or labels.get("pod_namespace")
    if not namespace:
        return ""
    logql = f'{{namespace="{namespace}"}}'
    try:
        starts_at = dt.datetime.fromisoformat(alert.get("startsAt", "").replace("Z", "+00:00"))
    except Exception:
        starts_at = dt.datetime.now(dt.timezone.utc)
    try:
        resp = loki_query(
            cluster,
            logql,
            start=starts_at - dt.timedelta(minutes=window_min),
            end=starts_at + dt.timedelta(minutes=5),
            limit=80,
        )
    except Exception as e:
        log.warning("loki_query failed: %r", e)
        return ""
    # Flatten the streams; we don't care about per-stream attribution here.
    lines: list[str] = []
    for stream in resp.get("data", {}).get("result", []):
        for _ts, line in stream.get("values", []):
            lines.append(line)
            if len(lines) >= 80:
                break
        if len(lines) >= 80:
            break
    return "\n".join(lines)


def _fetch_kubectl_describe(cluster: str, alert: dict[str, Any]) -> str:
    labels = alert.get("labels", {})
    namespace = labels.get("namespace") or labels.get("pod_namespace")
    pod = labels.get("pod")
    if not (namespace and pod):
        return ""
    try:
        return kubectl_describe(cluster, "pods", pod, namespace=namespace)
    except Exception as e:
        log.warning("kubectl_describe failed: %r", e)
        return ""


def _fetch_prom_values(cluster: str, alert: dict[str, Any]) -> str:
    """If the alert annotation includes an `expr`, re-execute it for a
    current value. Falls back to empty string if nothing useful."""
    expr = alert.get("annotations", {}).get("expression")
    if not expr:
        return ""
    try:
        resp = prometheus_query(cluster, expr)
    except Exception as e:
        log.warning("prometheus_query failed: %r", e)
        return ""
    return str(resp.get("data", {}).get("result", []))[:1000]


def _fetch_flux_state(cluster: str, alert: dict[str, Any]) -> str:
    """Flux state for the affected namespace, if known. Falls back to
    empty string — most alerts don't surface flux issues directly so
    blank context here is fine."""
    # P1 keeps this empty; Mode A can pull flux state into the prompt
    # via a follow-up if the operator finds it valuable during the soak.
    return ""


def gather_context_for_alert(
    alert: dict[str, Any], *, cluster: str, window_min: int = 30
) -> dict[str, str]:
    """Pre-fetch all context the prompt template needs.

    Each fetch is best-effort. Returns a dict with all four keys
    populated (possibly to empty strings); the LLM tolerates blanks.
    """
    return {
        "loki_excerpt":     _fetch_loki_excerpt(cluster, alert, window_min),
        "kubectl_describe": _fetch_kubectl_describe(cluster, alert),
        "prom_values":      _fetch_prom_values(cluster, alert),
        "flux_state":       _fetch_flux_state(cluster, alert),
    }
