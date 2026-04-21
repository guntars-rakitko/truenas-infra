"""Smoke tests — verify the scaffold imports and wiring work without a real NAS."""

from __future__ import annotations

import importlib

import pytest


def test_package_importable() -> None:
    import truenas_infra

    assert truenas_infra.__version__


@pytest.mark.parametrize(
    "module",
    [
        "truenas_infra.cli",
        "truenas_infra.client",
        "truenas_infra.config",
        "truenas_infra.logging",
        "truenas_infra.util",
    ],
)
def test_core_modules_import(module: str) -> None:
    importlib.import_module(module)


@pytest.mark.parametrize(
    "module",
    [
        "truenas_infra.modules.users",
        "truenas_infra.modules.network",
        "truenas_infra.modules.tunables",
        "truenas_infra.modules.tls",
        "truenas_infra.modules.pool",
        "truenas_infra.modules.datasets",
        "truenas_infra.modules.storage_tasks",
        "truenas_infra.modules.shares",
        "truenas_infra.modules.nut",
        "truenas_infra.modules.apps",
        "truenas_infra.modules.verify",
    ],
)
def test_phase_modules_expose_run(module: str) -> None:
    m = importlib.import_module(module)
    assert hasattr(m, "run"), f"{module} is missing a `run` callable"
    assert callable(m.run)


def test_runtime_config_requires_env() -> None:
    from truenas_infra.config import RuntimeConfig

    with pytest.raises(RuntimeError, match="TRUENAS_HOST"):
        RuntimeConfig.from_env()


def test_runtime_config_with_env(with_test_env: None) -> None:
    from truenas_infra.config import RuntimeConfig

    cfg = RuntimeConfig.from_env()
    assert cfg.truenas_host == "10.10.5.10"
    assert cfg.truenas_api_key == "test-key-not-real"
    assert cfg.truenas_verify_ssl is False
    assert cfg.apply is False


def test_all_phases_listed_in_cli() -> None:
    """Every phase entry in cli.PHASES must correspond to an importable module."""
    from truenas_infra.cli import PHASES

    for name, module_path, _desc in PHASES:
        m = importlib.import_module(f"truenas_infra.modules.{module_path}")
        assert hasattr(m, "run"), f"phase '{name}' → module '{module_path}' missing run()"
