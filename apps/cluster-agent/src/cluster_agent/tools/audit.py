"""Audit-log decorator — wraps every MCP tool call.

Emits one JSON line per call to stdout. The NAS log shipper routes it
to Loki, where it's indexed by `tool`, `agent_run_id`, `status`. Per
spec § 4.3: "Every action queryable in Loki forever."

Usage:

    @audit(tool="kubectl_get", redact=["password", "token"])
    def kubectl_get(context: str, resource: str, ...):
        ...

The decorator captures:
  - tool name
  - bound function params (with redaction list applied)
  - status: 'ok' / 'error'
  - result_summary (one-line repr, truncated)
  - duration_ms
  - agent_run_id (from contextvar — see set_run_id())
  - error message + type (on exception, re-raises)
"""
from __future__ import annotations
import contextvars
import functools
import inspect
import json
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


# Set per-mode-run by the mode runner so all tool calls during a run
# share an ID. ContextVar (not threading.local) so async tasks inherit it.
_current_run_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_run_id", default=None
)


def set_run_id(run_id: str) -> None:
    """Called once at the start of each mode run."""
    _current_run_id.set(run_id)


def get_run_id() -> str:
    v = _current_run_id.get()
    if v is None:
        # P0: no mode runner yet. Tests + ad-hoc calls fall through here.
        return f"adhoc-{uuid.uuid4()}"
    return v


@dataclass
class AuditEvent:
    """One emit per tool call. JSON-serialized to stdout."""
    tool: str
    params: dict[str, Any]
    status: str = "ok"
    result_summary: str | None = None
    duration_ms: float = 0.0
    agent_run_id: str = ""
    error: str | None = None
    ts: float = field(default_factory=time.time)


def _summarize(result: Any) -> str:
    """One-line summary of the return value, truncated at 200 chars."""
    s = repr(result)
    return s if len(s) <= 200 else s[:197] + "..."


def audit(
    tool: str,
    *,
    redact: list[str] | None = None,
    redact_result: bool = False,
    summarize: Callable[[Any], str] = _summarize,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator: emit one JSON audit line per call.

    Args:
        tool: short name (e.g. "kubectl_get", "loki_query")
        redact: param names to replace with ***REDACTED*** in the audit log
                (e.g. ["token", "password", "api_key"])
        redact_result: when True, the return value is NOT summarized into the
                log — `result_summary` becomes ***REDACTED***. Use for tools
                whose RETURN value is itself a secret (e.g. an installation
                access token), so the audit still records that the call
                happened + its status/duration without leaking the credential
                to stdout → Loki.
        summarize: callable that turns the return value into a one-line
                   summary string (default: repr() + 200-char truncation)
    """
    redact_set = set(redact or [])

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(fn)

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            params = {
                k: ("***REDACTED***" if k in redact_set else v)
                for k, v in bound.arguments.items()
            }
            event = AuditEvent(
                tool=tool,
                params=params,
                agent_run_id=get_run_id(),
            )
            try:
                result = fn(*args, **kwargs)
                event.result_summary = "***REDACTED***" if redact_result else summarize(result)
                return result
            except Exception as exc:
                event.status = "error"
                event.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                event.duration_ms = (time.perf_counter() - start) * 1000
                # default=str handles datetimes / Paths / etc. defensively.
                print(json.dumps(asdict(event), default=str),
                      file=sys.stdout, flush=True)

        return wrapper

    return decorator
