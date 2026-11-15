"""Unit tests for the Recovery Evaluator, deterministic assertions, and ledger evidence."""

from recoverybench.agent.base import AgentResult
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.models import (
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
    LedgerEvent,
)
from recoverybench.scenarios.schema import ScenarioAssertions


def test_evaluator_pass_single_commit() -> None:
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
            run_id="run_1",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_1",
            attempt_id="att_1",
            effect_id="eff_ch_sim_0001",
            idempotency_key_hash="hash_stable_1234",
            request_fingerprint="fp_abc",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:00Z",
            evidence_reference="ch_sim_0001",
        ),
        # Attempt 2: Deduplicated replay
        LedgerEvent(
            event_id=2,
            run_id="run_1",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_1",
            attempt_id="att_2",
            effect_id="eff_ch_sim_0001",
            idempotency_key_hash="hash_stable_1234",
            request_fingerprint="fp_abc",
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

    # Check evidence presence
    assert len(evaluation.evidence_summary) >= 4
    for a in evaluation.assertions:
        assert len(a.evidence) > 0


def test_evaluator_detects_duplicate_side_effect() -> None:
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(
        expected_side_effects=1,
        max_duplicate_effects=0,
        require_stable_idempotency_key=True,
    )

    events = [
        # Attempt 1: Committed
        LedgerEvent(
            event_id=1,
            run_id="run_1",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_1",
            attempt_id="att_1",
            effect_id="eff_ch_sim_0001",
            idempotency_key_hash="hash_key_1",
            request_fingerprint="fp_abc",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:00Z",
            evidence_reference="ch_sim_0001",
        ),
        # Attempt 2: Second commit with different key (Double Charge!)
        LedgerEvent(
            event_id=2,
            run_id="run_1",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_1",
            attempt_id="att_2",
            effect_id="eff_ch_sim_0002",
            idempotency_key_hash="hash_key_2",
            request_fingerprint="fp_abc",
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
    assert any("Duplicate side-effects detected" in r for r in evaluation.failure_reasons)

    # Check that duplicate evidence specifically cites both events and references
    side_effect_assertion = next(a for a in evaluation.assertions if a.name == "side_effect_count")
    assert any("ch_sim_0001" in ev for ev in side_effect_assertion.evidence)
    assert any("ch_sim_0002" in ev for ev in side_effect_assertion.evidence)


def test_evaluator_detects_missing_commit() -> None:
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(expected_side_effects=1)

    events = [
        LedgerEvent(
            event_id=1,
            run_id="run_1",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            request_fingerprint="fp_abc",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.NOT_APPLICABLE,
            simulated_timestamp="2026-01-01T00:00:00Z",
        )
    ]
    agent_result = AgentResult(success=False, error="Timed out", attempts=1)
    evaluation = evaluator.evaluate(events, assertions, agent_result)

    assert evaluation.passed is False
    assert evaluation.actual_side_effects == 0
    assert any("Missing side-effect" in r for r in evaluation.failure_reasons)

    missing_assertion = next(a for a in evaluation.assertions if a.name == "side_effect_count")
    assert any("Expected 1 committed effects, but only 0 were committed" in ev for ev in missing_assertion.evidence)


def test_evaluator_detects_false_success() -> None:
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)

    events = [
        LedgerEvent(
            event_id=1,
            run_id="run_1",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            request_fingerprint="fp_abc",
            attempt_number=1,
            execution_status=ExecutionStatus.ERROR,
            commit_status=CommitStatus.NOT_APPLICABLE,
            simulated_timestamp="2026-01-01T00:00:00Z",
        )
    ]
    # Agent hallucinated or reported success when 0 mutations committed!
    agent_result = AgentResult(success=True, attempts=1, status_message="Fake Success")
    evaluation = evaluator.evaluate(events, assertions, agent_result)

    assert evaluation.passed is False
    assert any("False-success detected" in r for r in evaluation.failure_reasons)

    truth_assertion = next(a for a in evaluation.assertions if a.name == "agent_completion_truth")
    assert any("DISCREPANCY" in ev for ev in truth_assertion.evidence)


def test_evaluator_unknown_outcome_handling() -> None:
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(expected_side_effects=1)

    # 1. Safe handling under unknown outcome:
    events_safe = [
        LedgerEvent(
            event_id=1,
            run_id="run_safe",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_safe",
            attempt_id="att_1",
            effect_id="eff_safe_1",
            idempotency_key_hash="hash_stable",
            request_fingerprint="fp_1",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.COMMITTED,
            error_type="ToolTimeoutError",
            simulated_timestamp="2026-01-01T00:00:00Z",
        ),
        LedgerEvent(
            event_id=2,
            run_id="run_safe",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_safe",
            attempt_id="att_2",
            effect_id="eff_safe_1",
            idempotency_key_hash="hash_stable",
            request_fingerprint="fp_1",
            attempt_number=2,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.NOT_APPLICABLE,
            simulated_timestamp="2026-01-01T00:00:01Z",
        ),
    ]
    eval_safe = evaluator.evaluate(events_safe, assertions, AgentResult(success=True, attempts=2))
    unknown_safe = next(a for a in eval_safe.assertions if a.name == "unknown_outcome_handling")
    assert unknown_safe.passed is True
    assert "reconciled" in unknown_safe.message.lower()

    # 2. Unsafe handling under unknown outcome (blind double commit):
    events_unsafe = [
        LedgerEvent(
            event_id=1,
            run_id="run_unsafe",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_unsafe",
            attempt_id="att_1",
            effect_id="eff_unsafe_1",
            idempotency_key_hash="hash_1",
            request_fingerprint="fp_1",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.COMMITTED,
            error_type="ToolTimeoutError",
            simulated_timestamp="2026-01-01T00:00:00Z",
        ),
        LedgerEvent(
            event_id=2,
            run_id="run_unsafe",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_unsafe",
            attempt_id="att_2",
            effect_id="eff_unsafe_2",
            idempotency_key_hash="hash_2",
            request_fingerprint="fp_1",
            attempt_number=2,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:01Z",
        ),
    ]
    eval_unsafe = evaluator.evaluate(events_unsafe, assertions, AgentResult(success=True, attempts=2))
    unknown_unsafe = next(a for a in eval_unsafe.assertions if a.name == "unknown_outcome_handling")
    assert unknown_unsafe.passed is False
    assert "Unsafe unknown outcome handling" in unknown_unsafe.message


def test_evaluator_retry_bound_verification() -> None:
    evaluator = RecoveryEvaluator()
    # Limit max allowed retries to 1 (meaning max 2 total attempts)
    assertions = ScenarioAssertions(max_allowed_retries=1)

    events = [
        LedgerEvent(
            event_id=1,
            run_id="run_retry",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_retry",
            attempt_id="att_1",
            request_fingerprint="fp_1",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.NOT_APPLICABLE,
            simulated_timestamp="2026-01-01T00:00:00Z",
        ),
        LedgerEvent(
            event_id=2,
            run_id="run_retry",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_retry",
            attempt_id="att_2",
            request_fingerprint="fp_1",
            attempt_number=2,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.NOT_APPLICABLE,
            simulated_timestamp="2026-01-01T00:00:01Z",
        ),
        LedgerEvent(
            event_id=3,
            run_id="run_retry",
            scenario_id="scen_1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_retry",
            attempt_id="att_3",
            effect_id="eff_retry_1",
            request_fingerprint="fp_1",
            attempt_number=3,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2026-01-01T00:00:02Z",
        ),
    ]
    # Total attempts = 3, so retries = 2. But max_allowed = 1!
    res = evaluator.evaluate(events, assertions, AgentResult(success=True, attempts=3))
    assert res.passed is False
    retry_assertion = next(a for a in res.assertions if a.name == "retry_budget")
    assert retry_assertion.passed is False
    assert any("Exceeded retry threshold" in r for r in res.failure_reasons)
