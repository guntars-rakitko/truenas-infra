"""Tests for modules/tls.py — phase 3 (ACME DNS-01 wildcard cert)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock


def _mk_cli(side_effects: list) -> MagicMock:
    cli = MagicMock()
    cli.call.side_effect = side_effects
    return cli


# ─── load_tls_config ─────────────────────────────────────────────────────────


def test_load_tls_config_parses_yaml(tmp_path: Path) -> None:
    from truenas_infra.modules.tls import load_tls_config

    p = tmp_path / "tls.yaml"
    p.write_text(textwrap.dedent("""
        domain: w1.lv
        authenticator_name: cloudflare-w1
        cert_name: w1-wildcard
        sans: ["*.w1.lv", "w1.lv"]
        acme_directory_uri: https://acme-v02.api.letsencrypt.org/directory
        renew_days: 30
    """).strip())

    cfg = load_tls_config(p)
    assert cfg.domain == "w1.lv"
    assert cfg.authenticator_name == "cloudflare-w1"
    assert cfg.cert_name == "w1-wildcard"
    assert cfg.sans == ("*.w1.lv", "w1.lv")
    assert cfg.acme_directory_uri.endswith("/directory")
    assert cfg.renew_days == 30


# ─── ensure_acme_authenticator ───────────────────────────────────────────────


def test_ensure_acme_authenticator_creates_when_missing() -> None:
    from truenas_infra.modules.tls import ensure_acme_authenticator

    cli = _mk_cli([
        [],  # acme.dns.authenticator.query → none
        {"id": 42, "name": "cloudflare-w1",
         "attributes": {"authenticator": "cloudflare", "api_token": "t-live"}},
    ])

    diff = ensure_acme_authenticator(
        cli, name="cloudflare-w1", api_token="t-live", apply=True,
    )

    assert diff.changed is True
    assert diff.action == "create"
    create = next(c for c in cli.call.call_args_list
                  if c.args[0] == "acme.dns.authenticator.create")
    payload = create.args[1]
    assert payload["name"] == "cloudflare-w1"
    assert payload["attributes"]["authenticator"] == "cloudflare"
    assert payload["attributes"]["api_token"] == "t-live"


def test_ensure_acme_authenticator_noop_when_exists_and_token_matches() -> None:
    from truenas_infra.modules.tls import ensure_acme_authenticator

    existing = {
        "id": 42, "name": "cloudflare-w1",
        "attributes": {"authenticator": "cloudflare", "api_token": "t-live"},
    }
    cli = _mk_cli([[existing]])

    diff = ensure_acme_authenticator(
        cli, name="cloudflare-w1", api_token="t-live", apply=True,
    )

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "acme.dns.authenticator.create" not in names
    assert "acme.dns.authenticator.update" not in names


def test_ensure_acme_authenticator_updates_when_token_changes() -> None:
    from truenas_infra.modules.tls import ensure_acme_authenticator

    existing = {
        "id": 42, "name": "cloudflare-w1",
        "attributes": {"authenticator": "cloudflare", "api_token": "t-old"},
    }
    cli = _mk_cli([
        [existing],
        {**existing, "attributes": {"authenticator": "cloudflare", "api_token": "t-new"}},
    ])

    diff = ensure_acme_authenticator(
        cli, name="cloudflare-w1", api_token="t-new", apply=True,
    )

    assert diff.changed is True
    assert diff.action == "update"
    update = next(c for c in cli.call.call_args_list
                  if c.args[0] == "acme.dns.authenticator.update")
    assert update.args[1] == 42  # id
    assert update.args[2]["attributes"]["api_token"] == "t-new"


# ─── ensure_csr_wildcard ─────────────────────────────────────────────────────


def test_ensure_csr_wildcard_creates_when_missing() -> None:
    from truenas_infra.modules.tls import ensure_csr_wildcard

    cli = _mk_cli([
        [],  # certificate.query → none
        {"id": 100, "name": "w1-wildcard-csr", "CSR": "-----BEGIN CSR-----..."},
    ])

    diff = ensure_csr_wildcard(
        cli, name="w1-wildcard-csr",
        common_name="*.w1.lv", sans=("*.w1.lv", "w1.lv"), apply=True,
    )

    assert diff.changed is True
    assert diff.action == "create"
    create = next(c for c in cli.call.call_args_list
                  if c.args[0] == "certificate.create")
    payload = create.args[1]
    assert payload["name"] == "w1-wildcard-csr"
    assert payload["create_type"] == "CERTIFICATE_CREATE_CSR"
    assert payload["common"] == "*.w1.lv"
    assert set(payload["san"]) == {"*.w1.lv", "w1.lv"}


def test_ensure_csr_wildcard_noop_when_exists() -> None:
    from truenas_infra.modules.tls import ensure_csr_wildcard

    existing = {
        "id": 100, "name": "w1-wildcard-csr",
        "CSR": "-----BEGIN CSR-----...",
        "common": "*.w1.lv", "san": ["*.w1.lv", "w1.lv"],
    }
    cli = _mk_cli([[existing]])

    diff = ensure_csr_wildcard(
        cli, name="w1-wildcard-csr",
        common_name="*.w1.lv", sans=("*.w1.lv", "w1.lv"), apply=True,
    )

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "certificate.create" not in names


# ─── ensure_acme_cert ────────────────────────────────────────────────────────


def test_ensure_acme_cert_creates_when_missing() -> None:
    from truenas_infra.modules.tls import ensure_acme_cert

    cli = _mk_cli([
        [],  # certificate.query → none matching this cert_name
        {"id": 200, "name": "w1-wildcard", "acme": {"directory": "https://..."}},
    ])

    diff = ensure_acme_cert(
        cli,
        name="w1-wildcard",
        csr_id=100,
        authenticator_id=42,
        sans=("*.w1.lv", "w1.lv"),
        directory_uri="https://acme-v02.api.letsencrypt.org/directory",
        renew_days=30,
        apply=True,
    )

    assert diff.changed is True
    assert diff.action == "create"
    create = next(c for c in cli.call.call_args_list
                  if c.args[0] == "certificate.create")
    payload = create.args[1]
    assert payload["name"] == "w1-wildcard"
    assert payload["create_type"] == "CERTIFICATE_CREATE_ACME"
    assert payload["csr_id"] == 100
    assert payload["tos"] is True
    assert payload["acme_directory_uri"].endswith("/directory")
    assert payload["renew_days"] == 30
    # Every SAN must map to our authenticator. TrueNAS expects the key in
    # `DNS:<name>` form so it can match against the CSR's san[] which also
    # uses that prefix.
    assert payload["dns_mapping"]["DNS:*.w1.lv"] == 42
    assert payload["dns_mapping"]["DNS:w1.lv"] == 42


def test_ensure_acme_cert_noop_when_active_cert_exists() -> None:
    from truenas_infra.modules.tls import ensure_acme_cert

    existing = {
        "id": 200, "name": "w1-wildcard",
        "acme": {"id": 999},  # presence of .acme means it's an ACME-managed cert
        "acme_uri": "https://acme-v02.api.letsencrypt.org/directory",
        "renew_days": 30,
    }
    cli = _mk_cli([[existing]])

    diff = ensure_acme_cert(
        cli, name="w1-wildcard",
        csr_id=100, authenticator_id=42,
        sans=("*.w1.lv", "w1.lv"),
        directory_uri="https://acme-v02.api.letsencrypt.org/directory",
        renew_days=30, apply=True,
    )

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "certificate.create" not in names


def test_ensure_acme_cert_updates_renew_days_when_drifted() -> None:
    """If the live cert exists with different renew_days, update in place
    rather than re-issuing."""
    from truenas_infra.modules.tls import ensure_acme_cert

    existing = {
        "id": 200, "name": "w1-wildcard",
        "acme": {"id": 999},
        "acme_uri": "https://acme-v02.api.letsencrypt.org/directory",
        "renew_days": 10,  # drifted
    }
    cli = _mk_cli([
        [existing],
        {**existing, "renew_days": 30},
    ])

    diff = ensure_acme_cert(
        cli, name="w1-wildcard",
        csr_id=100, authenticator_id=42,
        sans=("*.w1.lv", "w1.lv"),
        directory_uri="https://acme-v02.api.letsencrypt.org/directory",
        renew_days=30, apply=True,
    )

    assert diff.changed is True
    assert diff.action == "update"
    update = next(c for c in cli.call.call_args_list
                  if c.args[0] == "certificate.update")
    assert update.args[1] == 200  # id
    assert update.args[2]["renew_days"] == 30


# ─── ensure_ui_certificate ───────────────────────────────────────────────────


def test_ensure_ui_certificate_binds_when_different() -> None:
    from truenas_infra.modules.tls import ensure_ui_certificate

    cli = _mk_cli([
        {"ui_certificate": {"id": 1, "name": "truenas_default"}},  # current
        {"ui_certificate": {"id": 200}},                            # after update
        None,                                                        # ui_restart
    ])

    diff = ensure_ui_certificate(cli, cert_id=200, apply=True)

    assert diff.changed is True
    assert diff.action == "update"
    update = next(c for c in cli.call.call_args_list
                  if c.args[0] == "system.general.update")
    assert update.args[1]["ui_certificate"] == 200
    # MUST also kick the UI server; without this the browser keeps seeing
    # the old cert until the next reboot.
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "system.general.ui_restart" in names


def test_ensure_ui_certificate_noop_when_already_bound() -> None:
    from truenas_infra.modules.tls import ensure_ui_certificate

    cli = _mk_cli([
        {"ui_certificate": {"id": 200, "name": "w1-wildcard"}},
    ])

    diff = ensure_ui_certificate(cli, cert_id=200, apply=True)

    assert diff.changed is False
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "system.general.update" not in names


# ─── run() orchestration ─────────────────────────────────────────────────────


class _CfgStub:
    truenas_host = "10.10.5.10"
    truenas_api_key = "test-key"
    truenas_verify_ssl = False
    cloudflare_api_token = "t-test"


class _Ctx:
    def __init__(self, apply: bool = False) -> None:
        self.apply = apply
        self.config = _CfgStub()
        import structlog
        self.log = structlog.get_logger("test")


def test_run_orchestrates_all_four_steps_on_fresh_install(tmp_path: Path) -> None:
    """Clean NAS (no authenticator, no CSR, no ACME cert): run creates all four
    and binds the UI to the new cert."""
    from truenas_infra.modules.tls import run

    cfg_file = tmp_path / "tls.yaml"
    cfg_file.write_text(textwrap.dedent("""
        domain: w1.lv
        authenticator_name: cloudflare-w1
        cert_name: w1-wildcard
        sans: ["*.w1.lv", "w1.lv"]
        acme_directory_uri: https://acme-staging-v02.api.letsencrypt.org/directory
        renew_days: 30
    """).strip())

    cli = _mk_cli([
        # ensure_acme_authenticator: query → empty, create
        [],
        {"id": 42, "name": "cloudflare-w1",
         "attributes": {"authenticator": "cloudflare", "api_token": "t-test"}},
        # ensure_csr_wildcard: query → empty, create
        [],
        {"id": 100, "name": "w1-wildcard-csr", "CSR": "..."},
        # ensure_acme_cert: query → empty, create
        [],
        {"id": 200, "name": "w1-wildcard", "acme": {"id": 999}},
        # ensure_ui_certificate: system.general.config, update, ui_restart
        {"ui_certificate": {"id": 1, "name": "truenas_default"}},
        {"ui_certificate": {"id": 200}},
        None,  # system.general.ui_restart
    ])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_file)

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "acme.dns.authenticator.create" in names
    # certificate.create called twice (once for CSR, once for ACME cert)
    assert sum(1 for n in names if n == "certificate.create") == 2
    assert "system.general.update" in names


def test_run_is_fully_noop_when_everything_already_exists(tmp_path: Path) -> None:
    """Re-running after successful initial apply: all four ensure_* paths noop,
    zero writes."""
    from truenas_infra.modules.tls import run

    cfg_file = tmp_path / "tls.yaml"
    cfg_file.write_text(textwrap.dedent("""
        domain: w1.lv
        authenticator_name: cloudflare-w1
        cert_name: w1-wildcard
        sans: ["*.w1.lv", "w1.lv"]
        acme_directory_uri: https://acme-v02.api.letsencrypt.org/directory
        renew_days: 30
    """).strip())

    cli = _mk_cli([
        # authenticator exists with matching token
        [{"id": 42, "name": "cloudflare-w1",
          "attributes": {"authenticator": "cloudflare", "api_token": "t-test"}}],
        # CSR exists
        [{"id": 100, "name": "w1-wildcard-csr",
          "CSR": "...", "common": "*.w1.lv", "san": ["*.w1.lv", "w1.lv"]}],
        # ACME cert exists + current renew_days
        [{"id": 200, "name": "w1-wildcard", "acme": {"id": 999},
          "acme_uri": "https://acme-v02.api.letsencrypt.org/directory",
          "renew_days": 30}],
        # UI already bound to this cert
        {"ui_certificate": {"id": 200, "name": "w1-wildcard"}},
    ])

    rc = run(cli, _Ctx(apply=True), only=None, config_path=cfg_file)

    assert rc == 0
    names = [c.args[0] for c in cli.call.call_args_list]
    assert "acme.dns.authenticator.create" not in names
    assert "acme.dns.authenticator.update" not in names
    assert "certificate.create" not in names
    assert "certificate.update" not in names
    assert "system.general.update" not in names


def test_run_refuses_without_cloudflare_token(tmp_path: Path) -> None:
    """No CLOUDFLARE_API_TOKEN on ctx.config → clear failure, no partial apply."""
    from truenas_infra.modules.tls import run

    cfg_file = tmp_path / "tls.yaml"
    cfg_file.write_text(textwrap.dedent("""
        domain: w1.lv
        authenticator_name: cloudflare-w1
        cert_name: w1-wildcard
        sans: ["*.w1.lv"]
        acme_directory_uri: https://acme-v02.api.letsencrypt.org/directory
        renew_days: 30
    """).strip())

    class _CfgNoToken:
        truenas_host = "10.10.5.10"
        truenas_api_key = "k"
        truenas_verify_ssl = False
        cloudflare_api_token = ""  # empty

    class _CtxNoToken:
        apply = True
        config = _CfgNoToken()
        import structlog
        log = structlog.get_logger("test")

    cli = _mk_cli([])  # no API calls should happen
    rc = run(cli, _CtxNoToken(), only=None, config_path=cfg_file)

    assert rc != 0
    assert cli.call.call_count == 0
