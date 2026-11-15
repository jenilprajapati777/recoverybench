"""Integration tests for the core comparative demonstration (Naive vs. Safe Agent)."""

from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.models import EvaluationVerdict
from recoverybench.scenarios.schema import ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


def test_comparative_demonstration_naive_vs_safe(timeout_after_scenario: ScenarioModel) -> None:
    # Setup Engine
    ledger = InMemoryLedger()
    tool_registry = ToolRegistry()
    tool_registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=tool_registry)

    # 1. Execute Unsafe (Naive) Agent
    naive_agent = NaivePaymentAgent(max_retries=3)
    naive_result = engine.run(timeout_after_scenario, naive_agent, run_id="run_naive_demo")

    # Verify Naive Agent Failure Invariants:
    # - Produced DOUBLE CHARGE (2 actual commits vs. 1 expected)
    # - 1 duplicate side effect detected
    # - Idempotency key stability assertion failed
    # - Verdict is GENUINE_FAILURE
    assert naive_result.evaluation.passed is False
    assert naive_result.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert naive_result.evaluation.actual_side_effects == 2
    assert naive_result.evaluation.duplicate_effect_count == 1
    assert naive_result.evaluation.idempotency_key_stable is False

    # 2. Execute Resilient (Idempotent) Agent
    safe_agent = IdempotentPaymentAgent(max_retries=3)
    safe_result = engine.run(timeout_after_scenario, safe_agent, run_id="run_safe_demo")

    # Verify Safe Agent Success Invariants:
    # - Exactly 1 committed side effect in ledger
    # - 0 duplicate side effects
    # - Idempotency key stability assertion passed
    # - Verdict is PASS
    assert safe_result.evaluation.passed is True
    assert safe_result.evaluation.verdict == EvaluationVerdict.PASS
    assert safe_result.evaluation.actual_side_effects == 1
    assert safe_result.evaluation.duplicate_effect_count == 0
    assert safe_result.evaluation.idempotency_key_stable is True
