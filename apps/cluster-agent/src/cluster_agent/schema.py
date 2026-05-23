"""Finding schema — the persistence boundary between LLM output and storage.

See spec § 4.4 for the full design. The schema is strict (Pydantic
validates at construction time) so a malformed LLM response throws at
the parse step rather than silently corrupting downstream:
  - mode/cluster/severity: enumerated literals only
  - confidence: clamped to [0.0, 1.0]
  - title: ≤ 200 chars (GH issue title limit)
  - id: 26-char ULID (no validation beyond length — ULIDs are
    base32-Crockford but enforcing the alphabet adds little value
    when the producer is our own code, not user input)
"""
from __future__ import annotations
import datetime as dt
from typing import Literal, Annotated
from pydantic import BaseModel, Field, field_validator


Mode = Literal["A", "B", "D", "E", "F", "G", "H", "I", "J"]
Cluster = Literal["dev", "prd", "nas", "global"]
Severity = Literal["high", "medium", "low", "info"]


class Evidence(BaseModel):
    """One piece of evidence the agent looked at while producing a finding.

    `excerpt` is optional — for logs we sometimes inline a snippet; for
    metrics/commits the `ref` (e.g. PromQL query, commit SHA) is enough.
    """
    type: Literal["alert", "log", "metric", "commit", "helmrelease", "pr", "issue", "doc"]
    ref: str
    excerpt: str | None = None


class AutoAction(BaseModel):
    """Action the agent already took as part of resolving this finding.

    Nullable on Finding — most findings just record the issue; only Mode F
    (auto-PR) + Mode J (auto-merge) actually take an action.
    """
    type: Literal["draft_pr", "comment", "label", "issue_create"]
    ref: str


class Finding(BaseModel):
    """Structured output of an agent mode run.

    One Finding may correspond to one GH issue (subject to dedup) or
    one wiki report entry. Stored in state.db.findings, dispatched to
    GH/wiki/email/Grafana per the spec § 3.3.
    """
    id: Annotated[str, Field(min_length=26, max_length=26)]   # ULID
    created_at: dt.datetime = Field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc)
    )
    mode: Mode
    cluster: Cluster
    severity: Severity
    title: str
    summary: str
    evidence: list[Evidence]
    root_cause_hypothesis: str | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]
    recommended_action: str | None = None
    runbook_ref: str | None = None
    auto_action: AutoAction | None = None
    dedup_key: str

    @field_validator("title")
    @classmethod
    def title_must_be_short(cls, v: str) -> str:
        if len(v) > 200:
            raise ValueError("title must be ≤ 200 chars (GH issue title limit)")
        return v
