"""SQLite state database — single source of truth for findings + dedup.

Schema covers:
  - findings:      dedup-keyed work items (open/closed status, GH ref)
  - mode_runs:     per-run audit log (cost, tokens, error capture)
  - pr_triages:    Mode I dedup (don't comment twice on the same PR)
  - phase_history: rollout audit (P0 → P1 → ... operator-approved entries)

Per spec § 3.5 + 4.4.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    dedup_key       TEXT PRIMARY KEY,
    gh_issue_ref    TEXT,             -- "owner/repo#NN" or NULL
    state           TEXT NOT NULL,    -- 'open' / 'closed'
    created_at      TEXT NOT NULL,    -- ISO8601 UTC
    last_seen_at    TEXT NOT NULL,    -- ISO8601 UTC
    closed_at       TEXT,             -- ISO8601 UTC or NULL
    mode            TEXT NOT NULL,    -- 'A' / 'B' / etc.
    cluster         TEXT NOT NULL,    -- 'dev' / 'prd' / 'nas' / 'global'
    severity        TEXT NOT NULL,    -- 'high' / 'medium' / 'low' / 'info'
    payload_json    TEXT NOT NULL     -- full Finding schema as JSON
);

CREATE INDEX IF NOT EXISTS idx_findings_state ON findings(state);
CREATE INDEX IF NOT EXISTS idx_findings_cluster ON findings(cluster);

CREATE TABLE IF NOT EXISTS mode_runs (
    run_id            TEXT PRIMARY KEY,    -- ULID
    mode              TEXT NOT NULL,
    cluster           TEXT,                -- nullable for global modes
    started_at        TEXT NOT NULL,
    ended_at          TEXT,
    status            TEXT,                -- 'success' / 'error' / 'aborted_budget'
    cost_usd          REAL,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cache_read_tokens INTEGER,
    error_message     TEXT
);

CREATE TABLE IF NOT EXISTS pr_triages (
    pr_ref          TEXT PRIMARY KEY,    -- 'owner/repo#NN'
    triaged_at      TEXT NOT NULL,
    verdict         TEXT NOT NULL,       -- 'auto_merge' / 'skip' / 'comment_only'
    reason          TEXT,
    gh_comment_id   INTEGER
);

CREATE TABLE IF NOT EXISTS phase_history (
    phase           TEXT PRIMARY KEY,    -- 'P0' / 'P1' / ...
    entered_at      TEXT NOT NULL,
    exited_at       TEXT,
    operator_note   TEXT
);

-- Mode A tick-level dedup state (2026-05-26 cost-cut). Each cluster's
-- last evaluated alert-set hash lets us short-circuit ticks where the
-- active alert set is unchanged AND every alert already has an open
-- GH issue — i.e. the whole tick would just produce the same findings
-- the previous tick already produced, costing $0.035/run of LLM spend
-- per cluster for zero new information.
CREATE TABLE IF NOT EXISTS mode_a_tick_state (
    cluster                 TEXT PRIMARY KEY,
    last_alert_set_hash     TEXT NOT NULL,    -- sha256 of sorted (alertname, fingerprint) tuples
    last_evaluated_at       TEXT NOT NULL,    -- ISO8601 UTC
    all_have_open_issues    INTEGER NOT NULL  -- 0 / 1
);
"""


class StateDB:
    """Thin wrapper around sqlite3 with schema bootstrap + helpers.

    Single-threaded by design — APScheduler runs mode-jobs serially in
    a background thread; concurrent writes within a job are not expected.
    WAL mode is still enabled so readers don't block writers if/when we
    add read paths from FastAPI handlers later.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit transactions where needed
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self._conn.close()
