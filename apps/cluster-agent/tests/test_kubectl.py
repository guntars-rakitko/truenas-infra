"""kubectl tool — read-only, allowlist-enforced, audit-logged.

Per spec § 4.3 + 6.2: scoped allowlist + double-block on secrets/exec
even though RBAC already forbids them. Misconfigured tokens shouldn't
be able to read secrets through this tool.
"""
import json
import pytest

from cluster_agent.tools.kubectl import kubectl_get, ToolError


def test_get_pods_passes_through(monkeypatch):
    """A valid kubectl get pods invocation builds the right argv."""
    called = {}

    def fake_run(cmd, **kw):
        called["cmd"] = cmd

        class R:
            returncode = 0
            stdout = '{"items":[]}'
            stderr = ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    result = kubectl_get("dev", "pods", namespace="kube-system")
    assert called["cmd"][0:3] == ["kubectl", "--context", "dev"]
    assert "pods" in called["cmd"]
    assert "-n" in called["cmd"]
    assert "kube-system" in called["cmd"]
    assert result == {"items": []}


def test_get_secrets_blocked():
    """Listing secrets is hard-blocked at the tool layer."""
    with pytest.raises(ToolError, match="secrets"):
        kubectl_get("dev", "secrets", namespace="default")


def test_exec_blocked():
    """pods/exec is never allowed."""
    with pytest.raises(ToolError, match="exec"):
        kubectl_get("dev", "pods/exec", namespace="default")
