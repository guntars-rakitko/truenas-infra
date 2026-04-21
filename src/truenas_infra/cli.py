"""CLI — phase dispatcher.

Usage:
    truenas-infra list
    truenas-infra phase <name> [--apply] [--only <subitem>]
    truenas-infra preflight

Every phase is a callable `run(ctx)` in `truenas_infra.modules.<phase>`.
The default is dry-run; pass `--apply` to actually change state.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

import click
import structlog

from truenas_infra import logging as log_setup
from truenas_infra.client import connected
from truenas_infra.config import RuntimeConfig

# Phase ordering matches the plan (docs/plans/zesty-drifting-castle.md).
# Each entry: (phase-name, module under truenas_infra.modules, short description).
PHASES: list[tuple[str, str, str]] = [
    ("users", "users", "Local users, SSH keys, email alerts"),
    ("network", "network", "VLAN sub-interfaces on NIC1 (10/15/20)"),
    ("tunables", "tunables", "Kernel boot args (NVMe/PCIe power mgmt)"),
    ("tls", "tls", "Internal CA + ACME DNS-01 certificate"),
    ("pool", "pool", "RAIDZ1 across 6x NVMe (one-shot, gated)"),
    ("datasets", "datasets", "Nested dataset tree, quotas, ACLs"),
    ("storage-tasks", "storage_tasks", "SMART, scrub, snapshot tasks"),
    ("shares", "shares", "NFS (prd/dev) + SMB (home) + service bindings"),
    ("nut", "nut", "Built-in UPS/NUT service (1x APC Smart-UPS)"),
    ("apps", "apps", "netboot-xyz + minio-prd + minio-dev"),
    ("verify", "verify", "Run the verification matrix"),
]


@dataclass
class Context:
    """Per-invocation context handed to every module."""

    config: RuntimeConfig
    apply: bool
    log: structlog.BoundLogger
    confirm_token: str = ""


def _run_phase(name: str, module_path: str, ctx: Context, *, only: str | None) -> int:
    try:
        module = importlib.import_module(f"truenas_infra.modules.{module_path}")
    except ModuleNotFoundError as exc:
        ctx.log.error("phase_not_implemented", phase=name, error=str(exc))
        return 2

    if not hasattr(module, "run"):
        ctx.log.error("phase_missing_run", phase=name, module=module_path)
        return 2

    ctx.log.info("phase_start", phase=name, apply=ctx.apply, only=only)
    try:
        with connected(
            ctx.config.truenas_host,
            ctx.config.truenas_api_key,
            verify_ssl=ctx.config.truenas_verify_ssl,
        ) as cli:
            rc = module.run(cli=cli, ctx=ctx, only=only)
    except NotImplementedError as exc:
        ctx.log.warning("phase_stub", phase=name, reason=str(exc))
        return 0
    except Exception:  # noqa: BLE001
        ctx.log.exception("phase_failed", phase=name)
        return 1

    ctx.log.info("phase_done", phase=name, rc=rc or 0)
    return int(rc or 0)


@click.group(help=__doc__)
@click.option("--log-level", default=None, help="Override LOG_LEVEL env var.")
@click.pass_context
def cli(ctx: click.Context, log_level: str | None) -> None:
    cfg = RuntimeConfig.from_env()
    effective_level = log_level or cfg.log_level
    log = log_setup.configure(level=effective_level, log_dir=Path("logs"))
    # Default apply comes from env (APPLY=); individual commands can override.
    ctx.obj = Context(config=cfg, apply=cfg.apply, log=log)


@cli.command("list", help="List all phases.")
@click.pass_obj
def list_phases(obj: Context) -> None:
    for name, module_path, desc in PHASES:
        click.echo(f"  {name:<16s}  {desc}  (modules.{module_path})")


@cli.command("preflight", help="Check reachability and basic auth before any phase runs.")
@click.pass_obj
def preflight(obj: Context) -> None:
    obj.log.info("preflight", host=obj.config.truenas_host)
    try:
        with connected(
            obj.config.truenas_host,
            obj.config.truenas_api_key,
            verify_ssl=obj.config.truenas_verify_ssl,
        ) as cli_:
            info = cli_.call("system.info")
            obj.log.info("truenas_info", version=info.get("version"), hostname=info.get("hostname"))
    except Exception:  # noqa: BLE001
        obj.log.exception("preflight_failed")
        sys.exit(1)


@cli.command("phase", help="Run a single phase.")
@click.argument("name")
@click.option("--only", default=None, help="Run only a sub-item of the phase.")
@click.option("--apply/--dry-run", default=None, help="Actually apply (default: dry-run).")
@click.option("--confirm", default="", help="Confirm-token for destructive phases (e.g. 'CREATE-TANK').")
@click.pass_obj
def phase(
    obj: Context,
    name: str,
    only: str | None,
    apply: bool | None,
    confirm: str,
) -> None:
    if apply is not None:
        obj.apply = apply
    if confirm:
        obj.confirm_token = confirm
    for phase_name, module_path, _desc in PHASES:
        if phase_name == name:
            rc = _run_phase(phase_name, module_path, obj, only=only)
            sys.exit(rc)
    click.echo(f"Unknown phase: {name}. Run `truenas-infra list` for the list.", err=True)
    sys.exit(2)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
