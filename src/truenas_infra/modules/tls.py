"""Phase: tls — issue a Let's Encrypt wildcard cert via ACME DNS-01 + bind it.

Composition:
  1. `ensure_acme_authenticator` — register CloudFlare API token as a TrueNAS
     DNS authenticator (idempotent by name, updates in place if token drifts).
  2. `ensure_csr_wildcard` — create a CSR for `*.w1.lv` (+ SANs) so step 3
     has a valid CSR id to reference.
  3. `ensure_acme_cert` — call certificate.create with
     create_type=CERTIFICATE_CREATE_ACME referencing the CSR + authenticator.
     TrueNAS blocks ~30-90s while LE DNS-01 validates and hands back a
     real fullchain.
  4. `ensure_ui_certificate` — `system.general.update({ui_certificate: ...})`
     so the TrueNAS web UI stops serving the self-signed default cert.

Renewal is automatic (TrueNAS daemon). See docs/tls-runbook.md for the
forced-renewal drill that exercises rotation without waiting 60 days.

Does NOT touch app compose files — that's phase `apps` after cert lands.
Does NOT write the cert to disk for container consumption — that's phase
`apps` via apps/tls/tls-export.sh (cronjob-driven).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from truenas_infra.util import Diff


# ─── Config ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TlsConfig:
    domain: str
    authenticator_name: str
    cert_name: str
    sans: tuple[str, ...]
    acme_directory_uri: str
    renew_days: int


def load_tls_config(path: Path) -> TlsConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return TlsConfig(
        domain=str(raw["domain"]),
        authenticator_name=str(raw["authenticator_name"]),
        cert_name=str(raw["cert_name"]),
        sans=tuple(raw.get("sans") or []),
        acme_directory_uri=str(raw["acme_directory_uri"]),
        renew_days=int(raw.get("renew_days", 30)),
    )


DEFAULT_CONFIG_PATH = Path("config/tls.yaml")


# ─── ensure_acme_authenticator ───────────────────────────────────────────────


def ensure_acme_authenticator(
    cli: Any, *, name: str, api_token: str, apply: bool,
) -> Diff:
    """Register (or update) a CloudFlare DNS authenticator in TrueNAS.

    Idempotency key: `name`. Drift detection: if the stored api_token
    differs from the desired token, update in place (doesn't invalidate
    the cert reference).
    """
    existing = cli.call("acme.dns.authenticator.query", [["name", "=", name]])
    desired_attrs = {"authenticator": "cloudflare", "api_token": api_token}

    if existing:
        current = existing[0]
        current_attrs = current.get("attributes", {}) or {}
        # Compare authenticator type + token; CF-only relevant fields.
        if (current_attrs.get("authenticator") == "cloudflare"
                and current_attrs.get("api_token") == api_token):
            return Diff.noop(current)
        if apply:
            updated = cli.call(
                "acme.dns.authenticator.update",
                current["id"],
                {"attributes": desired_attrs},
            )
            return Diff.update(before=current, after=updated)
        return Diff.update(before=current,
                           after={**current, "attributes": desired_attrs})

    payload = {"name": name, "attributes": desired_attrs}
    if apply:
        created = cli.call("acme.dns.authenticator.create", payload)
        return Diff.create(created)
    return Diff.create(payload)


# ─── ensure_csr_wildcard ─────────────────────────────────────────────────────


def ensure_csr_wildcard(
    cli: Any, *, name: str, common_name: str, sans: tuple[str, ...], apply: bool,
) -> Diff:
    """Create a Certificate Signing Request for the wildcard + SANs.

    CSR is a prerequisite for the ACME cert — TrueNAS's ACME flow wants
    to reference an existing CSR (certificate.create with
    CERTIFICATE_CREATE_CSR is a separate call from
    CERTIFICATE_CREATE_ACME).

    Idempotency key: `name` in certificate.query.
    """
    existing = cli.call("certificate.query", [["name", "=", name]])
    if existing:
        # Exact SAN/CN drift detection is finicky (TrueNAS stores normalized
        # values). For now: presence of the named CSR is sufficient — if
        # the CSR needs different SANs, delete it out of band and rerun.
        return Diff.noop(existing[0])

    payload = {
        "name": name,
        "create_type": "CERTIFICATE_CREATE_CSR",
        "common": common_name,
        "san": list(sans),
        "key_type": "RSA",
        "key_length": 2048,
        "digest_algorithm": "SHA256",
        # LE is happy with minimal org/CN fields; provide defaults to avoid
        # TrueNAS validation errors on missing org data.
        "country": "LV",
        "state": "Latvia",
        "city": "Riga",
        "organization": "Homelab",
        "email": "guntars@rakitko.lv",
    }
    if apply:
        # certificate.create is a JOB. Without `job=True` the client returns
        # the job_id integer, not the finished record — downstream code that
        # expects a dict with `id` then gets None. Pass `job=True` so the
        # client waits for completion and hands back the cert dict.
        created = cli.call("certificate.create", payload, job=True)
        return Diff.create(created)
    return Diff.create(payload)


# ─── ensure_acme_cert ────────────────────────────────────────────────────────


def ensure_acme_cert(
    cli: Any,
    *,
    name: str,
    csr_id: int,
    authenticator_id: int,
    sans: tuple[str, ...],
    directory_uri: str,
    renew_days: int,
    apply: bool,
) -> Diff:
    """Issue or update a Let's Encrypt ACME-managed cert.

    Idempotency key: `name`. If a cert with this name exists AND is
    ACME-managed AND its `renew_days` + `acme_uri` match, noop. If only
    renew_days drifted, update in place (cheap, no re-issuance). Anything
    else (different directory URI, missing ACME metadata) → hands-off;
    the operator can `certificate.delete` and rerun.

    On fresh create: this call BLOCKS ~30-90s while TrueNAS drives the
    DNS-01 challenge against CloudFlare and gets the signed chain from LE.
    """
    existing = cli.call("certificate.query", [["name", "=", name]])
    if existing:
        current = existing[0]
        is_acme = bool(current.get("acme"))
        if is_acme:
            same_uri = current.get("acme_uri") == directory_uri
            same_renew = current.get("renew_days") == renew_days
            if same_uri and same_renew:
                return Diff.noop(current)
            if same_uri and not same_renew:
                if apply:
                    updated = cli.call(
                        "certificate.update",
                        current["id"],
                        {"renew_days": renew_days},
                    )
                    return Diff.update(before=current, after=updated)
                return Diff.update(
                    before=current,
                    after={**current, "renew_days": renew_days},
                )
            # URI mismatch — deliberately not auto-reissuing. Operator intervenes.
            return Diff.noop(current)

    # Fresh issue.
    # TrueNAS's validator compares dns_mapping keys against the CSR's `san`
    # array verbatim — and CSR SANs are stored as `DNS:<name>` with the
    # type prefix. Strip any existing prefix and re-add it to normalize.
    def _dns_key(s: str) -> str:
        return s if s.startswith("DNS:") else f"DNS:{s}"

    payload = {
        "name": name,
        "create_type": "CERTIFICATE_CREATE_ACME",
        "csr_id": csr_id,
        "tos": True,
        "acme_directory_uri": directory_uri,
        "renew_days": renew_days,
        # Each SAN needs an authenticator mapping — all use the same CloudFlare.
        "dns_mapping": {_dns_key(san): authenticator_id for san in sans},
    }
    if apply:
        # JOB — see ensure_csr_wildcard for the job=True rationale. This one
        # blocks ~30-90s while LE validates the DNS-01 TXT record.
        created = cli.call("certificate.create", payload, job=True)
        return Diff.create(created)
    return Diff.create(payload)


# ─── ensure_ui_certificate ───────────────────────────────────────────────────


# ─── ensure_ui_https_redirect ────────────────────────────────────────────────


def ensure_ui_https_redirect(cli: Any, *, enable: bool, apply: bool) -> Diff:
    """Toggle the TrueNAS UI's HTTP→HTTPS redirect.

    When enabled, `http://nas.w1.lv/` 301s to `https://nas.w1.lv/` instead
    of serving plain HTTP. Good hygiene once we have a real cert — prevents
    accidental plaintext credential submission.

    Same restart-the-UI-server dance as `ensure_ui_certificate` — without
    ui_restart, nginx doesn't pick up the new redirect rule.
    """
    current = cli.call("system.general.config")
    current_value = bool(current.get("ui_httpsredirect"))

    if current_value == enable:
        return Diff.noop({"ui_httpsredirect": current_value})

    if apply:
        updated = cli.call("system.general.update", {"ui_httpsredirect": enable})
        cli.call("system.general.ui_restart", 3)
        return Diff.update(before={"ui_httpsredirect": current_value},
                           after=updated)
    return Diff.update(before={"ui_httpsredirect": current_value},
                       after={"ui_httpsredirect": enable})


def ensure_ui_certificate(cli: Any, *, cert_id: int, apply: bool) -> Diff:
    """Bind the TrueNAS web UI to the given cert id + reload the UI server.

    `system.general.update` stores the association but nginx keeps serving
    the old cert until `system.general.ui_restart` signals a reload. The
    reload is quick (~1s) and doesn't drop API-WebSocket sessions, so it's
    safe to call unconditionally after a successful update.
    """
    current = cli.call("system.general.config")
    current_ui = current.get("ui_certificate") or {}
    current_id = current_ui.get("id") if isinstance(current_ui, dict) else current_ui

    if current_id == cert_id:
        return Diff.noop({"ui_certificate_id": current_id})

    if apply:
        updated = cli.call("system.general.update", {"ui_certificate": cert_id})
        # Kick the UI server so nginx re-reads the newly-bound cert.
        # Takes a `delay` in seconds; use 3 so the JSON-RPC response returns
        # cleanly before the restart kicks in.
        cli.call("system.general.ui_restart", 3)
        return Diff.update(before={"ui_certificate_id": current_id},
                           after=updated)
    return Diff.update(before={"ui_certificate_id": current_id},
                       after={"ui_certificate_id": cert_id})


# ─── Phase entry point ───────────────────────────────────────────────────────


def run(
    cli: Any,
    ctx: Any,
    only: str | None = None,
    *,
    config_path: Path | None = None,
) -> int:
    """Phase 3 TLS: issue wildcard cert + bind UI."""
    log = ctx.log.bind(phase="tls")

    token = getattr(ctx.config, "cloudflare_api_token", "")
    if not token:
        log.error(
            "cloudflare_token_missing",
            msg="CLOUDFLARE_API_TOKEN is empty. Add it to .env.sops — see "
                "bootstrap/01-bootstrap-notes.md step 7.",
        )
        return 2

    cfg = load_tls_config(config_path or DEFAULT_CONFIG_PATH)

    # 1. Authenticator
    auth_diff = ensure_acme_authenticator(
        cli, name=cfg.authenticator_name, api_token=token, apply=ctx.apply,
    )
    log.info("acme_authenticator_ensured",
             action=auth_diff.action, changed=auth_diff.changed,
             name=cfg.authenticator_name)
    auth_id = (
        auth_diff.after.get("id") if isinstance(auth_diff.after, dict) else None
    )

    # 2. CSR — named separately so we never accidentally bind CSR-only
    # cert records as the UI cert.
    csr_name = f"{cfg.cert_name}-csr"
    csr_diff = ensure_csr_wildcard(
        cli, name=csr_name,
        common_name=cfg.sans[0] if cfg.sans else f"*.{cfg.domain}",
        sans=cfg.sans, apply=ctx.apply,
    )
    log.info("csr_ensured",
             action=csr_diff.action, changed=csr_diff.changed, name=csr_name)
    csr_id = (
        csr_diff.after.get("id") if isinstance(csr_diff.after, dict) else None
    )

    # 3. ACME cert
    cert_diff = ensure_acme_cert(
        cli,
        name=cfg.cert_name,
        csr_id=csr_id or 0,
        authenticator_id=auth_id or 0,
        sans=cfg.sans,
        directory_uri=cfg.acme_directory_uri,
        renew_days=cfg.renew_days,
        apply=ctx.apply,
    )
    log.info("acme_cert_ensured",
             action=cert_diff.action, changed=cert_diff.changed,
             name=cfg.cert_name)
    cert_id = (
        cert_diff.after.get("id") if isinstance(cert_diff.after, dict) else None
    )

    # 4. Bind UI
    if cert_id:
        ui_diff = ensure_ui_certificate(cli, cert_id=cert_id, apply=ctx.apply)
        log.info("ui_certificate_ensured",
                 action=ui_diff.action, changed=ui_diff.changed)
    else:
        log.warning("ui_certificate_skipped",
                    reason="cert_id_unknown_after_ensure_acme_cert")

    # 5. HTTP→HTTPS redirect. Only safe to enable once the cert exists
    # and is bound — otherwise a plain-HTTP visitor gets redirected to a
    # broken HTTPS endpoint.
    redirect_diff = ensure_ui_https_redirect(cli, enable=True, apply=ctx.apply)
    log.info("ui_https_redirect_ensured",
             action=redirect_diff.action, changed=redirect_diff.changed)

    return 0
