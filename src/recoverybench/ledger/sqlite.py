"""SQLite-backed append-only effect ledger."""

import json
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from recoverybench.ledger.base import Ledger
from recoverybench.models import (
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
    LedgerEvent,
    RunSummary,
)


class SQLiteLedger(Ledger):
    """Persistent SQLite-backed append-only Effect Ledger."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._is_memory = self.db_path == ":memory:"
        self._lock = threading.RLock()
        self._mem_conn: sqlite3.Connection | None = None
        if self._is_memory:
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA foreign_keys = ON;")
        else:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager creating a connection and ensuring proper transaction lifecycle."""
        if self._is_memory:
            with self._lock:
                if self._mem_conn is None:
                    raise RuntimeError("SQLiteLedger in-memory database is closed.")
                try:
                    yield self._mem_conn
                except Exception:
                    self._mem_conn.rollback()
                    raise
        else:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON;")
            try:
                yield conn
            finally:
                conn.close()

    def close(self) -> None:
        """Close the ledger connection if in-memory."""
        with self._lock:
            if self._mem_conn is not None:
                self._mem_conn.close()
                self._mem_conn = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _init_db(self) -> None:
        """Initialize SQLite schema if not present and apply idempotent migrations."""
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    total_events INTEGER NOT NULL,
                    committed_effects INTEGER NOT NULL,
                    duplicate_effects INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    duration_ms REAL NOT NULL,
                    isolation_mode TEXT DEFAULT 'trusted'
                );

                CREATE TABLE IF NOT EXISTS effect_ledger (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    scenario_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    operation_id TEXT,
                    attempt_id TEXT,
                    effect_id TEXT,
                    idempotency_key_hash TEXT,
                    request_fingerprint TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    execution_status TEXT NOT NULL,
                    commit_status TEXT NOT NULL,
                    simulated_timestamp TEXT NOT NULL,
                    error_type TEXT,
                    evidence_reference TEXT,
                    details_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_ledger_run_id ON effect_ledger(run_id);
                CREATE INDEX IF NOT EXISTS idx_ledger_fingerprint ON effect_ledger(request_fingerprint);
                """
            )
            # Idempotent Schema Migration for legacy databases
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(effect_ledger);")
            columns = {row["name"] for row in cursor.fetchall()}
            if "operation_id" not in columns:
                conn.execute("ALTER TABLE effect_ledger ADD COLUMN operation_id TEXT;")
            if "attempt_id" not in columns:
                conn.execute("ALTER TABLE effect_ledger ADD COLUMN attempt_id TEXT;")
            if "effect_id" not in columns:
                conn.execute("ALTER TABLE effect_ledger ADD COLUMN effect_id TEXT;")

            # Indexes on new columns after ensuring columns exist
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_op_id ON effect_ledger(operation_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ledger_effect_id ON effect_ledger(effect_id);")

            # Runs table migration for isolation_mode
            cursor.execute("PRAGMA table_info(runs);")
            run_columns = {row["name"] for row in cursor.fetchall()}
            if "isolation_mode" not in run_columns:
                conn.execute("ALTER TABLE runs ADD COLUMN isolation_mode TEXT DEFAULT 'trusted';")

            conn.commit()

    def append(self, event: LedgerEvent) -> LedgerEvent:
        """Append an event to the SQLite database."""
        details_str = json.dumps(event.details, sort_keys=True)
        query = """
            INSERT INTO effect_ledger (
                run_id, scenario_id, tool_name, action_type,
                operation_id, attempt_id, effect_id,
                idempotency_key_hash, request_fingerprint, attempt_number,
                execution_status, commit_status, simulated_timestamp,
                error_type, evidence_reference, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            event.run_id,
            event.scenario_id,
            event.tool_name,
            event.action_type,
            event.operation_id,
            event.attempt_id,
            event.effect_id,
            event.idempotency_key_hash,
            event.request_fingerprint,
            event.attempt_number,
            event.execution_status.value,
            event.commit_status.value,
            event.simulated_timestamp,
            event.error_type,
            event.evidence_reference,
            details_str,
        )
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            assigned_id = cursor.lastrowid
            conn.commit()

        return event.model_copy(update={"event_id": assigned_id})

    def _row_to_event(self, row: sqlite3.Row) -> LedgerEvent:
        """Convert a SQLite row to a LedgerEvent model."""
        details: dict[str, Any] = {}
        if row["details_json"]:
            try:
                details = json.loads(row["details_json"])
            except Exception:
                details = {}

        row_keys = set(row.keys())
        op_id = row["operation_id"] if "operation_id" in row_keys else None
        att_id = row["attempt_id"] if "attempt_id" in row_keys else None
        eff_id = row["effect_id"] if "effect_id" in row_keys else None

        return LedgerEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            scenario_id=row["scenario_id"],
            tool_name=row["tool_name"],
            action_type=row["action_type"],
            operation_id=op_id,
            attempt_id=att_id,
            effect_id=eff_id,
            idempotency_key_hash=row["idempotency_key_hash"],
            request_fingerprint=row["request_fingerprint"],
            attempt_number=row["attempt_number"],
            execution_status=ExecutionStatus(row["execution_status"]),
            commit_status=CommitStatus(row["commit_status"]),
            simulated_timestamp=row["simulated_timestamp"],
            error_type=row["error_type"],
            evidence_reference=row["evidence_reference"],
            details=details,
        )

    def get_events(self, run_id: str) -> list[LedgerEvent]:
        """Retrieve all events for a run ordered by event_id."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM effect_ledger WHERE run_id = ? ORDER BY event_id ASC",
                (run_id,),
            )
            return [self._row_to_event(row) for row in cursor.fetchall()]

    def get_committed_effects(self, run_id: str) -> list[LedgerEvent]:
        """Retrieve all committed events for a run."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM effect_ledger WHERE run_id = ? AND commit_status = ? ORDER BY event_id ASC",
                (run_id, CommitStatus.COMMITTED.value),
            )
            return [self._row_to_event(row) for row in cursor.fetchall()]

    def record_run(self, summary: RunSummary) -> None:
        """Persist high-level metadata for an executed run."""
        query = """
            INSERT OR REPLACE INTO runs (
                run_id, scenario_id, agent_id, status, verdict,
                passed, total_events, committed_effects, duplicate_effects,
                created_at, duration_ms, isolation_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            summary.run_id,
            summary.scenario_id,
            summary.agent_id,
            "COMPLETED",
            summary.verdict.value,
            1 if summary.passed else 0,
            summary.total_events,
            summary.committed_effects,
            summary.duplicate_effects,
            summary.created_at,
            summary.duration_ms,
            summary.isolation_mode,
        )
        with self._connection() as conn:
            conn.execute(query, params)
            conn.commit()

    def get_run(self, run_id: str) -> RunSummary | None:
        """Retrieve run metadata by run_id."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return RunSummary(
                run_id=row["run_id"],
                scenario_id=row["scenario_id"],
                agent_id=row["agent_id"],
                passed=bool(row["passed"]),
                verdict=EvaluationVerdict(row["verdict"]),
                total_events=row["total_events"],
                committed_effects=row["committed_effects"],
                duplicate_effects=row["duplicate_effects"],
                created_at=row["created_at"],
                duration_ms=row["duration_ms"],
                isolation_mode=row["isolation_mode"] if "isolation_mode" in row.keys() else "trusted",
            )

    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        """List historical runs, ordered by most recent."""
        with self._connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            runs: list[RunSummary] = []
            for row in cursor.fetchall():
                runs.append(
                    RunSummary(
                        run_id=row["run_id"],
                        scenario_id=row["scenario_id"],
                        agent_id=row["agent_id"],
                        passed=bool(row["passed"]),
                        verdict=EvaluationVerdict(row["verdict"]),
                        total_events=row["total_events"],
                        committed_effects=row["committed_effects"],
                        duplicate_effects=row["duplicate_effects"],
                        created_at=row["created_at"],
                        duration_ms=row["duration_ms"],
                        isolation_mode=row["isolation_mode"] if "isolation_mode" in row.keys() else "trusted",
                    )
                )
            return runs

    def clear_run(self, run_id: str) -> None:
        """Clear historical events and metadata for a specific run_id."""
        with self._connection() as conn:
            conn.execute("DELETE FROM effect_ledger WHERE run_id = ?", (run_id,))
            conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            conn.commit()
