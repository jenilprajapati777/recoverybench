"""Exhaustive tests for P0-1 Transaction Identity remediation.

Validates the decoupling of transaction identity across:
- operation_id: Identifies one logical business operation.
- attempt_id: Identifies one specific tool invocation attempt.
- effect_id: Identifies ground-truth committed mutation in the mock service.
- request_fingerprint: Retained purely as diagnostic metadata.

Test Matrix:
A. Safe retry: same operation_id, different attempt_ids, same effect_id -> PASS, 1 effect
B. Unsafe retry: same operation_id, different attempt_ids, two different effect_ids -> FAIL, duplicate detected
C. Independent identical operations: different operation_ids, different effect_ids, identical request_fingerprints -> PASS
D. Independent operations with different amounts -> PASS
E. Missing operation_id -> INSUFFICIENT_EVIDENCE
F. Missing effect_id -> not counted as proven committed effect
G. SQLite migration: old database upgraded without losing historical data
H. Replay and reporting: new IDs survive persistence, retrieval and report generation
I. Existing integrations: Callable agent, LangGraph adapter, and SDK integration
"""

import sqlite3
from pathlib import Path
from typing import Any

from recoverybench.adapters.callable import wrap_agent
from recoverybench.adapters.langgraph import create_langchain_payment_tool
from recoverybench.agent.base import AgentResult, TaskContext, ToolCaller
from recoverybench.engine.runner import BoundToolClient, ExecutionEngine
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.evaluator.rules import RuleEngine
from recoverybench.faults.types import FaultConfig, FaultType
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.ledger.sqlite import SQLiteLedger
from recoverybench.models import (
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
    LedgerEvent,
    compute_request_fingerprint,
    hash_token,
)
from recoverybench.replay.engine import ReplayEngine
from recoverybench.reporting.json_reporter import JSONReporter
from recoverybench.scenarios.registry import ScenarioRegistry
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.sdk.tester import test_agent as rb_test_agent
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


def test_case_a_safe_retry_same_operation_same_effect() -> None:
    """Case A: Safe retry maintains same operation_id and resolves to same effect_id."""
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(
        expected_side_effects=1,
        max_duplicate_effects=0,
        require_stable_idempotency_key=True,
    )

    events = [
        # Attempt 1: Injected timeout after commit
        LedgerEvent(
            event_id=1,
            run_id="run_case_a",
            scenario_id="payment_timeout_after_commit",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_charge_100",
            attempt_id="att_1",
            effect_id="eff_ch_sim_0001",
            idempotency_key_hash=hash_token("idem_stable_key"),
            request_fingerprint="fp_same_params",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:00Z",
            evidence_reference="ch_sim_0001",
        ),
        # Attempt 2: Deduplicated idempotent replay
        LedgerEvent(
            event_id=2,
            run_id="run_case_a",
            scenario_id="payment_timeout_after_commit",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_charge_100",
            attempt_id="att_2",
            effect_id="eff_ch_sim_0001",
            idempotency_key_hash=hash_token("idem_stable_key"),
            request_fingerprint="fp_same_params",
            attempt_number=2,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.NOT_APPLICABLE,
            simulated_timestamp="2026-01-01T00:00:01Z",
            evidence_reference="ch_sim_0001",
        ),
    ]

    agent_result = AgentResult(success=True, attempts=2)
    evaluation = evaluator.evaluate(events, assertions, agent_result)

    assert evaluation.passed is True
    assert evaluation.verdict == EvaluationVerdict.PASS
    assert evaluation.actual_side_effects == 1
    assert evaluation.duplicate_effect_count == 0
    assert evaluation.idempotency_key_stable is True

    # Evidence citations
    side_effect_assertion = next(a for a in evaluation.assertions if a.name == "side_effect_count")
    assert any("op_charge_100" in ev and "eff_ch_sim_0001" in ev for ev in side_effect_assertion.evidence)


def test_case_b_unsafe_retry_creates_two_distinct_effects() -> None:
    """Case B: Unsafe retry with same operation_id creates two distinct effect_ids (Double Charge)."""
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(
        expected_side_effects=1,
        max_duplicate_effects=0,
        require_stable_idempotency_key=True,
    )

    events = [
        # Attempt 1: First commit (dropped ACK)
        LedgerEvent(
            event_id=1,
            run_id="run_case_b",
            scenario_id="payment_timeout_after_commit",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_charge_200",
            attempt_id="att_1",
            effect_id="eff_ch_sim_0001",
            idempotency_key_hash=hash_token("key_random_1"),
            request_fingerprint="fp_same_params",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:00Z",
            evidence_reference="ch_sim_0001",
        ),
        # Attempt 2: Second commit with different key (Double Charge!)
        LedgerEvent(
            event_id=2,
            run_id="run_case_b",
            scenario_id="payment_timeout_after_commit",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_charge_200",
            attempt_id="att_2",
            effect_id="eff_ch_sim_0002",
            idempotency_key_hash=hash_token("key_random_2"),
            request_fingerprint="fp_same_params",
            attempt_number=2,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:01Z",
            evidence_reference="ch_sim_0002",
        ),
    ]

    agent_result = AgentResult(success=True, attempts=2)
    evaluation = evaluator.evaluate(events, assertions, agent_result)

    assert evaluation.passed is False
    assert evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert evaluation.actual_side_effects == 2
    assert evaluation.duplicate_effect_count == 1
    assert evaluation.idempotency_key_stable is False

    # Evidence citations cite the specific operation and both effect IDs
    side_effect_assertion = next(a for a in evaluation.assertions if a.name == "side_effect_count")
    assert any(
        "DUPLICATE DETECTED for Operation 'op_charge_200'" in ev
        and "eff_ch_sim_0001" in ev
        and "eff_ch_sim_0002" in ev
        for ev in side_effect_assertion.evidence
    )


def test_case_c_independent_identical_operations_no_false_duplicate() -> None:
    """Case C: Two independent operations with identical parameters must NOT trigger duplicate warnings."""
    evaluator = RecoveryEvaluator()
    # Scenario expecting 2 distinct operations to succeed
    assertions = ScenarioAssertions(
        expected_side_effects=2,
        max_duplicate_effects=0,
        require_stable_idempotency_key=True,
    )

    # Identical parameters yield the exact same request fingerprint
    payload = {"amount": 2500, "currency": "USD", "customer_id": "cust_shared"}
    shared_fp = compute_request_fingerprint("payment_gateway", "charge", payload)

    events = [
        # Operation 1: Invoice #101
        LedgerEvent(
            event_id=1,
            run_id="run_case_c",
            scenario_id="multi_charge_scenario",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_invoice_101",
            attempt_id="att_101_1",
            effect_id="eff_ch_sim_0001",
            idempotency_key_hash=hash_token("key_invoice_101"),
            request_fingerprint=shared_fp,
            attempt_number=1,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:00Z",
            evidence_reference="ch_sim_0001",
        ),
        # Operation 2: Invoice #102 (identical parameters, but DIFFERENT operation_id)
        LedgerEvent(
            event_id=2,
            run_id="run_case_c",
            scenario_id="multi_charge_scenario",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_invoice_102",
            attempt_id="att_102_1",
            effect_id="eff_ch_sim_0002",
            idempotency_key_hash=hash_token("key_invoice_102"),
            request_fingerprint=shared_fp,  # IDENTICAL FINGERPRINT!
            attempt_number=1,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:01Z",
            evidence_reference="ch_sim_0002",
        ),
    ]

    agent_result = AgentResult(success=True, attempts=2)
    evaluation = evaluator.evaluate(events, assertions, agent_result)

    # Under the new architecture, this MUST PASS because operation_ids are distinct
    assert evaluation.passed is True
    assert evaluation.verdict == EvaluationVerdict.PASS
    assert evaluation.actual_side_effects == 2
    assert evaluation.duplicate_effect_count == 0
    assert evaluation.idempotency_key_stable is True


def test_case_d_independent_operations_with_different_amounts() -> None:
    """Case D: Multiple operations with different amounts and distinct operation IDs succeed."""
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(
        expected_side_effects=2,
        max_duplicate_effects=0,
    )

    events = [
        LedgerEvent(
            event_id=1,
            run_id="run_case_d",
            scenario_id="two_orders",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_order_1",
            attempt_id="att_1",
            effect_id="eff_order_1",
            request_fingerprint="fp_amount_50",
            attempt_number=1,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:00Z",
        ),
        LedgerEvent(
            event_id=2,
            run_id="run_case_d",
            scenario_id="two_orders",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_order_2",
            attempt_id="att_2",
            effect_id="eff_order_2",
            request_fingerprint="fp_amount_100",
            attempt_number=1,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:01Z",
        ),
    ]

    evaluation = evaluator.evaluate(events, assertions, AgentResult(success=True, attempts=2))
    assert evaluation.passed is True
    assert evaluation.actual_side_effects == 2
    assert evaluation.duplicate_effect_count == 0


def test_case_e_missing_operation_id_yields_insufficient_evidence() -> None:
    """Case E: Missing operation_id results in INSUFFICIENT_EVIDENCE rather than silent fingerprint fallback."""
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(expected_side_effects=1)

    events = [
        LedgerEvent(
            event_id=1,
            run_id="run_legacy",
            scenario_id="legacy_test",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id=None,  # MISSING OPERATION IDENTITY
            attempt_id="att_legacy_1",
            effect_id="eff_legacy_1",
            request_fingerprint="fp_legacy",
            attempt_number=1,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:00Z",
        )
    ]

    evaluation = evaluator.evaluate(events, assertions, AgentResult(success=True, attempts=1))

    assert evaluation.passed is False
    assert evaluation.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert any("insufficient_evidence_missing_operation_id" in str(a.actual) for a in evaluation.assertions)
    assert any("Missing operation identity" in r for r in evaluation.failure_reasons)


def test_case_f_missing_effect_id_not_counted_as_proven_committed() -> None:
    """Case F: An event with commit_status=COMMITTED but effect_id=None is NOT counted as proven."""
    assertions = ScenarioAssertions(expected_side_effects=1)

    # Event claims COMMITTED but has no effect_id proof
    events = [
        LedgerEvent(
            event_id=1,
            run_id="run_unproven",
            scenario_id="scen_unproven",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_unproven_1",
            attempt_id="att_1",
            effect_id=None,  # MISSING EFFECT PROOF
            request_fingerprint="fp_1",
            attempt_number=1,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:00Z",
        )
    ]

    assertion, actual_commits, duplicate_count = RuleEngine.evaluate_side_effects(events, assertions)

    # Must not count unproven commit towards actual_commits
    assert actual_commits == 0
    assert assertion.passed is False
    assert any("lack effect_id" in ev for ev in assertion.evidence)


def test_case_g_sqlite_schema_migration_preserves_historical_data(tmp_path: Path) -> None:
    """Case G: Existing SQLite database files with legacy schemas are migrated idempotently without data loss."""
    db_file = tmp_path / "legacy_ledger.db"

    # 1. Create a legacy database without operation_id, attempt_id, effect_id columns
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE runs (
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
            duration_ms REAL NOT NULL
        );

        CREATE TABLE effect_ledger (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            scenario_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            action_type TEXT NOT NULL,
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

        INSERT INTO effect_ledger (
            run_id, scenario_id, tool_name, action_type,
            idempotency_key_hash, request_fingerprint, attempt_number,
            execution_status, commit_status, simulated_timestamp, details_json
        ) VALUES (
            'run_legacy_db', 'scen_old', 'payment_gateway', 'charge',
            'hash_old', 'fp_old', 1, 'SUCCESS', 'COMMITTED', '2025-12-31T23:59:59Z', '{}'
        );
        """
    )
    conn.commit()
    conn.close()

    # 2. Instantiate SQLiteLedger on the legacy database (triggers migration)
    migrated_ledger = SQLiteLedger(db_file)

    # 3. Verify historical event was retrieved and safely mapped
    events = migrated_ledger.get_events("run_legacy_db")
    assert len(events) == 1
    legacy_event = events[0]
    assert legacy_event.run_id == "run_legacy_db"
    assert legacy_event.request_fingerprint == "fp_old"
    assert legacy_event.operation_id is None
    assert legacy_event.attempt_id is None
    assert legacy_event.effect_id is None

    # 4. Append a modern event with the new identifiers to the migrated database
    modern_event = LedgerEvent(
        run_id="run_modern_db",
        scenario_id="scen_new",
        tool_name="payment_gateway",
        action_type="charge",
        operation_id="op_modern_999",
        attempt_id="att_modern_1",
        effect_id="eff_modern_1",
        idempotency_key_hash="hash_modern",
        request_fingerprint="fp_modern",
        attempt_number=1,
        execution_status=ExecutionStatus.SUCCESS,
        commit_status=CommitStatus.COMMITTED,
        simulated_timestamp="2026-01-01T12:00:00Z",
    )
    saved = migrated_ledger.append(modern_event)
    assert saved.event_id is not None

    # 5. Retrieve modern event and verify full persistence
    modern_retrieved = migrated_ledger.get_events("run_modern_db")
    assert len(modern_retrieved) == 1
    assert modern_retrieved[0].operation_id == "op_modern_999"
    assert modern_retrieved[0].attempt_id == "att_modern_1"
    assert modern_retrieved[0].effect_id == "eff_modern_1"


def test_case_h_replay_and_reporting_persistence(tmp_path: Path) -> None:
    """Case H: New identifiers survive replay trace extraction and JSON reporting."""
    db_file = tmp_path / "replay_test.db"
    ledger = SQLiteLedger(db_file)
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="test_replay_identities",
        name="Test Replay Scenario",
        description="Verify identifier survival in traces",
        target_tool="payment_gateway",
        target_action="charge",
        task={"amount": 3000, "currency": "USD", "operation_id": "op_persisted_777"},
        assertions=ScenarioAssertions(expected_side_effects=1),
    )

    def simple_agent(context: TaskContext, tool: ToolCaller) -> dict[str, Any]:
        return tool(
            "payment_gateway",
            "charge",
            context.task_params,
            idempotency_key="key_persisted_777",
            operation_id="op_persisted_777",
        )

    run_result = engine.run(scenario, simple_agent, run_id="run_replay_test_123")
    assert run_result.passed is True

    # Check ReplayEngine trace
    replay_engine = ReplayEngine(ledger)
    trace = replay_engine.replay_run("run_replay_test_123")
    assert trace is not None
    assert len(trace.reconstructed_timeline) == 1
    timeline_entry = trace.reconstructed_timeline[0]
    assert timeline_entry["operation_id"] == "op_persisted_777"
    assert timeline_entry["attempt_id"] == "att_run_replay_test_123_1"
    assert str(timeline_entry["effect_id"]).startswith("eff_ch_sim_")

    # Check JSON Report serialization
    report_dict = JSONReporter.generate_report_dict(run_result)
    events_list = report_dict.get("events")
    assert isinstance(events_list, list)
    event_dict = events_list[0]
    assert isinstance(event_dict, dict)
    assert event_dict["operation_id"] == "op_persisted_777"
    assert event_dict["attempt_id"] == "att_run_replay_test_123_1"
    assert str(event_dict["effect_id"]).startswith("eff_ch_sim_")


def test_case_i_callable_multi_operation_agent(timeout_after_scenario: ScenarioModel) -> None:
    """Case I: Callable agent executing two distinct operations within one run."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    # Scenario expecting 2 distinct operations to succeed
    multi_op_scenario = timeout_after_scenario.model_copy(
        update={
            "scenario_id": "two_charges_scenario",
            "faults": [],  # Clean run to test identity separation
            "assertions": ScenarioAssertions(
                expected_side_effects=2,
                max_duplicate_effects=0,
            ),
        }
    )

    @wrap_agent
    def multi_charge_agent(context: TaskContext, tool: ToolCaller) -> AgentResult:
        # Charge 1: $50
        res1 = tool(
            "payment_gateway",
            "charge",
            {"amount": 5000, "currency": "USD"},
            idempotency_key="idem_op_1",
            operation_id="op_first_charge",
        )
        # Charge 2: $50 with identical arguments!
        res2 = tool(
            "payment_gateway",
            "charge",
            {"amount": 5000, "currency": "USD"},
            idempotency_key="idem_op_2",
            operation_id="op_second_charge",
        )
        return AgentResult(success=True, data={"res1": res1, "res2": res2}, attempts=2)

    result = engine.run(multi_op_scenario, multi_charge_agent, run_id="run_multi_op")
    assert result.passed is True
    assert result.evaluation.actual_side_effects == 2
    assert result.evaluation.duplicate_effect_count == 0


def test_case_i_sdk_fluent_runner() -> None:
    """Case I: Test SDK test_agent integration with new transaction identities."""
    def safe_callable(context: TaskContext, tool: ToolCaller) -> dict[str, Any]:
        return tool(
            "payment_gateway",
            "charge",
            context.task_params,
            idempotency_key="idem_fluent_test",
        )

    res = rb_test_agent(
        "payment_timeout_before_commit",
        safe_callable,
        run_id="run_sdk_test",
        seed=42,
    )
    # Built-in scenarios have faults injected, but here we confirm events capture operation_id and attempt_id
    assert len(res.events) >= 1
    assert res.events[0].operation_id == "op_payment_timeout_before_commit"
    assert res.events[0].attempt_id == "att_run_sdk_test_1"


def test_case_a_single_operation_declared_default_identity() -> None:
    """Case A: Single-operation scenario using declared default identity."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="single_op_demo",
        name="Single Operation Demo",
        description="Verify single operation uses declared default identity",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=True,
        task={"amount": 4200, "currency": "USD", "customer_id": "cust_alice"},
        assertions=ScenarioAssertions(expected_side_effects=1, max_duplicate_effects=0),
    )

    def single_op_agent(context: TaskContext, tool: ToolCaller) -> dict[str, Any]:
        # Agent does NOT pass operation_id; relies on context default
        return tool(
            "payment_gateway",
            "charge",
            context.task_params,
            idempotency_key="key_single_alice",
        )

    result = engine.run(scenario, single_op_agent, run_id="run_case_a_single")

    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.evaluation.actual_side_effects == 1
    assert result.evaluation.duplicate_effect_count == 0

    # Assert ledger events captured the default op_{scenario_id}
    events = result.events
    assert len(events) == 1
    assert events[0].operation_id == "op_single_op_demo"
    assert events[0].attempt_id == "att_run_case_a_single_1"
    assert events[0].effect_id == "eff_ch_sim_0001"

    # Assert supporting evidence cites the operation_id and effect_id
    effects_assertion = next(a for a in result.evaluation.assertions if a.name == "side_effect_count")
    assert any("op_single_op_demo" in ev and "eff_ch_sim_0001" in ev for ev in effects_assertion.evidence)


def test_case_b_multi_operation_five_independent_operations() -> None:
    """Case B: Multi-operation scenario using five independent operations with distinct IDs."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    # Multi-operation scenario expecting 5 side effects
    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="multi_op_five_orders",
        name="Multi Operation 5 Orders",
        description="Verify 5 distinct operations succeed with distinct operation IDs",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=False,
        task={"currency": "USD"},
        assertions=ScenarioAssertions(
            expected_side_effects=5,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
        ),
    )

    def five_ops_agent(context: TaskContext, tool: ToolCaller) -> dict[str, Any]:
        results = []
        for i in range(1, 6):
            op_id = f"op_batch_order_{i}"
            # Test helper context manager with tool.operation(op_id)
            if hasattr(tool, "operation"):
                with tool.operation(op_id):
                    res = tool(
                        "payment_gateway",
                        "charge",
                        {"amount": 1000 * i, "customer_id": f"cust_{i}"},
                        idempotency_key=f"key_order_{i}",
                    )
            else:
                res = tool(
                    "payment_gateway",
                    "charge",
                    {"amount": 1000 * i, "customer_id": f"cust_{i}"},
                    idempotency_key=f"key_order_{i}",
                    operation_id=op_id,
                )
            results.append(res)
        return {"success": True, "processed": len(results)}

    result = engine.run(scenario, five_ops_agent, run_id="run_case_b_five")

    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.evaluation.actual_side_effects == 5
    assert result.evaluation.duplicate_effect_count == 0

    # Verify each of the 5 events has a distinct operation_id and unique effect_id
    events = result.events
    assert len(events) == 5
    op_ids = [e.operation_id for e in events]
    effect_ids = [e.effect_id for e in events]
    assert len(set(op_ids)) == 5
    assert len(set(effect_ids)) == 5

    # Evidence assertion
    effects_assertion = next(a for a in result.evaluation.assertions if a.name == "side_effect_count")
    for i in range(1, 6):
        assert any(f"op_batch_order_{i}" in ev for ev in effects_assertion.evidence)


def test_case_c_multi_operation_identical_payloads_distinct_operation_ids() -> None:
    """Case C: Multi-operation scenario with identical payloads but distinct operation IDs."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="multi_op_identical_payloads",
        name="Identical Payloads Distinct Ops",
        description="Verify identical request fingerprints do not trigger false duplicates",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=False,
        task={},
        assertions=ScenarioAssertions(
            expected_side_effects=2,
            max_duplicate_effects=0,
        ),
    )

    def twin_orders_agent(context: TaskContext, tool: BoundToolClient) -> dict[str, Any]:
        identical_payload = {"amount": 5000, "currency": "USD", "customer_id": "cust_twin"}
        # Operation 1
        with tool.operation("op_order_alpha"):
            res1 = tool("payment_gateway", "charge", identical_payload, idempotency_key="key_alpha")
        # Operation 2 with exact same payload
        with tool.operation("op_order_beta"):
            res2 = tool("payment_gateway", "charge", identical_payload, idempotency_key="key_beta")
        return {"success": True, "res1": res1, "res2": res2}

    result = engine.run(scenario, twin_orders_agent, run_id="run_case_c_twins")

    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.evaluation.actual_side_effects == 2
    assert result.evaluation.duplicate_effect_count == 0

    events = result.events
    assert len(events) == 2
    # Verify request fingerprints are identical, proving that identity is decoupled!
    assert events[0].request_fingerprint == events[1].request_fingerprint
    assert events[0].operation_id == "op_order_alpha"
    assert events[1].operation_id == "op_order_beta"
    assert events[0].effect_id != events[1].effect_id

    # Evidence assertion asserts both operations verified independently
    effects_assertion = next(a for a in result.evaluation.assertions if a.name == "side_effect_count")
    assert any("op_order_alpha" in ev for ev in effects_assertion.evidence)
    assert any("op_order_beta" in ev for ev in effects_assertion.evidence)


def test_case_d_multi_operation_omitted_operation_id_yields_insufficient_evidence() -> None:
    """Case D: Multi-operation scenario where an operation ID is omitted yields INSUFFICIENT_EVIDENCE."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="multi_op_missing_id",
        name="Multi Op Missing Identity",
        description="Verify omitted operation ID produces INSUFFICIENT_EVIDENCE in multi-op scenarios",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=False,  # Explicitly multi-operation
        task={},
        assertions=ScenarioAssertions(expected_side_effects=2),
    )

    def careless_agent(context: TaskContext, tool: ToolCaller) -> dict[str, Any]:
        # Omission of operation_id in a multi-operation scenario
        return tool(
            "payment_gateway",
            "charge",
            {"amount": 1000, "currency": "USD"},
            idempotency_key="key_careless",
        )

    result = engine.run(scenario, careless_agent, run_id="run_case_d_omitted")

    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert result.events[0].operation_id is None

    effects_assertion = next(a for a in result.evaluation.assertions if a.name == "side_effect_count")
    assert effects_assertion.passed is False
    assert effects_assertion.actual == "insufficient_evidence_missing_operation_id"
    assert len(effects_assertion.event_ids) >= 1
    assert any("without operation_id" in ev for ev in effects_assertion.evidence)
    assert any("Fingerprint-based inference is prohibited" in ev for ev in effects_assertion.evidence)


def test_case_e_retry_attempts_preserving_original_operation_id_safe_and_unsafe() -> None:
    """Case E: Retry attempts preserving original operation_id (safe pass vs unsafe failure)."""
    # Part 1: Safe Retry
    ledger_safe = InMemoryLedger()
    registry_safe = ToolRegistry()
    registry_safe.register(MockPaymentService())
    engine_safe = ExecutionEngine(ledger=ledger_safe, tool_registry=registry_safe)

    scenario_safe = ScenarioModel(
        schema_version=1,
        scenario_id="retry_preserves_id",
        name="Retry Preserves ID",
        description="Verify retries reuse the same operation_id",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.TIMEOUT_AFTER_COMMIT,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=1,
            )
        ],
        single_operation=False,
        task={},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
        ),
    )

    def safe_retrying_agent(context: TaskContext, tool: BoundToolClient) -> dict[str, Any]:
        # Wrap retries within operation context scope
        with tool.operation("op_safe_retry_task"):
            try:
                return tool("payment_gateway", "charge", {"amount": 2000}, idempotency_key="stable_key")
            except Exception:
                # Retry preserving the same operation_id and idempotency key
                return tool("payment_gateway", "charge", {"amount": 2000}, idempotency_key="stable_key")

    res_safe = engine_safe.run(scenario_safe, safe_retrying_agent, run_id="run_retry_safe")
    assert res_safe.passed is True
    assert res_safe.evaluation.verdict == EvaluationVerdict.PASS
    assert res_safe.evaluation.actual_side_effects == 1
    assert res_safe.evaluation.duplicate_effect_count == 0

    assert len(res_safe.events) == 2
    assert res_safe.events[0].operation_id == "op_safe_retry_task"
    assert res_safe.events[1].operation_id == "op_safe_retry_task"
    assert res_safe.events[0].attempt_id != res_safe.events[1].attempt_id
    assert res_safe.events[0].effect_id == res_safe.events[1].effect_id

    # Part 2: Unsafe Retry
    ledger_unsafe = InMemoryLedger()
    registry_unsafe = ToolRegistry()
    registry_unsafe.register(MockPaymentService())
    engine_unsafe = ExecutionEngine(ledger=ledger_unsafe, tool_registry=registry_unsafe)

    def unsafe_retrying_agent(context: TaskContext, tool: BoundToolClient) -> dict[str, Any]:
        with tool.operation("op_unsafe_retry_task"):
            try:
                return tool("payment_gateway", "charge", {"amount": 2000}, idempotency_key="key_1")
            except Exception:
                # Unsafe retry: different key under the SAME operation_id
                return tool("payment_gateway", "charge", {"amount": 2000}, idempotency_key="key_2")

    res_unsafe = engine_unsafe.run(scenario_safe, unsafe_retrying_agent, run_id="run_retry_unsafe")
    assert res_unsafe.passed is False
    assert res_unsafe.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert res_unsafe.evaluation.duplicate_effect_count == 1
    effects_assertion = next(a for a in res_unsafe.evaluation.assertions if a.name == "side_effect_count")
    assert any("DUPLICATE DETECTED for Operation 'op_unsafe_retry_task'" in ev for ev in effects_assertion.evidence)


def test_case_f_committed_event_no_effect_id_and_independent_violation_preservation() -> None:
    """Case F: Committed event with no effect_id yields INSUFFICIENT_EVIDENCE, but preserves independent violations."""
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(
        expected_side_effects=1,
        max_allowed_retries=3,
        require_stable_idempotency_key=True,
    )

    # 1. Single committed event with missing effect_id
    unverified_event = LedgerEvent(
        event_id=42,
        run_id="run_unverified",
        scenario_id="scen_unverified",
        tool_name="payment_gateway",
        action_type="charge",
        operation_id="op_unverified_42",
        attempt_id="att_1",
        effect_id=None,  # Missing effect_id
        idempotency_key_hash=hash_token("stable_key"),
        request_fingerprint="fp_42",
        attempt_number=1,
        execution_status=ExecutionStatus.SUCCESS,
        commit_status=CommitStatus.COMMITTED,
        simulated_timestamp="2026-01-01T00:00:00Z",
    )

    eval_result = evaluator.evaluate([unverified_event], assertions, AgentResult(success=True, attempts=1))
    assert eval_result.passed is False
    assert eval_result.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert eval_result.actual_side_effects == 0  # Not counted as proven

    effects_assertion = next(a for a in eval_result.assertions if a.name == "side_effect_count")
    assert effects_assertion.actual == "insufficient_evidence_missing_effect_id"
    assert 42 in effects_assertion.event_ids
    assert any("without verified effect_id (lacks effect_id)" in ev for ev in effects_assertion.evidence)

    # 2. Independent violation preservation: Agent retried 5 times (exceeding budget of 3)
    excess_retry_events = [
        LedgerEvent(
            event_id=i,
            run_id="run_excess",
            scenario_id="scen_unverified",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_unverified_42",
            attempt_id=f"att_{i}",
            effect_id=None,
            idempotency_key_hash=hash_token("stable_key"),
            request_fingerprint="fp_42",
            attempt_number=i,
            execution_status=ExecutionStatus.SUCCESS if i == 5 else ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.COMMITTED if i == 5 else CommitStatus.NOT_APPLICABLE,
            simulated_timestamp="2026-01-01T00:00:00Z",
        )
        for i in range(1, 6)
    ]

    eval_excess = evaluator.evaluate(excess_retry_events, assertions, AgentResult(success=True, attempts=5))
    # Must preserve GENUINE_FAILURE because retry limit was independently violated!
    assert eval_excess.passed is False
    assert eval_excess.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert any("Exceeded retry threshold" in r for r in eval_excess.failure_reasons)


def test_case_g_historical_sqlite_event_missing_identifiers_evaluated(tmp_path: Path) -> None:
    """Case G: Historical SQLite event with missing new identifiers evaluates to INSUFFICIENT_EVIDENCE."""
    db_file = tmp_path / "legacy_eval_test.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, scenario_id TEXT, agent_id TEXT, status TEXT,
            verdict TEXT, passed INTEGER, total_events INTEGER, committed_effects INTEGER,
            duplicate_effects INTEGER, created_at TEXT, duration_ms REAL
        );
        CREATE TABLE effect_ledger (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, scenario_id TEXT NOT NULL, tool_name TEXT NOT NULL,
            action_type TEXT NOT NULL, idempotency_key_hash TEXT, request_fingerprint TEXT NOT NULL,
            attempt_number INTEGER NOT NULL, execution_status TEXT NOT NULL, commit_status TEXT NOT NULL,
            simulated_timestamp TEXT NOT NULL, error_type TEXT, evidence_reference TEXT, details_json TEXT NOT NULL
        );
        INSERT INTO effect_ledger (
            run_id, scenario_id, tool_name, action_type, idempotency_key_hash,
            request_fingerprint, attempt_number, execution_status, commit_status,
            simulated_timestamp, details_json
        ) VALUES (
            'run_historical_001', 'scen_old', 'payment_gateway', 'charge',
            'hash_old', 'fp_old', 1, 'SUCCESS', 'COMMITTED', '2025-01-01T00:00:00Z', '{}'
        );
        """
    )
    conn.commit()
    conn.close()

    migrated_ledger = SQLiteLedger(db_file)
    events = migrated_ledger.get_events("run_historical_001")
    assert len(events) == 1
    assert events[0].operation_id is None
    assert events[0].effect_id is None

    evaluator = RecoveryEvaluator()
    evaluation = evaluator.evaluate(
        events,
        ScenarioAssertions(expected_side_effects=1),
        AgentResult(success=True, attempts=1),
    )

    assert evaluation.passed is False
    assert evaluation.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert any("insufficient_evidence_missing_operation_id" in str(a.actual) for a in evaluation.assertions)
    effects_assertion = next(a for a in evaluation.assertions if a.name == "side_effect_count")
    assert events[0].event_id in effects_assertion.event_ids
    assert any("Missing operation identity" in ev for ev in effects_assertion.evidence)


def test_case_h_existing_payment_demo_and_langgraph_integration() -> None:
    """Case H: Verify existing payment demo and LangGraph integration with hardened identity."""
    # 1. CLI demo verification
    registry = ScenarioRegistry()
    scenario = registry.get("payment_timeout_after_commit")
    assert scenario is not None
    assert scenario.is_single_operation is True

    # 2. LangGraph tool bridge verification
    tool_calls_recorded = []

    def mock_dispatcher(tool: str, action: str, params: dict[str, Any], key: str | None = None, op_id: str | None = None) -> dict[str, Any]:
        tool_calls_recorded.append({"tool": tool, "action": action, "params": params, "key": key, "op_id": op_id})
        return {"status": "succeeded", "charge_id": "ch_mock_123"}

    client = BoundToolClient(mock_dispatcher, default_operation_id="op_scenario_lg")
    lc_tool = create_langchain_payment_tool(client)

    # Calling tool within operation context
    with client.operation("op_custom_lg"):
        lc_tool.invoke({"amount": 7500, "currency": "USD", "idempotency_key": "lg_key_1"})

    assert len(tool_calls_recorded) == 1
    assert tool_calls_recorded[0]["op_id"] == "op_custom_lg"
    assert tool_calls_recorded[0]["key"] == "lg_key_1"
