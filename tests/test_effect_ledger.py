"""Unit tests for in-memory and SQLite append-only effect ledgers."""

from pathlib import Path

import pytest

from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.ledger.sqlite import SQLiteLedger
from recoverybench.models import (
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
    LedgerEvent,
    RunSummary,
    compute_request_fingerprint,
    hash_token,
)


def test_in_memory_ledger() -> None:
    ledger = InMemoryLedger()
    event1 = LedgerEvent(
        run_id="run_1",
        scenario_id="scen_1",
        tool_name="payment_gateway",
        action_type="charge",
        idempotency_key_hash=hash_token("key_1"),
        request_fingerprint=compute_request_fingerprint("payment_gateway", "charge", {"amount": 5000}),
        attempt_number=1,
        execution_status=ExecutionStatus.FAULT_INJECTED,
        commit_status=CommitStatus.COMMITTED,
        simulated_timestamp="2026-01-01T00:00:00Z",
        error_type="ToolTimeoutError",
    )
    saved1 = ledger.append(event1)
    assert saved1.event_id == 1

    event2 = event1.model_copy(
        update={"attempt_number": 2, "execution_status": ExecutionStatus.SUCCESS}
    )
    saved2 = ledger.append(event2)
    assert saved2.event_id == 2

    events = ledger.get_events("run_1")
    assert len(events) == 2
    assert ledger.count_attempts("run_1", saved1.request_fingerprint) == 2
    assert ledger.count_committed_effects("run_1") == 2


def test_sqlite_ledger_persistence(tmp_path: Path) -> None:
    db_file = tmp_path / "ledger.db"
    ledger = SQLiteLedger(db_file)

    event = LedgerEvent(
        run_id="run_sql_1",
        scenario_id="scen_sql",
        tool_name="payment_gateway",
        action_type="charge",
        idempotency_key_hash=hash_token("idem_99"),
        request_fingerprint="fp_abc123",
        attempt_number=1,
        execution_status=ExecutionStatus.SUCCESS,
        commit_status=CommitStatus.COMMITTED,
        simulated_timestamp="2026-01-01T12:00:00Z",
        evidence_reference="ch_sim_0001",
        details={"currency": "USD"},
    )
    saved = ledger.append(event)
    assert saved.event_id == 1

    # Record Run
    summary = RunSummary(
        run_id="run_sql_1",
        scenario_id="scen_sql",
        agent_id="test_agent",
        passed=True,
        verdict=EvaluationVerdict.PASS,
        total_events=1,
        committed_effects=1,
        duplicate_effects=0,
        created_at="2026-01-01T12:00:00Z",
        duration_ms=45.2,
    )
    ledger.record_run(summary)

    # Re-open database from disk and verify persistence
    ledger2 = SQLiteLedger(db_file)
    events = ledger2.get_events("run_sql_1")
    assert len(events) == 1
    assert events[0].evidence_reference == "ch_sim_0001"
    assert events[0].details["currency"] == "USD"

    retrieved_run = ledger2.get_run("run_sql_1")
    assert retrieved_run is not None
    assert retrieved_run.passed is True
    assert retrieved_run.verdict == EvaluationVerdict.PASS


def test_token_hashing_and_fingerprinting() -> None:
    token = "secret_customer_card_token_123"
    hashed = hash_token(token)
    assert hashed is not None
    assert len(hashed) == 16
    assert token not in hashed

    fp1 = compute_request_fingerprint("payment_gateway", "charge", {"amount": 5000, "customer": "cust_1"})
    fp2 = compute_request_fingerprint("payment_gateway", "charge", {"customer": "cust_1", "amount": 5000})
    # Order independence in payload
    assert fp1 == fp2


def test_sqlite_ledger_in_memory_immediate_clear_run() -> None:
    """Verify that clear_run on a freshly instantiated in-memory SQLiteLedger succeeds without missing table error."""
    ledger = SQLiteLedger(":memory:")
    # This must not raise sqlite3.OperationalError: no such table: effect_ledger
    ledger.clear_run("run_fresh_123")
    assert ledger.get_events("run_fresh_123") == []


def test_sqlite_ledger_in_memory_lifecycle() -> None:
    """Verify full read/write/clear lifecycle for SQLiteLedger(':memory:')."""
    ledger = SQLiteLedger(":memory:")

    event = LedgerEvent(
        run_id="run_mem_1",
        scenario_id="scen_mem",
        tool_name="payment_service",
        action_type="charge",
        operation_id="op_123",
        attempt_id="att_123",
        effect_id="eff_123",
        idempotency_key_hash=hash_token("key_mem"),
        request_fingerprint="fp_mem_123",
        attempt_number=1,
        execution_status=ExecutionStatus.SUCCESS,
        commit_status=CommitStatus.COMMITTED,
        simulated_timestamp="2026-11-15T12:00:00Z",
        details={"amount": 100},
    )
    saved = ledger.append(event)
    assert saved.event_id == 1

    events = ledger.get_events("run_mem_1")
    assert len(events) == 1
    assert events[0].operation_id == "op_123"
    assert events[0].attempt_id == "att_123"
    assert events[0].effect_id == "eff_123"

    committed = ledger.get_committed_effects("run_mem_1")
    assert len(committed) == 1
    assert ledger.count_committed_effects("run_mem_1") == 1

    summary = RunSummary(
        run_id="run_mem_1",
        scenario_id="scen_mem",
        agent_id="agent_mem",
        passed=True,
        verdict=EvaluationVerdict.PASS,
        total_events=1,
        committed_effects=1,
        duplicate_effects=0,
        created_at="2026-11-15T12:00:00Z",
        duration_ms=25.0,
        isolation_mode="container",
    )
    ledger.record_run(summary)

    retrieved = ledger.get_run("run_mem_1")
    assert retrieved is not None
    assert retrieved.passed is True
    assert retrieved.isolation_mode == "container"

    runs = ledger.list_runs()
    assert len(runs) == 1
    assert runs[0].run_id == "run_mem_1"

    # Clear run and verify data is deleted but schema remains intact
    ledger.clear_run("run_mem_1")
    assert ledger.get_events("run_mem_1") == []
    assert ledger.get_run("run_mem_1") is None

    # Verify we can append again after clear_run
    saved2 = ledger.append(event)
    assert saved2.event_id is not None
    assert len(ledger.get_events("run_mem_1")) == 1


def test_sqlite_ledger_in_memory_instance_isolation() -> None:
    """Verify that multiple SQLiteLedger(':memory:') instances are strictly isolated from each other."""
    ledger_a = SQLiteLedger(":memory:")
    ledger_b = SQLiteLedger(":memory:")

    event_a = LedgerEvent(
        run_id="run_a",
        scenario_id="scen_a",
        tool_name="payment_service",
        action_type="charge",
        operation_id="op_a",
        request_fingerprint="fp_a",
        attempt_number=1,
        execution_status=ExecutionStatus.SUCCESS,
        commit_status=CommitStatus.COMMITTED,
        simulated_timestamp="2026-11-15T12:00:00Z",
    )
    ledger_a.append(event_a)

    # ledger_a has the event
    assert len(ledger_a.get_events("run_a")) == 1
    # ledger_b MUST NOT see ledger_a's events
    assert len(ledger_b.get_events("run_a")) == 0


def test_sqlite_ledger_in_memory_close_lifecycle() -> None:
    """Verify that explicit close() cleans up connection and prevents subsequent access."""
    ledger = SQLiteLedger(":memory:")
    ledger.clear_run("run_test")
    ledger.close()

    with pytest.raises(RuntimeError) as exc_info:
        ledger.get_events("run_test")
    assert "in-memory database is closed" in str(exc_info.value)
