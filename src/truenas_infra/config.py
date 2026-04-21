"""Runtime configuration loaded from environment variables.

`.env` is decrypted by `manage.sh` (via SOPS) and exported before Python is
invoked, so we only read from `os.environ` here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(
            f"Required env var '{name}' is not set. "
            "Check .env.sops and re-run via ./manage.sh."
        )
    return value or ""


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved runtime config for the CLI invocation."""

    truenas_host: str
    truenas_api_key: str
    truenas_verify_ssl: bool
    log_level: str
    apply: bool  # global default; CLI --apply / --dry-run overrides
    # CloudFlare API token used by phase tls for ACME DNS-01 validation.
    # Scope: Zone:w1.lv:DNS:Edit + Zone:w1.lv:Zone:Read. Optional here so
    # bringup phases 1-2 run without it; phase tls errors if empty.
    cloudflare_api_token: str = ""

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        return cls(
            truenas_host=_env("TRUENAS_HOST", required=True),
            truenas_api_key=_env("TRUENAS_API_KEY", required=True),
            truenas_verify_ssl=_bool_env("TRUENAS_VERIFY_SSL", default=False),
            log_level=_env("LOG_LEVEL", default="INFO"),
            apply=_bool_env("APPLY", default=False),
            cloudflare_api_token=_env("CLOUDFLARE_API_TOKEN", default=""),
        )
