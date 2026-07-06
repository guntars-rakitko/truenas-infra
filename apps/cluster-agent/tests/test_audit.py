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


def test_audit_redacts_result_when_flagged(capsys):
    """redact_result=True: the caller still gets the real return value, but
    the audit line's result_summary is ***REDACTED*** so a secret RETURN
    value (e.g. a GH installation token) never reaches stdout → Loki."""
    @audit(tool="mint_token", redact_result=True)
    def mint_token() -> str:
        return "ghs_supersecrettoken"

    result = mint_token()
    assert result == "ghs_supersecrettoken"          # caller gets the real value
    event = json.loads(capsys.readouterr().out.strip())
    assert event["result_summary"] == "***REDACTED***"
    assert "ghs_supersecrettoken" not in json.dumps(event)   # not anywhere in the line
    assert event["status"] == "ok"


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
