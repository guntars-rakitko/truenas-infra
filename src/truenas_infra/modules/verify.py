"""Phase: verify — run the verification matrix from the plan.

Read-only. Queries live state via the API, aggregates pass/fail, returns
rc=0 if everything checks out, rc=1 otherwise.
"""

from __future__ import annotations

import socket
import ssl
import subprocess
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


# ─── TLS / DNS / cert checks (phase 3 glue) ──────────────────────────────────


def _cert_days_left(cert: dict) -> int | None:
    """Extract days-until-expiration from a certificate.query record.

    TrueNAS 25.10 exposes this via:
    - `parsed.days_left` (dict form — our mock/test fixture shape)
    - `until` (date string: "Mon Jul 20 06:58:46 2026")
    - `lifetime` (integer days since issue; NOT what we want)
    """
    import datetime
    parsed = cert.get("parsed")
    if isinstance(parsed, dict) and parsed.get("days_left") is not None:
        return int(parsed["days_left"])
    until = cert.get("until")
    if until:
        try:
            dt = datetime.datetime.strptime(until, "%a %b %d %H:%M:%S %Y")
            return (dt - datetime.datetime.utcnow()).days
        except Exception:  # noqa: BLE001
            return None
    return None


def check_cert_expiry(
    cli: Any, *, cert_name: str, warn_days: int = 14, fail_days: int = 7,
) -> CheckResult:
    """Watch the wildcard cert's days left. TrueNAS auto-renews at
    renew_days=30, so <14 is a warning (renewal stalled), <7 is a fail
    (cert about to expire regardless of reason)."""
    certs = cli.call("certificate.query", [["name", "=", cert_name]])
    if not certs:
        return CheckResult(f"cert {cert_name}", False, "not found")
    days = _cert_days_left(certs[0])
    if days is None:
        return CheckResult(f"cert {cert_name}", False, "days_left unavailable")
    if days < fail_days:
        return CheckResult(
            f"cert {cert_name}", False,
            f"expires in {days} days (<{fail_days} fail threshold)")
    if days < warn_days:
        return CheckResult(
            f"cert {cert_name}", True,
            f"{days} days left (WARN: <{warn_days} — check auto-renewal)")
    return CheckResult(f"cert {cert_name}", True, f"{days} days left")


def _tls_handshake_cert(host: str, port: int, timeout: float = 5.0) -> dict:
    """Open a strict-TLS connection to host:port, return the peer cert.

    Extracted as a module-level function so tests can monkeypatch it
    without needing real network. Validates against the system trust
    store — failure raises (caller catches as CheckResult.passed=False).
    """
    ctx = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as s:
            cert = s.getpeercert()
            subject = dict(x[0] for x in cert.get("subject", []))
            issuer = dict(x[0] for x in cert.get("issuer", []))
            sans = [v for k, v in cert.get("subjectAltName", ())
                    if k.lower() == "dns"]
            return {
                "subject": ", ".join(f"{k}={v}" for k, v in subject.items()),
                "issuer": ", ".join(f"{k}={v}" for k, v in issuer.items()),
                "sans": sans,
            }


def _san_matches(host: str, sans: list[str]) -> bool:
    """RFC 6125 hostname vs SAN matching, wildcard-aware."""
    for s in sans:
        if s == host:
            return True
        if s.startswith("*.") and host.endswith(s[1:]) and "." in host:
            # "*.w1.lv" matches "foo.w1.lv" but NOT "bar.foo.w1.lv" (RFC 6125
            # allows only one left-most label substitution).
            if host.count(".") == s.count("."):
                return True
    return False


def check_tls_https(*, host: str, port: int = 443, timeout: float = 5.0) -> CheckResult:
    """TLS handshake + chain validation + SAN coverage of host."""
    try:
        info = _tls_handshake_cert(host, port, timeout)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(f"tls {host}:{port}", False, f"{exc}")
    if not _san_matches(host, info.get("sans") or []):
        return CheckResult(
            f"tls {host}:{port}", False,
            f"SAN mismatch: host={host!r} sans={info['sans']}")
    issuer = info.get("issuer", "")
    return CheckResult(f"tls {host}:{port}", True, f"issuer={issuer}")


def _dig_short(host: str, resolver: str) -> str | None:
    """Return the first A record (as a string) for host via resolver,
    or None if empty/NXDOMAIN. Wrapped as a module-level helper so tests
    can monkeypatch without touching real DNS."""
    try:
        out = subprocess.run(
            ["dig", "+short", "+time=2", "+tries=1",
             f"@{resolver}", host, "A"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:  # noqa: BLE001
        return None
    lines = [l.strip() for l in out.stdout.splitlines() if l.strip()]
    return lines[0] if lines else None


def check_dns_records(
    *, records: list[dict], internal_resolver: str = "10.10.0.1",
) -> CheckResult:
    """Every record in `records` must resolve to its expected address via
    the MikroTik internal resolver.

    Takes a plain list-of-dicts (matches config/dns.yaml's shape —
    `{"name": ..., "address": ...}`). Preserved entries (`preserve: true`)
    and non-A records are skipped — we only vet what this phase manages.
    """
    failures: list[str] = []
    checked = 0
    for r in records:
        if r.get("preserve"):
            continue
        name = r.get("name")
        expected = r.get("address")
        if not name or not expected:
            continue
        actual = _dig_short(name, internal_resolver)
        checked += 1
        if actual != expected:
            failures.append(f"{name}→{actual or 'NXDOMAIN'} (want {expected})")
    if failures:
        return CheckResult(
            "dns records", False,
            f"{len(failures)}/{checked} wrong: {'; '.join(failures[:3])}")
    return CheckResult("dns records", True, f"{checked}/{checked} resolve correctly")


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
        # Every Custom App we deploy in phase apps gets a state check.
        # Order matches config/apps.yaml.
        check_app(cli, app_name="netboot-xyz"),
        check_app(cli, app_name="minio-prd"),
        check_app(cli, app_name="minio-dev"),
        check_app(cli, app_name="meshcentral"),
        check_app(cli, app_name="traefik"),
        check_app(cli, app_name="wiki"),
        # TLS + DNS checks (phase 3 added these)
        check_cert_expiry(cli, cert_name="w1-wildcard"),
    ]

    # DNS records — load from config/dns.yaml
    try:
        import yaml as _yaml
        from pathlib import Path as _P
        dns_cfg = _yaml.safe_load(_P("config/dns.yaml").read_text(encoding="utf-8")) or {}
        records = dns_cfg.get("records") or []
        checks.append(check_dns_records(records=records))
    except Exception as exc:  # noqa: BLE001
        checks.append(CheckResult("dns records", False, f"config load failed: {exc}"))

    # HTTPS endpoint probes — the user-facing URLs our services expose.
    # Host headers go through Traefik (mc/pxe/minio-prd/minio-dev) or direct
    # (nas/traefik-nas/s3-prd/s3-dev). All must present a cert whose SAN
    # covers the host.
    for host, port in [
        ("nas.w1.lv", 443),
        ("mc.w1.lv", 443),
        ("pxe.w1.lv", 443),
        ("minio-prd.w1.lv", 443),
        ("minio-dev.w1.lv", 443),
        ("traefik-nas.w1.lv", 443),
        ("wiki.w1.lv", 443),
        ("s3-prd.w1.lv", 9000),
        ("s3-dev.w1.lv", 9000),
    ]:
        checks.append(check_tls_https(host=host, port=port))

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
