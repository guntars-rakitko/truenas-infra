"""kubectl tool — read-only, allowlist-enforced, audit-logged.

Per spec § 4.3 + 6.2: scoped allowlist + double-block on secrets/exec
even though RBAC already forbids them. Misconfigured tokens shouldn't
be able to read secrets through this tool.
"""
import json
import pytest

from cluster_agent.tools.kubectl import kubectl_get, ToolError


def test_get_pods_passes_through(monkeypatch):
    """A valid kubectl get pods invocation builds the right argv —
    including --kubeconfig pointing at the decoded KUBECONFIG_DEV file."""
    called = {}

    def fake_run(cmd, **kw):
        called["cmd"] = cmd

        class R:
            returncode = 0
            stdout = '{"items":[]}'
            stderr = ""
        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    # Plain YAML in env — _kubeconfig_path's base64 decode falls back to
    # using the value as-is on decode failure.
    monkeypatch.setenv("KUBECONFIG_DEV", "apiVersion: v1\nkind: Config\n")
    from cluster_agent.tools import kubectl as kmod
    kmod._KUBECONFIG_PATHS.clear()

    result = kubectl_get("dev", "pods", namespace="kube-system")
    assert called["cmd"][0] == "kubectl"
    assert "--kubeconfig" in called["cmd"]
    kc_idx = called["cmd"].index("--kubeconfig")
    assert called["cmd"][kc_idx + 1].endswith("-dev.kubeconfig")
    assert called["cmd"][kc_idx + 2:kc_idx + 4] == ["--context", "dev"]
    assert "pods" in called["cmd"]
    assert "-n" in called["cmd"]
    assert "kube-system" in called["cmd"]
    assert result == {"items": []}


def test_missing_kubeconfig_env_raises(monkeypatch):
    """If KUBECONFIG_{cluster} is not set, a clear ToolError surfaces
    instead of the cryptic 'context does not exist' kubectl would emit.
    Regression test for the 2026-05-26 silent-fail."""
    monkeypatch.delenv("KUBECONFIG_PRD", raising=False)
    from cluster_agent.tools import kubectl as kmod
    kmod._KUBECONFIG_PATHS.pop("prd", None)
    with pytest.raises(ToolError, match="KUBECONFIG_PRD not set"):
        kubectl_get("prd", "pods", namespace="default")


def test_get_secrets_blocked():
    """Listing secrets is hard-blocked at the tool layer."""
    with pytest.raises(ToolError, match="secrets"):
        kubectl_get("dev", "secrets", namespace="default")


def test_exec_blocked():
    """pods/exec is never allowed."""
    with pytest.raises(ToolError, match="exec"):
        kubectl_get("dev", "pods/exec", namespace="default")
