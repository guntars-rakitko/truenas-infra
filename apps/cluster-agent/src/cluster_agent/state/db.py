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

-- mode_a_tick_state was used by the 5-min polling model (added
-- 2026-05-26, removed same day with the daily-digest pivot). Existing
-- deployments drop it via the migration below.
"""

# One-shot cleanup migrations applied at StateDB init. SQLite has no
# ALTER-table flexibility, so we just DROP what's gone. Cheap to run
# every startup; idempotent.
_MIGRATIONS = [
    "DROP TABLE IF EXISTS mode_a_tick_state",
]


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
        for stmt in _MIGRATIONS:
            self._conn.execute(stmt)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        self._conn.close()
