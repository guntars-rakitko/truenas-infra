"""Finding schema — the JSON contract between LLM output and persistence.

Per spec § 4.4: every mode run produces zero or more Findings, each
normalized through this schema before storage/dispatch. Catches malformed
LLM output at the persistence boundary so bad data never reaches state.db
or GH/wiki/email.
"""
import datetime as dt
import pytest
from pydantic import ValidationError

from cluster_agent.schema import Finding, Evidence


def test_minimal_finding_parses():
    f = Finding(
        id="01JKR8Q9M0000000000000ABCD",   # 26-char ULID
        mode="A",
        cluster="dev",
        severity="high",
        title="Test",
        summary="Test summary",
        dedup_key="alert:Foo:bar:dev",
        confidence=0.8,
        evidence=[],
    )
    assert f.severity == "high"
    assert f.created_at is not None


def test_evidence_with_excerpt():
    ev = Evidence(type="log", ref="loki:foo", excerpt="error: ...")
    assert ev.type == "log"


def test_invalid_severity_rejected():
    with pytest.raises(ValidationError):
        Finding(
            id="01JKR8Q9M0000000000000ABCD",
            mode="A",
            cluster="dev",
            severity="critical",   # not allowed — only high/medium/low/info
            title="x", summary="x", dedup_key="x",
            confidence=0.5, evidence=[],
        )


def test_confidence_clamped_0_1():
    with pytest.raises(ValidationError):
        Finding(
            id="01JKR8Q9M0000000000000ABCD",
            mode="A",
            cluster="dev",
            severity="low",
            title="x", summary="x", dedup_key="x",
            confidence=1.5,        # > 1 — outside [0, 1]
            evidence=[],
        )


def test_serialization_roundtrip():
    f = Finding(
        id="01JKR8Q9M0000000000000ABCD",
        mode="A", cluster="dev", severity="medium",
        title="x", summary="x", dedup_key="x",
        confidence=0.7, evidence=[Evidence(type="alert", ref="AM/X")],
    )
    json_str = f.model_dump_json()
    f2 = Finding.model_validate_json(json_str)
    assert f2 == f
