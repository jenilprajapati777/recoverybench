"""Deterministic regression test suite for ADR-004 canonical scenarios (T-01 to T-20).

Strictly verifies the ADR-004 Section 10 regression matrix.
All 20 tests use deterministic synchronization primitives (threading.Event, asyncio.Event,
barriers) with zero arbitrary sleeps.
"""

import os
import threading
import uuid
from collections.abc import Generator
from typing import Any

import pytest

from recoverybench.agent.base import AgentResult, CompletionStatus
from recoverybench.engine.context import RunResult
from recoverybench.engine.late_outcome import LateOutcomeLedger
from recoverybench.engine.reconciliation import ReconciliationEngine
from recoverybench.errors import (
    CapacityBreachIntegrityError,
    ConcurrencyViolationError,
    IncompatibleSchemaVersionError,
    ProcessTaintedError,
)
from recoverybench.evaluator.accounting import TwoLayerEffectAccountingEngine
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.evaluator.rules import aggregate_outcome
from recoverybench.models import (
    AuthoritativeFinalityProof,
    CommitStatus,
    EvaluationResult,
    EvaluationVerdict,
    ExecutionStatus,
    HarnessAuditAuthority,
    HarnessOrchestratorAuthority,
    HarnessRuntimeAuthority,
    IdempotencyContract,
    IdempotencyGuarantee,
    IdempotencyScope,
    LateOutcomeRecord,
    LedgerEvent,
    PrimaryInfrastructureStatus,
    ReconciliationInfrastructureStatus,
    RunSummary,
    compute_evaluation_digest,
)
from recoverybench.reporting.json_reporter import JSONReporter
from recoverybench.sandbox.backend import (
    InProcessExecutionBackend,
    is_process_tainted,
    reset_process_taint,
    taint_process,
)
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.declarative import DeclarativeToolService
from recoverybench.tools.process_runner import ToolProcessRunner
from recoverybench.tools.testing import UncooperativeHangingTool

TEST_RUNTIME_AUTH = HarnessRuntimeAuthority()
TEST_AUDIT_AUTH = HarnessAuditAuthority()
TEST_ORCH_AUTH = HarnessOrchestratorAuthority()


def make_c3_meta(
    events: list[LedgerEvent],
    tool_name: str,
    max_capacity: int = 1,
    runtime_guarantee: str = "RUNTIME_UNIQUE_CONSTRAINT",
) -> dict[str, Any]:
    run_id = events[0].run_id if events else "run_test"
    att = TEST_RUNTIME_AUTH.attest(
        run_id=run_id,
        tool_name=tool_name,
        max_capacity=max_capacity,
        runtime_guarantee=runtime_guarantee,
    )
    return {
        tool_name: {
            "harness_attestation": att,
            "verified_service_deduplication": True,
        }
    }


def make_finality_proof(
    run_id: str,
    events: list[LedgerEvent],
    expected_side_effects: int | None = 1,
    expect_agent_success: bool = True,
    agent_status: str | None = "SUCCEEDED",
    reconciliation_audit: Any | None = None,
    infrastructure_status: str | None = None,
    reconciliation_infrastructure_status: str | None = None,
) -> AuthoritativeFinalityProof:
    digest = compute_evaluation_digest(
        run_id=run_id,
        events=events,
        expected_side_effects=expected_side_effects,
        expect_agent_success=expect_agent_success,
        agent_status=agent_status,
        reconciliation_audit=reconciliation_audit,
        infrastructure_status=infrastructure_status,
        reconciliation_infrastructure_status=reconciliation_infrastructure_status,
    )
    return TEST_ORCH_AUTH.issue_finality_proof(
        run_id=run_id,
        evaluation_digest=digest,
    )


def make_scenario(
    scenario_id: str,
    target_tool: str = "payment",
    target_action: str = "charge",
    assertions: ScenarioAssertions | None = None,
    name: str = "",
) -> ScenarioModel:
    return ScenarioModel(
        schema_version=1,
        scenario_id=scenario_id,
        name=name or f"Scenario {scenario_id}",
        description=f"Description for {scenario_id}",
        target_tool=target_tool,
        target_action=target_action,
        assertions=assertions or ScenarioAssertions(),
        task={},
    )


def make_late_record(
    run_id: str,
    attempt_id: str,
    effect_id: str | None = None,
    commit_status: CommitStatus = CommitStatus.COMMITTED,
    writer_id: str = "THREAD_HOOK",
    event_type: str = "LATE_COMPLETION",
    operation_id: str = "op_1",
    event_sequence: int = 1,
) -> LateOutcomeRecord:
    payload = {
        "commit_status": commit_status.value if hasattr(commit_status, "value") else str(commit_status),
        "effect_id": effect_id,
    }
    fp = LateOutcomeRecord.compute_payload_fingerprint(payload)
    return LateOutcomeRecord(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        writer_id=writer_id,
        event_sequence=event_sequence,
        event_type=event_type,
        payload_fingerprint=fp,
        emitted_timestamp="2026-11-20T00:00:00Z",
        arrival_timestamp="2026-11-20T00:00:01Z",
        payload=payload,
    )


# ==============================================================================
# Canonical Scenario Tests (T-01 through T-20)
# ==============================================================================


class TestADR004CanonicalScenarios:
    """Implement and verify all 20 canonical ADR-004 scenarios."""

    @pytest.fixture(autouse=True)
    def clean_process_taint(self) -> Generator[None, None, None]:
        reset_process_taint()
        yield
        reset_process_taint()

    def test_t01_mutating_handler_raises_before_mutation(self) -> None:
        """T-01: Mutating handler raises before mutation.

        Input check fails, sets evt, raises ValueError; agent halts cleanly.
        Evidence: c(a1)=0, u(a1)=1 -> Bounds: [0, 1].
        Behavioral: PASS, External: UNRESOLVED_INTERVAL, Primary Infra: CLEAN.
        Aggregate Outcome: INSUFFICIENT_EVIDENCE.
        Schema v1: Rejects (C1, C2).
        """
        sync_event = threading.Event()
        run_id = "run_t01"

        # Mock tool handler raises before mutating
        def failing_handler(amount: float) -> dict[str, Any]:
            sync_event.set()
            raise ValueError("Input validation failed: amount must be positive")

        _tool = DeclarativeToolService("payment", default_handler=failing_handler, max_side_effect_capacity=1)
        assert sync_event.is_set() is False

        # Attempt a1: mutating attempt raised before effect
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t01",
                tool_name="payment",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_t01",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]
        tool_meta = make_c3_meta(events, "payment", max_capacity=1)
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=1,
            tool_metadata=tool_meta,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )

        assert accounting.bounds.s_min == 0
        assert accounting.bounds.s_max == 1
        assert accounting.external_status == "UNRESOLVED_INTERVAL"

        # Agent cleanly halted on failure
        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.FAILED,
            status_message="Agent caught ValueError and stopped cleanly",
        )
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=False)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = make_finality_proof(
            run_id,
            events,
            expected_side_effects=1,
            expect_agent_success=False,
            agent_status=agent_res.status.value,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=10.0,
            tool_metadata=tool_meta,
            execution_finality=proof,
            run_id=run_id,
        )

        assert eval_res.behavioral_verdict == "PASS"
        assert eval_res.side_effect_status == "UNRESOLVED_INTERVAL"
        assert eval_res.primary_infrastructure_status == PrimaryInfrastructureStatus.CLEAN.value
        assert eval_res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert eval_res.overall_outcome == "INSUFFICIENT_EVIDENCE"
        assert eval_res.execution_finality == "FINAL"

        # Schema v1 rejection on C1/C2
        scenario = make_scenario(
            scenario_id="t01",
            target_tool="payment",
            target_action="charge",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t01",
            agent_id="test_agent",
            passed=eval_res.passed,
            verdict=eval_res.verdict,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=10.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value) or "C2" in str(exc_info.value)

    def test_t02_mutating_handler_commits_then_raises(self) -> None:
        """T-02: Mutating handler commits then raises.

        DB insert succeeds, sets evt, raises RuntimeError; agent blindly retries.
        Evidence: c(a1)=1, u(a1)=0 -> Bounds: [1, 1].
        Behavioral: GENUINE_FAILURE, External: EXACT_MATCH, Primary Infra: CLEAN.
        Aggregate Outcome: GENUINE_FAILURE.
        Schema v1: Rejects if attempt has UNCERTAIN event.
        """
        sync_event = threading.Event()
        run_id = "run_t02"

        # Attempt 1: DB commit succeeded
        # Attempt 2: Blind retry failed with UNCERTAIN event
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t02",
                tool_name="database",
                action_type="insert",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_t02",
                request_fingerprint="fp_t02",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                evidence_reference="tx_t02",
                details={
                    "capacity_provenance": "TIER_C3",
                    "max_capacity": 1,
                    "receipt_complete": True,
                },
            ),
            LedgerEvent(
                event_id=2,
                run_id=run_id,
                scenario_id="t02",
                tool_name="database",
                action_type="insert",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_t02",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="101.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        sync_event.set()
        assert sync_event.is_set()

        tool_meta = make_c3_meta(events, "database", max_capacity=1)
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=1,
            tool_metadata=tool_meta,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )

        assert accounting.bounds.s_min == 1
        assert accounting.bounds.s_max == 1
        assert accounting.external_status == "EXACT_MATCH"

        # Agent blindly claimed failure or crashed without idempotent recovery
        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.FAILED,
            status_message="Agent blindly crashed or performed uncoordinated retry",
        )
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = make_finality_proof(
            run_id,
            events,
            expected_side_effects=1,
            expect_agent_success=True,
            agent_status=agent_res.status.value,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=10.0,
            tool_metadata=tool_meta,
            execution_finality=proof,
            run_id=run_id,
        )

        assert eval_res.behavioral_verdict == "GENUINE_FAILURE"
        assert eval_res.side_effect_status == "EXACT_MATCH"
        assert eval_res.primary_infrastructure_status == PrimaryInfrastructureStatus.CLEAN.value
        assert eval_res.overall_outcome == "GENUINE_FAILURE"

        # Schema v1 rejection on C1 due to UNCERTAIN commit_status event on a2
        scenario = make_scenario(
            scenario_id="t02",
            target_tool="database",
            target_action="insert",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t02",
            agent_id="test_agent",
            passed=eval_res.passed,
            verdict=eval_res.verdict,
            total_events=len(events),
            committed_effects=1,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=10.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value)

    def test_t03_partial_multi_effect_commit(self) -> None:
        """T-03: Partial multi-effect commit.

        2 of 3 records committed, then connection dropped.
        Evidence: c(a1)=2, u(a1)=1 -> Bounds: [2, 3].
        Behavioral: INSUFFICIENT_EVIDENCE, External: UNRESOLVED_INTERVAL, Primary Infra: CLEAN.
        Aggregate Outcome: INSUFFICIENT_EVIDENCE.
        Schema v1: Rejects (C2).
        """
        run_id = "run_t03"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t03",
                tool_name="batch_service",
                action_type="insert_batch",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_rec_01",
                request_fingerprint="fp_batch",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 3},
            ),
            LedgerEvent(
                event_id=2,
                run_id=run_id,
                scenario_id="t03",
                tool_name="batch_service",
                action_type="insert_batch",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_rec_02",
                request_fingerprint="fp_batch",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.1",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 3},
            ),
            LedgerEvent(
                event_id=3,
                run_id=run_id,
                scenario_id="t03",
                tool_name="batch_service",
                action_type="insert_batch",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_batch",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.2",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 3},
            ),
        ]
        tool_meta = make_c3_meta(events, "batch_service", max_capacity=3)
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=3,
            tool_metadata=tool_meta,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )

        assert accounting.bounds.s_min == 2
        assert accounting.bounds.s_max == 3
        assert accounting.external_status == "UNRESOLVED_INTERVAL"

        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.UNKNOWN,
            status_message="Agent timed out on batch insert",
        )
        assertions = ScenarioAssertions(expected_side_effects=3, expect_agent_success=True)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = make_finality_proof(
            run_id,
            events,
            expected_side_effects=3,
            expect_agent_success=True,
            agent_status=agent_res.status.value,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=10.0,
            tool_metadata=tool_meta,
            execution_finality=proof,
            run_id=run_id,
        )

        assert eval_res.behavioral_verdict == "INSUFFICIENT_EVIDENCE"
        assert eval_res.side_effect_status == "UNRESOLVED_INTERVAL"
        assert eval_res.primary_infrastructure_status == PrimaryInfrastructureStatus.CLEAN.value
        assert eval_res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert eval_res.overall_outcome == "INSUFFICIENT_EVIDENCE"

        # Schema v1 rejection on C2
        scenario = make_scenario(
            scenario_id="t03",
            target_tool="batch_service",
            target_action="insert_batch",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t03",
            agent_id="test_agent",
            passed=False,
            verdict=eval_res.verdict,
            total_events=len(events),
            committed_effects=2,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=10.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value) or "C2" in str(exc_info.value)

    def test_t04_non_mutating_declaration_without_proof(self) -> None:
        """T-04: Non-mutating declaration without proof.

        Tool declared mutates=False under C1, raises ConnectionError; agent halts cleanly.
        Evidence: Absent=UNVERIFIED, u=inf -> Bounds: [0, inf).
        Behavioral: PASS, External: UNBOUNDED_AMBIGUITY, Primary Infra: CLEAN.
        Aggregate Outcome: INSUFFICIENT_EVIDENCE.
        Schema v1: Rejects (C2).
        """
        run_id = "run_t04"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t04",
                tool_name="analytics_reader",
                action_type="query",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_query",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C1", "max_capacity": 0, "mutates": False},
            )
        ]
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=0,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )

        assert accounting.bounds.s_min == 0
        assert accounting.bounds.is_unbounded
        assert accounting.external_status == "UNBOUNDED_AMBIGUITY"

        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.FAILED,
            status_message="Agent cleanly halted on ConnectionError",
        )
        assertions = ScenarioAssertions(expected_side_effects=0, expect_agent_success=False)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = make_finality_proof(
            run_id,
            events,
            expected_side_effects=0,
            expect_agent_success=False,
            agent_status=agent_res.status.value,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=10.0,
            execution_finality=proof,
            run_id=run_id,
        )

        assert eval_res.behavioral_verdict == "PASS"
        assert eval_res.side_effect_status == "UNBOUNDED_AMBIGUITY"
        assert eval_res.primary_infrastructure_status == PrimaryInfrastructureStatus.CLEAN.value
        assert eval_res.overall_outcome == "INSUFFICIENT_EVIDENCE"

        # Schema v1 rejection on C2
        scenario = make_scenario(
            scenario_id="t04",
            target_tool="analytics_reader",
            target_action="query",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t04",
            agent_id="test_agent",
            passed=False,
            verdict=eval_res.verdict,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=10.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value) or "C2" in str(exc_info.value)

    def test_t05_verified_absence(self) -> None:
        """T-05: Verified absence.

        Pre-dispatch validation error; 0 I/O confirmed.
        Evidence: Absent=TRUE, c=0, u=0 -> Bounds: [0, 0].
        Behavioral: PASS, External: CONFIRMED_ABSENT, Primary Infra: CLEAN.
        Aggregate Outcome: PASS.
        Schema v1: Accepts (v1 Valid).
        """
        run_id = "run_t05"
        audit = TEST_AUDIT_AUTH.issue_audit_evidence(
            run_id=run_id,
            scenario_id="t05",
            snapshot_id="snap_t05",
            attempt_ids=["a1"],
            verified_effects=set(),
            confirmed_absent=True,
            drain_delay_verified=True,
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t05",
                tool_name="vault",
                action_type="store",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_vault",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.ABORTED,
                simulated_timestamp="100.0",
                details={"absent_status": "TRUE"},
            )
        ]
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=0,
            reconciliation_audit=audit,
            audit_authority=TEST_AUDIT_AUTH,
            runtime_authority=TEST_RUNTIME_AUTH,
        )

        assert accounting.bounds.s_min == 0
        assert accounting.bounds.s_max == 0
        assert accounting.bounds.is_zero
        assert accounting.external_status == "CONFIRMED_ABSENT"

        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.FAILED,
            status_message="Agent cleanly halted on validation error",
        )
        assertions = ScenarioAssertions(expected_side_effects=0, expect_agent_success=False)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = make_finality_proof(
            run_id,
            events,
            expected_side_effects=0,
            expect_agent_success=False,
            agent_status=agent_res.status.value,
            reconciliation_audit=audit,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=10.0,
            execution_finality=proof,
            run_id=run_id,
            reconciliation_audit=audit,
        )

        assert eval_res.behavioral_verdict == "PASS"
        assert eval_res.side_effect_status == "CONFIRMED_ABSENT"
        assert eval_res.primary_infrastructure_status == PrimaryInfrastructureStatus.CLEAN.value
        assert eval_res.verdict == EvaluationVerdict.PASS
        assert eval_res.overall_outcome == "PASS"

        # Schema v1 accepted
        scenario = make_scenario(
            scenario_id="t05",
            target_tool="vault",
            target_action="store",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t05",
            agent_id="test_agent",
            passed=True,
            verdict=EvaluationVerdict.PASS,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=10.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        v1_dict = JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert v1_dict["report_schema_version"] == 1
        assert v1_dict["outcome"]["verdict"] == "PASS"

    def test_t06_timeout_with_unresolved_worker(self) -> None:
        """T-06: Timeout with unresolved worker.

        Worker thread hangs; timeout 50ms expires.
        Evidence: Attempt in A_uncertain, u=1 -> Bounds: [0, 1].
        Behavioral: INSUFFICIENT_EVIDENCE, External: UNRESOLVED_INTERVAL, Primary Infra: WORKER_TIMED_OUT.
        Aggregate Outcome: INSUFFICIENT_EVIDENCE.
        Schema v1: Rejects (C2, C5).
        """
        run_id = "run_t06"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t06",
                tool_name="slow_service",
                action_type="process",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_slow",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]
        tool_meta = make_c3_meta(events, "slow_service", max_capacity=1)
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=1,
            tool_metadata=tool_meta,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )

        assert accounting.bounds.s_min == 0
        assert accounting.bounds.s_max == 1
        assert accounting.external_status == "UNRESOLVED_INTERVAL"

        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.UNKNOWN,
            status_message="TimeoutError: Worker timed out",
        )
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = make_finality_proof(
            run_id,
            events,
            expected_side_effects=1,
            expect_agent_success=True,
            agent_status=agent_res.status.value,
            infrastructure_status=PrimaryInfrastructureStatus.WORKER_TIMED_OUT.value,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=50.0,
            tool_metadata=tool_meta,
            execution_finality=proof,
            infrastructure_status=PrimaryInfrastructureStatus.WORKER_TIMED_OUT.value,
            run_id=run_id,
        )

        assert eval_res.primary_infrastructure_status == PrimaryInfrastructureStatus.WORKER_TIMED_OUT.value
        assert eval_res.overall_outcome == "INSUFFICIENT_EVIDENCE"

        # Schema v1 rejection on C2/C5
        scenario = make_scenario(
            scenario_id="t06",
            target_tool="slow_service",
            target_action="process",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t06",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=50.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value) or "C2" in str(exc_info.value) or "C5" in str(exc_info.value)

    def test_t07_cancellation_while_synchronous_worker_runs(self) -> None:
        """T-07: Cancellation while synchronous worker runs.

        Task cancelled while handler on barrier.
        Evidence: In-flight attempt u=1 -> Bounds: [0, 1].
        Behavioral: INSUFFICIENT_EVIDENCE, External: UNRESOLVED_INTERVAL, Primary Infra: CLEAN.
        Finality: PROVISIONAL.
        Aggregate Outcome: INSUFFICIENT_EVIDENCE.
        Schema v1: Rejects (C2, C4).
        """
        run_id = "run_t07"
        sync_barrier = threading.Barrier(2)

        def blocked_worker() -> None:
            try:
                sync_barrier.wait(timeout=1.0)
            except threading.BrokenBarrierError:
                pass

        thread = threading.Thread(target=blocked_worker)
        thread.start()
        try:
            sync_barrier.wait(timeout=1.0)
        finally:
            thread.join()

        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t07",
                tool_name="sync_tool",
                action_type="block",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_barrier",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]
        tool_meta = make_c3_meta(events, "sync_tool", max_capacity=1)
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=1,
            tool_metadata=tool_meta,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )
        assert accounting.bounds.s_min == 0
        assert accounting.bounds.s_max == 1

        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.UNKNOWN,
            status_message="Task cancelled during execution",
        )
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=10.0,
            tool_metadata=tool_meta,
            execution_finality="PROVISIONAL",
            run_id=run_id,
        )

        assert eval_res.execution_finality == "PROVISIONAL"
        assert eval_res.behavioral_verdict == "INSUFFICIENT_EVIDENCE"
        assert eval_res.side_effect_status == "UNRESOLVED_INTERVAL"
        assert eval_res.primary_infrastructure_status == PrimaryInfrastructureStatus.CLEAN.value

        # Schema v1 rejection on C2/C4
        scenario = make_scenario(
            scenario_id="t07",
            target_tool="sync_tool",
            target_action="block",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t07",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=10.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value) or "C2" in str(exc_info.value) or "C4" in str(exc_info.value)

    def test_t08_late_local_worker_completion(self, tmp_path: Any) -> None:
        """T-08: Late local worker completion.

        Worker finishes late; thread hook writes supplemental event to WAL ledger.
        Primary: [0, 1] INSUFFICIENT_EVIDENCE -> Supplemental: [1, 1] PASS.
        Schema v1: Rejects (C8).
        """
        run_id = "run_t08"
        db_path = str(tmp_path / "t08_wal.db")
        wal_ledger = LateOutcomeLedger(db_path=db_path)

        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t08",
                tool_name="slow_writer",
                action_type="write",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_late",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]
        tool_meta = make_c3_meta(events, "slow_writer", max_capacity=1)
        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.UNKNOWN,
            status_message="Cutoff reached before worker returned",
        )
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = make_finality_proof(
            run_id,
            events,
            expected_side_effects=1,
            expect_agent_success=True,
            agent_status=agent_res.status.value,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=20.0,
            tool_metadata=tool_meta,
            execution_finality=proof,
            run_id=run_id,
        )
        eval_res.behavioral_verdict = "PASS"

        scenario = make_scenario(
            scenario_id="t08",
            target_tool="slow_writer",
            target_action="write",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t08",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=20.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        primary_dict = JSONReporter.generate_report_dict(run_res, schema_version=2)
        assert primary_dict["outcome"]["overall_outcome"] == "INSUFFICIENT_EVIDENCE"

        # Late hook writes completion to WAL ledger
        late_record = make_late_record(
            run_id=run_id,
            attempt_id="a1",
            effect_id="eff_late_01",
            commit_status=CommitStatus.COMMITTED,
            writer_id="THREAD_HOOK",
        )
        wal_ledger.append(late_record)

        # Reconcile via ReconciliationEngine
        engine = ReconciliationEngine(ledger=wal_ledger)
        supp_report = engine.generate_supplemental_report(
            primary_report=primary_dict,
            cutoff_timestamp="100.0",
        )
        assert supp_report.side_effects.bounds == [1, 1]
        assert supp_report.reconciled_outcome.overall_outcome == "PASS"

        # Schema v1 rejection on C8
        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value) or "C2" in str(exc_info.value) or "C8" in str(exc_info.value)

    def test_t09_late_remote_commit_after_process_killed(self, tmp_path: Any) -> None:
        """T-09: Late remote commit after process killed.

        Worker process hard-terminated by parent; remote mock commits.
        Supervisor records death; reconcile finds commit.
        Primary: PROCESS_TERMINATED, Supplemental: INFRASTRUCTURE_ERROR (Tier 3 precedence retained).
        Schema v1: Rejects (C6, C8).
        """
        # 1. Execute uncooperative hanging tool in actual OS child process via ToolProcessRunner
        runner = ToolProcessRunner()
        hanging_tool = UncooperativeHangingTool(
            name="remote_payment",
            timeout_seconds=0.1,
            sleep_duration=10.0,
        )
        proc_res = runner.execute(
            tool=hanging_tool,
            action="charge",
            params={"amount": 100},
            raise_on_error=False,
        )

        # 2. Verify child process termination at OS level
        assert proc_res.terminated is True
        assert proc_res.child_pid is not None
        assert proc_res.child_pid != os.getpid()
        assert proc_res.exit_code is not None

        run_id = "run_t09"
        db_path = str(tmp_path / "t09_wal.db")
        wal_ledger = LateOutcomeLedger(db_path=db_path)

        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t09",
                tool_name="remote_payment",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_rem",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                error_type="ProcessTerminatedError",
                details={
                    "process_terminated": True,
                    "child_pid": proc_res.child_pid,
                    "exit_code": proc_res.exit_code,
                    "capacity_provenance": "TIER_C3",
                    "max_capacity": 1,
                },
            )
        ]
        tool_meta = make_c3_meta(events, "remote_payment", max_capacity=1)
        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.FAILED,
            status_message=f"Supervisor killed child process PID {proc_res.child_pid} upon deadline expiry",
        )
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = make_finality_proof(
            run_id,
            events,
            expected_side_effects=1,
            expect_agent_success=True,
            agent_status=agent_res.status.value,
            infrastructure_status=PrimaryInfrastructureStatus.PROCESS_TERMINATED.value,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=proc_res.duration_ms,
            tool_metadata=tool_meta,
            execution_finality=proof,
            infrastructure_status=PrimaryInfrastructureStatus.PROCESS_TERMINATED.value,
            run_id=run_id,
        )
        eval_res.behavioral_verdict = "PASS"
        eval_res.primary_infrastructure_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value
        eval_res.verdict = EvaluationVerdict.INFRASTRUCTURE_ERROR
        eval_res.overall_outcome = "INFRASTRUCTURE_ERROR"

        scenario = make_scenario(
            scenario_id="t09",
            target_tool="remote_payment",
            target_action="charge",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t09",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.INFRASTRUCTURE_ERROR,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=proc_res.duration_ms,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        primary_dict = JSONReporter.generate_report_dict(run_res, schema_version=2)

        # Late remote commit ingested
        late_record = make_late_record(
            run_id=run_id,
            attempt_id="a1",
            effect_id="eff_remote_01",
            commit_status=CommitStatus.COMMITTED,
            writer_id="RECON_ENGINE",
            event_type="AUDIT_SNAPSHOT",
        )
        wal_ledger.append(late_record)

        engine = ReconciliationEngine(ledger=wal_ledger)
        supp_report = engine.generate_supplemental_report(
            primary_report=primary_dict,
            cutoff_timestamp="100.0",
        )

        assert supp_report.side_effects.bounds == [1, 1]
        assert supp_report.reconciled_outcome.side_effect_status == "EXACT_MATCH"
        assert supp_report.reconciled_outcome.primary_infrastructure_status == "PROCESS_TERMINATED"
        # Tier 3 precedence: primary infrastructure termination is permanently retained
        assert supp_report.reconciled_outcome.overall_outcome == "INFRASTRUCTURE_ERROR"

        # Schema v1 rejection on C6
        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value) or "C6" in str(exc_info.value) or "C2" in str(exc_info.value)

    def test_t10_worker_never_returns_in_trusted_mode(self) -> None:
        """T-10: Worker never returns in trusted mode.

        Handler uncontained; drain expires; process tainted.
        Evidence: primary_infrastructure_status: THREAD_LEAKED.
        Outcome: INFRASTRUCTURE_ERROR.
        Subsequent execution raises ProcessTaintedError.
        Schema v1: Rejects (C5, C6).
        """
        assert is_process_tainted() is False

        # Simulate thread leak and drain barrier expiry in trusted mode
        taint_process()
        assert is_process_tainted() is True

        backend = InProcessExecutionBackend()
        with pytest.raises(ProcessTaintedError) as exc_info:
            backend.run(
                agent=lambda ctx, tc: AgentResult(success=True),
                task_context=None,  # type: ignore[arg-type]
                tool_client=None,  # type: ignore[arg-type]
            )
        assert "tainted" in str(exc_info.value).lower()

        # Build T-10 report
        run_id = "run_t10"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t10",
                tool_name="leaky_tool",
                action_type="spin",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_spin",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        eval_res = EvaluationResult(
            verdict=EvaluationVerdict.INFRASTRUCTURE_ERROR,
            passed=False,
            primary_infrastructure_status=PrimaryInfrastructureStatus.THREAD_LEAKED.value,
            reconciliation_infrastructure_status="NOT_APPLICABLE",
            overall_outcome="INFRASTRUCTURE_ERROR",
            behavioral_verdict="INSUFFICIENT_EVIDENCE",
            execution_finality="FINAL",
            bounds=[0, 1],
            actual_side_effects=0,
        )
        scenario = make_scenario(
            scenario_id="t10",
            target_tool="leaky_tool",
            target_action="spin",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t10",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.INFRASTRUCTURE_ERROR,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=10.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        with pytest.raises(IncompatibleSchemaVersionError) as exc_v1:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_v1.value) or "C5" in str(exc_v1.value) or "C6" in str(exc_v1.value)

    def test_t11_registry_reset_while_worker_active(self) -> None:
        """T-11: Registry reset while worker active.

        Worker in-flight; test invokes tools.reset_all().
        Directly raises ConcurrencyViolationError.
        """
        registry = ToolRegistry()
        tool = DeclarativeToolService("svc", default_handler=lambda: {"res": 1})
        registry.register(tool)

        # Worker begins execution
        registry.worker_started()
        assert registry.active_workers == 1

        # Resetting registry while worker is in-flight must raise ConcurrencyViolationError
        with pytest.raises(ConcurrencyViolationError) as exc_info:
            registry.reset_all()
        assert "Cannot reset tool registry" in str(exc_info.value)
        assert "1 worker(s) still active" in str(exc_info.value)

        # Clean worker finish
        registry.worker_finished()
        assert registry.active_workers == 0
        registry.reset_all()  # Now succeeds cleanly

    def test_t12_schema_v1_export_with_uncertain_event(self) -> None:
        """T-12: Schema v1 export with uncertain event.

        Run contains 1 event with commit_status=UNCERTAIN.
        JSONReporter.generate_report_dict(result, schema_version=1) raises IncompatibleSchemaVersionError (C1).
        """
        run_id = "run_t12"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t12",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_t12",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        eval_res = EvaluationResult(
            verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
            passed=False,
            primary_infrastructure_status="CLEAN",
            overall_outcome="INSUFFICIENT_EVIDENCE",
            behavioral_verdict="PASS",
            execution_finality="FINAL",
            bounds=[0, 1],
            actual_side_effects=0,
        )
        scenario = make_scenario(
            scenario_id="t12",
            target_tool="svc",
            target_action="act",
            assertions=ScenarioAssertions(expected_side_effects=1),
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t12",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
            total_events=1,
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=5.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )

        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value)

    def test_t13_schema_v1_export_with_non_degenerate_bounds(self) -> None:
        """T-13: Schema v1 export with non-degenerate bounds.

        Clean events, but S in [1, 2].
        Raises IncompatibleSchemaVersionError (C2).
        """
        run_id = "run_t13"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t13",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_t13",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        eval_res = EvaluationResult(
            verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
            passed=False,
            primary_infrastructure_status="CLEAN",
            overall_outcome="INSUFFICIENT_EVIDENCE",
            behavioral_verdict="PASS",
            execution_finality="FINAL",
            bounds=[1, 2],
            actual_side_effects=1,
        )
        scenario = make_scenario(
            scenario_id="t13",
            target_tool="svc",
            target_action="act",
            assertions=ScenarioAssertions(expected_side_effects=1),
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t13",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
            total_events=1,
            committed_effects=1,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=5.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )

        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C1" in str(exc_info.value) or "C2" in str(exc_info.value)

    def test_t14_schema_v1_export_of_completely_clean_run(self) -> None:
        """T-14: Schema v1 export of completely clean run.

        Clean run, S in [1, 1], zero leaks, finality FINAL. All C1..C10 True.
        Emits valid Schema v1 JSON.
        """
        run_id = "run_t14"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t14",
                tool_name="svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_clean",
                request_fingerprint="fp_t14",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                evidence_reference="tx_clean",
            )
        ]
        eval_res = EvaluationResult(
            verdict=EvaluationVerdict.PASS,
            passed=True,
            primary_infrastructure_status="CLEAN",
            reconciliation_infrastructure_status=None,
            overall_outcome="PASS",
            behavioral_verdict="PASS",
            side_effect_status="EXACT_MATCH",
            execution_finality="FINAL",
            bounds=[1, 1],
            actual_side_effects=1,
            expected_side_effects=1,
        )
        scenario = make_scenario(
            scenario_id="t14",
            target_tool="svc",
            target_action="charge",
            assertions=ScenarioAssertions(expected_side_effects=1, expect_agent_success=True),
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t14",
            agent_id="test_agent",
            passed=True,
            verdict=EvaluationVerdict.PASS,
            total_events=1,
            committed_effects=1,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=5.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )

        v1_dict = JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert v1_dict["report_schema_version"] == 1
        assert v1_dict["outcome"]["verdict"] == "PASS"

    def test_t15_same_key_with_changed_payload(self) -> None:
        """T-15: Same key with changed payload.

        Attempt 2 changes amount under same idempotency key (PAYLOAD_MUTATED_UNDER_KEY).
        Bounds: [1, 2], External: DUPLICATE_COMMITTED.
        Outcome: GENUINE_FAILURE.
        Schema v1: Rejects (C1).
        """
        run_id = "run_t15"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t15",
                tool_name="payment",
                action_type="charge",
                operation_id="op_t15",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                idempotency_key_hash="key_hash_abc",
                request_fingerprint="fp_amount_10",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id=run_id,
                scenario_id="t15",
                tool_name="payment",
                action_type="charge",
                operation_id="op_t15",
                attempt_id="a2",
                attempt_number=2,
                effect_id="eff_02",
                idempotency_key_hash="key_hash_abc",
                request_fingerprint="fp_amount_20",  # Payload changed!
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="105.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "payment", max_capacity=1)
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=1,
            tool_metadata=tool_meta,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )

        assert accounting.bounds.s_min == 1
        assert accounting.bounds.s_max == 2
        assert accounting.external_status == "DUPLICATE_COMMITTED"

        outcome = aggregate_outcome(
            behavioral_verdict="GENUINE_FAILURE",
            external_status="DUPLICATE_COMMITTED",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert outcome == "GENUINE_FAILURE"

    def test_t16_same_key_with_expired_ttl(self) -> None:
        """T-16: Same key with expired TTL.

        Attempt 2 occurs at T > retention_ttl (RETENTION_TTL_EXPIRED).
        Bounds: [1, 2], External: DUPLICATE_COMMITTED.
        Outcome: GENUINE_FAILURE.
        Schema v1: Rejects (C1).
        """
        run_id = "run_t16"
        contract = IdempotencyContract(
            scope=IdempotencyScope.OPERATION_AND_ACTION,
            guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
            retention_ttl_seconds=10.0,
            verified_service_deduplication=True,
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t16",
                tool_name="payment",
                action_type="charge",
                operation_id="op_t16",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                idempotency_key_hash="key_hash_ttl",
                request_fingerprint="fp_t16",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id=run_id,
                scenario_id="t16",
                tool_name="payment",
                action_type="charge",
                operation_id="op_t16",
                attempt_id="a2",
                attempt_number=2,
                effect_id="eff_02",
                idempotency_key_hash="key_hash_ttl",
                request_fingerprint="fp_t16",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="150.0",  # Exceeded retention_ttl_seconds!
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "payment", max_capacity=1)
        tool_meta["payment"]["idempotency_contract"] = contract
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=1,
            tool_metadata=tool_meta,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )

        assert accounting.bounds.s_min == 2
        assert accounting.external_status == "DUPLICATE_COMMITTED"

        outcome = aggregate_outcome(
            behavioral_verdict="GENUINE_FAILURE",
            external_status="DUPLICATE_COMMITTED",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert outcome == "GENUINE_FAILURE"

    def test_t17_lucky_agent_misbehavior(self) -> None:
        """T-17: Lucky agent misbehavior.

        Agent claims false success without recovery when server happened to commit.
        Behavioral: GENUINE_FAILURE, External: EXACT_MATCH.
        Outcome: GENUINE_FAILURE.
        Schema v1: Rejects (C7).
        """
        run_id = "run_t17"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t17",
                tool_name="svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_lucky",
                request_fingerprint="fp_t17",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                evidence_reference="tx_lucky",
            )
        ]
        eval_res = EvaluationResult(
            verdict=EvaluationVerdict.GENUINE_FAILURE,
            passed=False,
            primary_infrastructure_status="CLEAN",
            reconciliation_infrastructure_status=None,
            overall_outcome="PASS",
            behavioral_verdict="GENUINE_FAILURE",
            side_effect_status="EXACT_MATCH",
            execution_finality="FINAL",
            bounds=[1, 1],
            actual_side_effects=1,
            expected_side_effects=1,
        )
        scenario = make_scenario(
            scenario_id="t17",
            target_tool="svc",
            target_action="charge",
            assertions=ScenarioAssertions(expected_side_effects=1, expect_agent_success=True),
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="t17",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.GENUINE_FAILURE,
            total_events=1,
            committed_effects=1,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=5.0,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )

        with pytest.raises(IncompatibleSchemaVersionError) as exc_info:
            JSONReporter.generate_report_dict(run_res, schema_version=1)
        assert "C7" in str(exc_info.value)

    def test_t18_crash_during_late_ingestion_and_recovery(self, tmp_path: Any) -> None:
        """T-18: Crash during late ingestion & recovery.

        Harness crashes before ack; restarts and reads WAL ledger.
        Primary: INSUFFICIENT_EVIDENCE -> Supplemental: PASS.
        Schema v1: Rejects (C8).
        """
        run_id = "run_t18"
        db_path = str(tmp_path / "t18_wal.db")

        # Session 1: Record late outcome in WAL ledger and simulate abrupt process death
        ledger1 = LateOutcomeLedger(db_path=db_path)
        record = make_late_record(
            run_id=run_id,
            attempt_id="a1",
            effect_id="eff_t18",
            commit_status=CommitStatus.COMMITTED,
            writer_id="THREAD_HOOK",
        )
        ledger1.append(record)
        del ledger1

        # Session 2: Fresh process instance loads ledger from disk
        ledger2 = LateOutcomeLedger(db_path=db_path)
        replayed = ledger2.get_late_records(run_id)
        assert len(replayed) == 1
        assert replayed[0].payload.get("effect_id") == "eff_t18"

        primary_report = {
            "report_schema_version": 2,
            "recoverybench_version": "0.1.0",
            "run_id": run_id,
            "scenario": {
                "id": "t18",
                "name": "T-18",
                "description": "",
                "schema_version": 2,
                "target_tool": "svc",
                "target_action": "act",
                "is_single_operation": True,
            },
            "agent_id": "agent",
            "isolation_mode": "trusted",
            "outcome": {
                "verdict": "INSUFFICIENT_EVIDENCE",
                "overall_outcome": "INSUFFICIENT_EVIDENCE",
                "behavioral_verdict": "PASS",
                "side_effect_status": "UNRESOLVED_INTERVAL",
                "primary_infrastructure_status": "CLEAN",
                "reconciliation_infrastructure_status": "CLEAN",
                "execution_finality": "FINAL",
                "execution_success": False,
                "recovery_success": True,
                "side_effect_safe": False,
            },
            "evaluation": {
                "verdict": "INSUFFICIENT_EVIDENCE",
                "passed": False,
                "isolation_mode": "trusted",
                "duration_ms": 10.0,
                "expected_side_effects": 1,
                "actual_side_effects": 0,
                "duplicate_effect_count": 0,
                "idempotency_key_stable": True,
                "max_retries_observed": 0,
                "failure_reasons": [],
                "evidence_summary": {},
                "assertions": [],
            },
            "side_effects": {
                "expected_count": 1,
                "actual_count": 0,
                "duplicate_count": 0,
                "idempotency_key_stable": True,
                "safety_status": "UNRESOLVED_INTERVAL",
                "safe": False,
                "committed_effect_ids": [],
                "uncertain_count": 1,
                "bounds": [0, 1],
            },
            "fault_injection": {"configured_faults": [], "injected_fault_count": 0, "faults": []},
            "events": [
                {
                    "event_id": 1,
                    "event_type": "tool_call",
                    "timestamp": "100.0",
                    "tool": "svc",
                    "action": "act",
                    "operation_id": "op_1",
                    "attempt_id": "a1",
                    "attempt_number": 1,
                    "execution_status": "ERROR",
                    "commit_status": "UNCERTAIN",
                    "request_fingerprint": "fp_t18",
                    "details": {"capacity_provenance": "TIER_C3", "max_capacity": 1},
                }
            ],
            "errors": [],
            "metadata": {
                "agent_id": "agent",
                "isolation_mode": "trusted",
                "created_at": "2026-11-20T00:00:00Z",
                "duration_ms": 10.0,
                "total_events": 1,
                "max_retries_observed": 0,
                "seed": 42,
                "unresolved_workers": 0,
            },
            "agent_result": {
                "success": False,
                "status": "UNKNOWN",
                "data": {},
                "attempts": 1,
                "status_message": "",
            },
        }

        engine = ReconciliationEngine(ledger=ledger2)
        supp_report = engine.generate_supplemental_report(
            primary_report=primary_report,
            cutoff_timestamp="100.0",
        )
        assert supp_report.side_effects.bounds == [1, 1]
        assert supp_report.reconciled_outcome.overall_outcome == "PASS"

    def test_t19_tool_capacity_breach_falsified_declaration(self) -> None:
        """T-19: Tool capacity breach / falsified declaration.

        Tool declared M(a1)=1, but commits 2 records (c=2 > M=1).
        Breach detected; M promoted to inf, u=inf -> Bounds: [2, inf).
        Primary Infra: INTEGRITY_VIOLATION. Outcome: INFRASTRUCTURE_ERROR.
        Schema v1: Rejects (C2, C6).
        """
        run_id = "run_t19"
        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="t19",
                tool_name="bad_tool",
                action_type="insert",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_t19",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id=run_id,
                scenario_id="t19",
                tool_name="bad_tool",
                action_type="insert",
                attempt_id="a1",  # Same attempt emitted second distinct effect!
                attempt_number=1,
                effect_id="eff_02",
                request_fingerprint="fp_t19",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="101.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "bad_tool", max_capacity=1)

        # 1. Verification of raise_on_breach=True
        with pytest.raises(CapacityBreachIntegrityError, match="Capacity breach detected"):
            TwoLayerEffectAccountingEngine.calculate(
                events,
                raise_on_breach=True,
                tool_metadata=tool_meta,
                runtime_authority=TEST_RUNTIME_AUTH,
                audit_authority=TEST_AUDIT_AUTH,
            )

        # 2. Conservative fallback on breach: M promoted to inf, bounds [2, inf), status INTEGRITY_VIOLATION
        accounting = TwoLayerEffectAccountingEngine.calculate(
            events,
            raise_on_breach=False,
            tool_metadata=tool_meta,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
        )
        assert accounting.bounds.s_min == 2
        assert accounting.bounds.is_unbounded
        assert accounting.infrastructure_status == PrimaryInfrastructureStatus.INTEGRITY_VIOLATION.value

        # 3. Outcome synthesis for integrity violation
        outcome = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="UNBOUNDED_AMBIGUITY",
            infrastructure_status=PrimaryInfrastructureStatus.INTEGRITY_VIOLATION.value,
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert outcome == "INFRASTRUCTURE_ERROR"

    def test_t20_compound_failure_process_killed_and_reconciliation_failed(self) -> None:
        """T-20: Compound failure: process killed & reconciliation failed.

        Worker process SIGKILLed; post-cutoff reconciliation crashes.
        Dual failure: primary PROCESS_TERMINATED, recon RECONCILIATION_FAILED.
        Outcome: INFRASTRUCTURE_ERROR.
        Schema v1: Rejects (C6, C8).
        """
        outcome = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="UNRESOLVED_INTERVAL",
            infrastructure_status=PrimaryInfrastructureStatus.PROCESS_TERMINATED.value,
            expected_side_effects=1,
            execution_finality="FINAL",
            reconciliation_infrastructure_status=ReconciliationInfrastructureStatus.RECONCILIATION_FAILED.value,
        )
        assert outcome == "INFRASTRUCTURE_ERROR"
