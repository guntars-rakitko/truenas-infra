"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def env_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests never accidentally read real TRUENAS_* env vars."""
    for var in ("TRUENAS_HOST", "TRUENAS_API_KEY", "TRUENAS_VERIFY_SSL"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def with_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide a minimal set of env vars for tests that need a RuntimeConfig."""
    monkeypatch.setenv("TRUENAS_HOST", "10.10.5.10")
    monkeypatch.setenv("TRUENAS_API_KEY", "test-key-not-real")
    monkeypatch.setenv("TRUENAS_VERIFY_SSL", "false")


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
