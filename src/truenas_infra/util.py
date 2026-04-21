"""Shared helpers used by every module."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Diff:
    """Result of comparing desired state to live state.

    Modules return this from `ensure_*` functions so the caller can decide
    whether to apply and what to log.
    """

    changed: bool
    before: Any
    after: Any
    action: str  # "create" | "update" | "noop" | "delete"

    @classmethod
    def noop(cls, state: Any) -> Diff:
        return cls(changed=False, before=state, after=state, action="noop")

    @classmethod
    def create(cls, after: Any) -> Diff:
        return cls(changed=True, before=None, after=after, action="create")

    @classmethod
    def update(cls, before: Any, after: Any) -> Diff:
        return cls(changed=True, before=before, after=after, action="update")


def redact(value: str, *, keep: int = 4) -> str:
    """Redact sensitive strings for logging — keeps first N chars."""
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return value[:keep] + "…" + "*" * 8
