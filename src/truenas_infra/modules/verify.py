"""Phase: verify — run the verification matrix from the plan.

Read-only. Queries live state via the API, aggregates pass/fail, returns
rc=0 if everything checks out, rc=1 otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    message: str = ""


# ─── Individual checks ───────────────────────────────────────────────────────


def check_pool(cli: Any, *, pool_name: str) -> CheckResult:
    pools = cli.call("pool.query", [["name", "=", pool_name]])
    if not pools:
        return CheckResult(f"pool {pool_name}", False, "pool not found")
    p = pools[0]
    ok = p.get("status") == "ONLINE" and p.get("healthy") in (True, None)
    return CheckResult(
        f"pool {pool_name}",
        ok,
        f"status={p.get('status')} healthy={p.get('healthy')}",
    )


def check_service(cli: Any, *, service_name: str) -> CheckResult:
    svc = cli.call("service.query", [["service", "=", service_name]])
    if not svc:
        return CheckResult(f"service {service_name}", False, "service not found")
    s = svc[0]
    ok = s.get("state") == "RUNNING" and s.get("enable") is True
    return CheckResult(
        f"service {service_name}",
        ok,
        f"state={s.get('state')} enable={s.get('enable')}",
    )


def check_app(cli: Any, *, app_name: str) -> CheckResult:
    apps = cli.call("app.query", [["name", "=", app_name]])
    if not apps:
        return CheckResult(f"app {app_name}", False, "app not installed")
    a = apps[0]
    ok = a.get("state") == "RUNNING"
    return CheckResult(f"app {app_name}", ok, f"state={a.get('state')}")


def check_datasets(cli: Any, *, expected: tuple[str, ...]) -> CheckResult:
    live = cli.call("pool.dataset.query")
    names = {d.get("name") for d in live}
    missing = [n for n in expected if n not in names]
    ok = not missing
    return CheckResult(
        "datasets",
        ok,
        f"all present ({len(expected)})" if ok else f"missing: {', '.join(missing)}",
    )


# ─── Phase entry point ───────────────────────────────────────────────────────


# The minimal set of datasets we expect after phases 1-9 have run.
_EXPECTED_DATASETS: tuple[str, ...] = (
    "tank/kube/prd",
    "tank/kube/dev",
    "tank/media",
    "tank/shared/general",
    "tank/system",
)


def run(cli: Any, ctx: Any, only: str | None = None) -> int:
    """Phase 10: verify. Returns 0 if all checks pass, 1 otherwise."""
    log = ctx.log.bind(phase="verify")

    checks: list[CheckResult] = [
        check_pool(cli, pool_name="tank"),
        check_datasets(cli, expected=_EXPECTED_DATASETS),
        check_service(cli, service_name="nfs"),
        check_service(cli, service_name="cifs"),
        check_service(cli, service_name="ups"),
        check_app(cli, app_name="netboot-xyz"),
    ]

    failed: list[CheckResult] = []
    for r in checks:
        if r.passed:
            log.info("check_passed", name=r.name, message=r.message)
        else:
            log.warning("check_failed", name=r.name, message=r.message)
            failed.append(r)

    total = len(checks)
    passed = total - len(failed)
    log.info("verify_summary", total=total, passed=passed, failed=len(failed))

    return 0 if not failed else 1
