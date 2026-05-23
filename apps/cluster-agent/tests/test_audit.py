"""Audit log wrapper — every MCP tool call must produce one JSON line.

Per spec § 4.3: "Every action queryable in Loki forever." The @audit
decorator wraps each MCP tool function; the NAS log shipper routes
stdout to Loki, where the JSON is indexed.
"""
import json
from cluster_agent.tools.audit import audit, AuditEvent


def test_audit_decorator_emits_one_event(capsys):
    @audit(tool="dummy_get")
    def dummy_get(arg: str) -> str:
        return "result"

    result = dummy_get("input")
    assert result == "result"
    out = capsys.readouterr().out.strip()
    event = json.loads(out)
    assert event["tool"] == "dummy_get"
    assert event["params"] == {"arg": "input"}
    assert event["status"] == "ok"
    assert "agent_run_id" in event


def test_audit_redacts_known_secret_fields(capsys):
    @audit(tool="dummy_with_secret", redact=["token"])
    def dummy_with_secret(token: str, public: str) -> str:
        return "ok"

    dummy_with_secret("super-secret", "visible")
    event = json.loads(capsys.readouterr().out.strip())
    assert event["params"]["token"] == "***REDACTED***"
    assert event["params"]["public"] == "visible"


def test_audit_captures_exception(capsys):
    @audit(tool="dummy_fail")
    def dummy_fail() -> None:
        raise ValueError("boom")

    try:
        dummy_fail()
    except ValueError:
        pass
    event = json.loads(capsys.readouterr().out.strip())
    assert event["status"] == "error"
    assert "ValueError" in event["error"]
    assert "boom" in event["error"]
