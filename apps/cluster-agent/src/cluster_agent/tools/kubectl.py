"""kubectl wrapper — read-only, allowlist-enforced.

Per spec § 4.3: scoped allowlist + audit-log emit. Even though RBAC
also blocks secrets at the cluster level (Tasks 1-3 created the
cluster-agent-readonly SA with no secrets grant), we double-block in
code so a misconfigured kubeconfig can't accidentally read them.

The agent never needs `kubectl apply` / `delete` / `patch` etc. —
those verbs aren't exposed here at all. If a future mode legitimately
needs them, it gets its own narrowly-scoped tool (e.g. Mode G's
test-restore Job creation goes through a separate `kubectl_apply_in_ns`
that's restricted to the cluster-agent-tests namespace).
"""
from __future__ import annotations
import json
import re
import subprocess
from typing import Any

from .audit import audit


class ToolError(RuntimeError):
    """Raised when the tool refuses to issue a kubectl command —
    e.g. resource not in allowlist, or hard-banned."""


# Read-only resources allowed. Composed from the cluster-agent-readonly
# ClusterRole's verbs/resources (kube-infra
# flux-cd/infrastructure/configs/base/cluster-agent-rbac.yaml).
ALLOWED_RESOURCES = re.compile(
    r"^(pods|pods/log|nodes|namespaces|events|services|configmaps|"
    r"persistentvolumes|persistentvolumeclaims|deployments|statefulsets|"
    r"daemonsets|replicasets|jobs|cronjobs|helmreleases|kustomizations|"
    r"gitrepositories|helmrepositories|ocirepositories|volumes\.longhorn\.io|"
    r"backups\.velero\.io|certificates|ingressroutes|servicemonitors|"
    r"podmonitors|prometheusrules)$"
)

# Hard-banned. The allowlist regex doesn't match these, but we also
# explicitly enumerate them for defense-in-depth — a typo or future
# regex broadening can't accidentally let these through.
BANNED_RESOURCES = {"secrets", "pods/exec", "pods/attach", "pods/portforward"}


@audit(tool="kubectl_get")
def kubectl_get(
    context: str,
    resource: str,
    *,
    name: str | None = None,
    namespace: str | None = None,
    label_selector: str | None = None,
    field_selector: str | None = None,
    output: str = "json",
) -> dict[str, Any]:
    """Read-only kubectl get against the named context.

    Args:
        context: kubeconfig context (e.g. 'dev', 'prd')
        resource: K8s resource type (must match the ALLOWED_RESOURCES regex)
        name: specific resource name (optional — without it, lists all)
        namespace: target namespace (or all-namespaces if None)
        label_selector: -l value
        field_selector: --field-selector value
        output: -o value (default 'json'; parsed as dict)

    Raises:
        ToolError: resource not allowed or kubectl command failed
    """
    if resource in BANNED_RESOURCES:
        raise ToolError(f"resource '{resource}' is hard-banned for the agent")
    if not ALLOWED_RESOURCES.match(resource):
        raise ToolError(f"resource '{resource}' not in agent allowlist")

    cmd = ["kubectl", "--context", context, "get", resource]
    if name:
        cmd.append(name)
    if namespace:
        cmd.extend(["-n", namespace])
    else:
        cmd.append("-A")
    if label_selector:
        cmd.extend(["-l", label_selector])
    if field_selector:
        cmd.extend(["--field-selector", field_selector])
    cmd.extend(["-o", output])

    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ToolError(f"kubectl get failed: {r.stderr.strip()}")
    if output == "json":
        return json.loads(r.stdout)
    return {"raw": r.stdout}


@audit(tool="kubectl_describe")
def kubectl_describe(
    context: str,
    resource: str,
    name: str,
    *,
    namespace: str | None = None,
) -> str:
    """Read-only kubectl describe. Same allowlist as get."""
    if resource in BANNED_RESOURCES:
        raise ToolError(f"resource '{resource}' is hard-banned")
    if not ALLOWED_RESOURCES.match(resource):
        raise ToolError(f"resource '{resource}' not in agent allowlist")
    cmd = ["kubectl", "--context", context, "describe", resource, name]
    if namespace:
        cmd.extend(["-n", namespace])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ToolError(f"kubectl describe failed: {r.stderr.strip()}")
    return r.stdout


@audit(tool="kubectl_logs")
def kubectl_logs(
    context: str,
    pod: str,
    namespace: str,
    *,
    container: str | None = None,
    tail: int = 200,
    since: str | None = None,
) -> str:
    """Read-only kubectl logs."""
    cmd = ["kubectl", "--context", context, "logs", pod, "-n", namespace, f"--tail={tail}"]
    if container:
        cmd.extend(["-c", container])
    if since:
        cmd.extend(["--since", since])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise ToolError(f"kubectl logs failed: {r.stderr.strip()}")
    return r.stdout
