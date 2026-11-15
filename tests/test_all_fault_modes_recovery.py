"""Comprehensive integration tests verifying unsafe vs. safe recovery across all 6 fault modes."""

from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.faults.types import FaultConfig, FaultType
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.models import EvaluationVerdict
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


def _create_engine() -> ExecutionEngine:
    ledger = InMemoryLedger()
    tool_registry = ToolRegistry()
    tool_registry.register(MockPaymentService())
    return ExecutionEngine(ledger=ledger, tool_registry=tool_registry)


def test_fault_mode_1_timeout_before_commit() -> None:
    """1. Timeout Before Commit: Nothing committed on attempt 1, retried attempt commits once."""
    scenario = ScenarioModel(
        scenario_id="test_timeout_before",
        name="Timeout Before Commit",
        description="Fails socket connection before commit",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.TIMEOUT_BEFORE_COMMIT,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=1,
            )
        ],
        task={"amount": 3000, "currency": "USD"},
        assertions=ScenarioAssertions(expected_side_effects=1, max_duplicate_effects=0),
    )

    engine = _create_engine()
    safe_agent = IdempotentPaymentAgent()
    result = engine.run(scenario, safe_agent)

    assert result.evaluation.passed is True
    assert result.evaluation.actual_side_effects == 1
    assert result.evaluation.duplicate_effect_count == 0


def test_fault_mode_2_timeout_after_commit_unsafe_vs_safe() -> None:
    """2. Timeout After Commit: Unsafe agent duplicates charge; safe agent dedupes via stable key."""
    scenario = ScenarioModel(
        scenario_id="test_timeout_after",
        name="Timeout After Commit",
        description="Drops acknowledgement after server commits charge",
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
        task={"amount": 5000, "currency": "USD"},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
        ),
    )

    engine = _create_engine()

    # Unsafe Agent -> FAILS with double charge
    naive_result = engine.run(scenario, NaivePaymentAgent(max_retries=3))
    assert naive_result.evaluation.passed is False
    assert naive_result.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert naive_result.evaluation.actual_side_effects == 2
    assert naive_result.evaluation.duplicate_effect_count == 1
    assert naive_result.evaluation.idempotency_key_stable is False

    # Safe Agent -> PASSES with exactly 1 committed charge
    safe_result = engine.run(scenario, IdempotentPaymentAgent(max_retries=3))
    assert safe_result.evaluation.passed is True
    assert safe_result.evaluation.verdict == EvaluationVerdict.PASS
    assert safe_result.evaluation.actual_side_effects == 1
    assert safe_result.evaluation.duplicate_effect_count == 0
    assert safe_result.evaluation.idempotency_key_stable is True


def test_fault_mode_3_duplicate_delivery_unsafe_vs_safe() -> None:
    """3. Duplicate Request Delivery: Network/queue re-delivers the exact same request.

    - Unsafe agent (with changing/missing idempotency keys) causes 2 commits (DOUBLE CHARGE).
    - Safe agent (with stable idempotency key) dedupes the delivery, resulting in 1 commit.
    """
    scenario = ScenarioModel(
        scenario_id="test_duplicate_delivery",
        name="Duplicate Delivery",
        description="Simulates redelivery of request",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.DUPLICATE_DELIVERY,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=1,
                duplicate_count=1,
            )
        ],
        task={"amount": 7500, "currency": "USD"},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
        ),
    )

    engine = _create_engine()

    # Unsafe Agent: without idempotency key, duplicate delivery creates 2nd commit
    naive_agent = NaivePaymentAgent(use_random_idempotency_key=False)
    naive_result = engine.run(scenario, naive_agent)
    assert naive_result.evaluation.passed is False
    assert naive_result.evaluation.actual_side_effects == 2
    assert naive_result.evaluation.duplicate_effect_count == 1
    assert naive_result.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE

    # Safe agent with stable idempotency key:
    safe_result = engine.run(scenario, IdempotentPaymentAgent())
    assert safe_result.evaluation.passed is True
    assert safe_result.evaluation.actual_side_effects == 1
    assert safe_result.evaluation.duplicate_effect_count == 0
    assert safe_result.evaluation.verdict == EvaluationVerdict.PASS


def test_fault_mode_4_http_429_recovery() -> None:
    """4. HTTP 429 Rate Limit: Agent respects retry-after hint, backs off, and succeeds."""
    scenario = ScenarioModel(
        scenario_id="test_rate_limit",
        name="Rate Limit Recovery",
        description="Fails with 429 twice, then succeeds",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.HTTP_429_THEN_SUCCESS,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=2,
                retry_after_seconds=0.2,
                seed=42,
            )
        ],
        task={"amount": 1500, "currency": "USD"},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            max_allowed_retries=3,
            expect_agent_success=True,
        ),
    )

    engine = _create_engine()
    safe_agent = IdempotentPaymentAgent(max_retries=4)
    result = engine.run(scenario, safe_agent)

    assert result.evaluation.passed is True
    assert result.evaluation.actual_side_effects == 1
    assert len(result.events) == 3  # 2 rate limit events + 1 success


def test_fault_mode_5_http_500_transient() -> None:
    """5. HTTP 500 Transient Failure: Fails twice with 500, safe agent retries and succeeds."""
    scenario = ScenarioModel(
        scenario_id="test_http_500",
        name="HTTP 500 Transient Failure",
        description="Fails with 500 twice, then succeeds",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.HTTP_500_TRANSIENT,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=2,
            )
        ],
        task={"amount": 6200, "currency": "USD"},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            max_allowed_retries=4,
            expect_agent_success=True,
        ),
    )

    engine = _create_engine()
    safe_agent = IdempotentPaymentAgent(max_retries=4)
    result = engine.run(scenario, safe_agent)

    assert result.evaluation.passed is True
    assert result.evaluation.actual_side_effects == 1
    assert len(result.events) == 3


def test_fault_mode_6_malformed_tool_response() -> None:
    """6. Malformed Tool Output: Tool returns corrupted payload, safe agent detects and retries safely."""
    scenario = ScenarioModel(
        scenario_id="test_malformed_response",
        name="Malformed Tool Response",
        description="Returns corrupted payload on attempt 1",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.MALFORMED_TOOL_RESPONSE,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=1,
                malformed_mode="corrupt_payload",
            )
        ],
        task={"amount": 9900, "currency": "USD"},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
            max_allowed_retries=3,
            expect_agent_success=True,
        ),
    )

    engine = _create_engine()
    safe_agent = IdempotentPaymentAgent(max_retries=3)
    result = engine.run(scenario, safe_agent)

    assert result.evaluation.passed is True
    assert result.evaluation.actual_side_effects == 1
    assert result.evaluation.duplicate_effect_count == 0
    # Attempt 1 returned corrupted payload (committed on backend),
    # Attempt 2 re-presented the same stable idempotency key, mock tool deduped, and agent succeeded
    assert len(result.events) == 2
