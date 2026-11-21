"""Durable, append-only SQLite WAL storage for late outcome records."""

import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from recoverybench.errors import ConflictingEventIdentityError
from recoverybench.models import LateOutcomeRecord


class LateOutcomeLedger:
    """Append-only, durable SQLite WAL ledger for late-arriving execution events and reconciliation snapshots.

    Implements ADR-004 Section 5 (Sections 5.1-5.4):
    - SQLite Write-Ahead Logging (WAL) with synchronous = FULL.
    - Serialized writer transactions via BEGIN IMMEDIATE with busy_timeout = 5000ms.
    - Strict uniqueness on event_id and (writer_id, attempt_id, event_sequence).
    - Idempotent duplicate delivery recognition.
    - Strict quarantine and ConflictingEventIdentityError on conflicting payloads.
    - Crash-recovery replay from persistent WAL.
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._is_memory = self.db_path == ":memory:"

        if not self._is_memory:
            parent_dir = Path(self.db_path).parent
            parent_dir.mkdir(parents=True, exist_ok=True)

        self._conn = sqlite3.connect(
            self.db_path,
            timeout=5.0,
            check_same_thread=False,
            isolation_level=None,  # Manual transaction management
        )
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite WAL pragmas and relational schema."""
        with self._lock:
            cur = self._conn.cursor()
            if not self._is_memory:
                cur.execute("PRAGMA journal_mode = WAL;")
                cur.execute("PRAGMA synchronous = FULL;")
            cur.execute("PRAGMA foreign_keys = ON;")
            cur.execute("PRAGMA busy_timeout = 5000;")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS late_outcome_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    attempt_id TEXT NOT NULL,
                    writer_id TEXT NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_fingerprint TEXT NOT NULL,
                    emitted_timestamp TEXT NOT NULL,
                    arrival_timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    CONSTRAINT uq_writer_sequence UNIQUE (writer_id, attempt_id, event_sequence),
                    CONSTRAINT uq_event_id UNIQUE (event_id)
                );
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS quarantine_late_events (
                    quarantine_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT,
                    run_id TEXT,
                    reason TEXT,
                    record_json TEXT,
                    quarantined_at TEXT
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_late_run ON late_outcome_events (run_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_quarantine_run ON quarantine_late_events (run_id);")

    def _sync_fs(self) -> None:
        """Explicitly flush OS write caches to disk for local durability."""
        if not self._is_memory and os.path.exists(self.db_path):
            try:
                # Flush WAL and database file descriptors
                with open(self.db_path, "rb") as f:
                    os.fsync(f.fileno())
                wal_path = f"{self.db_path}-wal"
                if os.path.exists(wal_path):
                    with open(wal_path, "rb") as wf:
                        os.fsync(wf.fileno())
            except OSError:
                pass

    def append(self, record: LateOutcomeRecord) -> bool:
        """Ingest a late outcome record into the durable append-only ledger.

        Args:
            record: LateOutcomeRecord instance.

        Returns:
            bool: True if newly inserted, False if idempotent duplicate no-op.

        Raises:
            ConflictingEventIdentityError: If an incoming record has an existing event_id or
                (writer_id, attempt_id, event_sequence) with a conflicting payload fingerprint.
        """
        now_arrival = datetime.now(UTC).isoformat()
        if not record.arrival_timestamp:
            record.arrival_timestamp = now_arrival

        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE;")
            try:
                # 1. Check for existing event_id
                cur.execute(
                    "SELECT payload_fingerprint, payload_json FROM late_outcome_events WHERE event_id = ?;",
                    (record.event_id,),
                )
                row_id = cur.fetchone()
                if row_id is not None:
                    existing_fp = row_id[0]
                    if existing_fp == record.payload_fingerprint:
                        # Identical duplicate delivery: Idempotent No-Op
                        cur.execute("COMMIT;")
                        return False
                    else:
                        # Conflicting duplicate identity!
                        reason = (
                            f"Conflicting payload for existing event_id '{record.event_id}'. "
                            f"Existing FP: {existing_fp}, Incoming FP: {record.payload_fingerprint}"
                        )
                        self._quarantine_record(cur, record, reason)
                        cur.execute("COMMIT;")
                        self._sync_fs()
                        raise ConflictingEventIdentityError(reason)

                # 2. Check for existing (writer_id, attempt_id, event_sequence)
                cur.execute(
                    """
                    SELECT event_id, payload_fingerprint FROM late_outcome_events
                    WHERE writer_id = ? AND attempt_id = ? AND event_sequence = ?;
                    """,
                    (record.writer_id, record.attempt_id, record.event_sequence),
                )
                row_seq = cur.fetchone()
                if row_seq is not None:
                    existing_eid, existing_fp = row_seq
                    if existing_eid == record.event_id and existing_fp == record.payload_fingerprint:
                        # Identical duplicate delivery
                        cur.execute("COMMIT;")
                        return False
                    else:
                        reason = (
                            f"Conflicting event sequence ({record.writer_id}, {record.attempt_id}, {record.event_sequence}). "
                            f"Existing event_id '{existing_eid}' with FP {existing_fp}, incoming event_id '{record.event_id}' with FP {record.payload_fingerprint}"
                        )
                        self._quarantine_record(cur, record, reason)
                        cur.execute("COMMIT;")
                        self._sync_fs()
                        raise ConflictingEventIdentityError(reason)

                # 3. Clean Insertion
                payload_json = json.dumps(record.payload, sort_keys=True)
                cur.execute(
                    """
                    INSERT INTO late_outcome_events (
                        event_id, run_id, operation_id, attempt_id, writer_id,
                        event_sequence, event_type, payload_fingerprint,
                        emitted_timestamp, arrival_timestamp, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        record.event_id,
                        record.run_id,
                        record.operation_id,
                        record.attempt_id,
                        record.writer_id,
                        record.event_sequence,
                        record.event_type,
                        record.payload_fingerprint,
                        record.emitted_timestamp,
                        record.arrival_timestamp,
                        payload_json,
                    ),
                )
                cur.execute("COMMIT;")
                self._sync_fs()
                return True
            except Exception:
                cur.execute("ROLLBACK;")
                raise

    def _quarantine_record(self, cur: sqlite3.Cursor, record: LateOutcomeRecord, reason: str) -> None:
        """Write conflicting record to quarantine table."""
        cur.execute(
            """
            INSERT INTO quarantine_late_events (event_id, run_id, reason, record_json, quarantined_at)
            VALUES (?, ?, ?, ?, ?);
            """,
            (
                record.event_id,
                record.run_id,
                reason,
                record.model_dump_json(),
                datetime.now(UTC).isoformat(),
            ),
        )

    def get_events(self, run_id: str) -> list[LateOutcomeRecord]:
        """Retrieve all late outcome events recorded for a given run ID, ordered by arrival."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                SELECT event_id, run_id, operation_id, attempt_id, writer_id,
                       event_sequence, event_type, payload_fingerprint,
                       emitted_timestamp, arrival_timestamp, payload_json
                FROM late_outcome_events
                WHERE run_id = ?
                ORDER BY arrival_timestamp ASC, event_sequence ASC;
                """,
                (run_id,),
            )
            rows = cur.fetchall()
            records = []
            for r in rows:
                payload = json.loads(r[10]) if r[10] else {}
                records.append(
                    LateOutcomeRecord(
                        event_id=r[0],
                        run_id=r[1],
                        operation_id=r[2],
                        attempt_id=r[3],
                        writer_id=r[4],
                        event_sequence=r[5],
                        event_type=r[6],
                        payload_fingerprint=r[7],
                        emitted_timestamp=r[8],
                        arrival_timestamp=r[9],
                        payload=payload,
                    )
                )
            return records

    def get_late_records(self, run_id: str) -> list[LateOutcomeRecord]:
        """Alias for get_events."""
        return self.get_events(run_id)

    def get_quarantined_events(self, run_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieve quarantined records indicating integrity violations."""
        with self._lock:
            cur = self._conn.cursor()
            if run_id:
                cur.execute(
                    "SELECT quarantine_id, event_id, run_id, reason, record_json, quarantined_at FROM quarantine_late_events WHERE run_id = ?;",
                    (run_id,),
                )
            else:
                cur.execute(
                    "SELECT quarantine_id, event_id, run_id, reason, record_json, quarantined_at FROM quarantine_late_events;"
                )
            rows = cur.fetchall()
            return [
                {
                    "quarantine_id": r[0],
                    "event_id": r[1],
                    "run_id": r[2],
                    "reason": r[3],
                    "record": json.loads(r[4]) if r[4] else {},
                    "quarantined_at": r[5],
                }
                for r in rows
            ]

    def has_integrity_violations(self, run_id: str) -> bool:
        """Check if any events for this run were quarantined due to conflicting identities."""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM quarantine_late_events WHERE run_id = ?;",
                (run_id,),
            )
            count = cur.fetchone()[0]
            return bool(count > 0)

    def close(self) -> None:
        """Close SQLite database connection."""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    def __enter__(self) -> "LateOutcomeLedger":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
