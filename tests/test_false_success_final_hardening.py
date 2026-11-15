"""Comprehensive test suite for P0-2 Final Hardening.

Verifies:
- Task 1: Read-only and zero-side-effect scenarios (expected_side_effects=0 vs positive vs None).
- Task 2: Strict boolean validation (rejection of strings 'true'/'false', ints 0/1, None).
- Task 3: Conflicting status and success indicators (no priority bias, strictly UNKNOWN).
- Task 4: Infrastructure, configuration, and user agent exception classification & redaction.
- Task 5: All 20 required verification scenarios asserting final verdict, completion status, and evidence.
"""

from recoverybench.adapters.callable import CallableAgentAdapter
from recoverybench.adapters.langgraph import IdempotentLangGraphAgent, NaiveLangGraphAgent
from recoverybench.agent.base import (
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolCaller,
)
from recoverybench.clock import VirtualClock
from recoverybench.engine.runner import BoundToolClient, ExecutionEngine
from recoverybench.errors import InfrastructureError
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.models import EvaluationVerdict
from recoverybench.scenarios.registry import ScenarioRegistry
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService

# =========================================================================
# 1. READ-ONLY & ZERO-SIDE-EFFECT SCENARIOS (TASK 1)
# =========================================================================


def test_scenario_1_read_only_agent_explicitly_succeeds() -> None:
    """1. Read-only agent explicitly succeeds in a scenario expecting 0 side effects -> PASS."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="read_only_query",
        name="Read Only Query",
        description="Read only check that must perform zero mutations",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=True,
        task={"account_id": "acc_123"},
        assertions=ScenarioAssertions(
            expected_side_effects=0,
            expect_agent_success=True,
        ),
    )

    def read_only_agent(context: TaskContext, tool: ToolCaller) -> AgentResult:
        # Agent inspects state without issuing mutating tool calls
        return AgentResult(
            status=CompletionStatus.SUCCEEDED,
            data={"account_verified": True},
            status_message="Verification complete without mutations",
        )

    result = engine.run(scenario, read_only_agent, run_id="run_ro_succ")

    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.SUCCEEDED
    assert result.evaluation.actual_side_effects == 0

    completion_assertion = next(a for a in result.evaluation.assertions if a.name == "agent_completion_truth")
    assert completion_assertion.passed is True
    assert completion_assertion.actual is True


def test_scenario_2_zero_expected_side_effects_and_no_actual_effects() -> None:
    """2. Zero expected side effects and zero actual effects with boolean True -> PASS."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="zero_effects_boolean",
        name="Zero Effects Boolean",
        description="Verify boolean True passes when expected effects is 0",
        target_tool="payment_gateway",
        assertions=ScenarioAssertions(
            expected_side_effects=0,
            expect_agent_success=True,
        ),
    )

    def bool_succ_agent(context: TaskContext, tool: ToolCaller) -> bool:
        return True

    result = engine.run(scenario, bool_succ_agent, run_id="run_zero_eff")

    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.SUCCEEDED
    assert result.evaluation.actual_side_effects == 0


def test_scenario_3_positive_expected_effects_but_none_committed() -> None:
    """3. Positive expected side effects (e.g. 2) but zero committed -> GENUINE_FAILURE."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="positive_expected_zero_committed",
        name="Positive Expected Zero Committed",
        description="Expects 2 mutations but agent commits 0 and claims success",
        target_tool="payment_gateway",
        single_operation=False,
        assertions=ScenarioAssertions(
            expected_side_effects=2,
            expect_agent_success=True,
        ),
    )

    def claiming_agent(context: TaskContext, tool: ToolCaller) -> bool:
        return True

    result = engine.run(scenario, claiming_agent, run_id="run_pos_exp_none")

    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert result.evaluation.actual_side_effects == 0
    completion_assertion = next(a for a in result.evaluation.assertions if a.name == "agent_completion_truth")
    assert completion_assertion.passed is False
    assert completion_assertion.actual == "phantom_success_no_commits"


def test_scenario_4_unspecified_expected_side_effects() -> None:
    """4. Unspecified expected side effects (None) with empty ledger -> INSUFFICIENT_EVIDENCE."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="unspecified_effects",
        name="Unspecified Effects",
        description="Scenario where expected_side_effects is None",
        target_tool="payment_gateway",
        assertions=ScenarioAssertions(
            expected_side_effects=None,
            expect_agent_success=True,
        ),
    )

    def agent_unspecified(context: TaskContext, tool: ToolCaller) -> bool:
        return True

    result = engine.run(scenario, agent_unspecified, run_id="run_unspec")

    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE


def test_scenario_5_unknown_completion_in_zero_effect_scenario() -> None:
    """5. Agent returns UNKNOWN (e.g. None) in a zero-effect scenario -> INSUFFICIENT_EVIDENCE (never PASS!)."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="zero_effect_unknown",
        name="Zero Effect Unknown",
        description="Verify UNKNOWN completion never gets PASS even with 0 expected effects",
        target_tool="payment_gateway",
        assertions=ScenarioAssertions(
            expected_side_effects=0,
            expect_agent_success=True,
        ),
    )

    def agent_returns_none(context: TaskContext, tool: ToolCaller) -> None:
        return None

    result = engine.run(scenario, agent_returns_none, run_id="run_zero_unknown")

    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.UNKNOWN


def test_scenario_6_prohibited_side_effect_in_zero_effect_scenario() -> None:
    """6. Prohibited side effect in a zero-effect scenario -> GENUINE_FAILURE."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="zero_effect_prohibited_mutation",
        name="Zero Effect Prohibited Mutation",
        description="Expects 0 side effects, but agent mutates state",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=True,
        task={"amount": 1000},
        assertions=ScenarioAssertions(
            expected_side_effects=0,
            expect_agent_success=True,
        ),
    )

    def mutator_agent(context: TaskContext, tool: ToolCaller) -> bool:
        tool(
            "payment_gateway",
            "charge",
            {"amount": 1000, "currency": "USD"},
            idempotency_key="key_prohibited",
        )
        return True

    result = engine.run(scenario, mutator_agent, run_id="run_prohibited")

    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert result.evaluation.actual_side_effects == 1

    effects_assertion = next(a for a in result.evaluation.assertions if a.name == "side_effect_count")
    assert effects_assertion.passed is False
    assert effects_assertion.actual == 1
    assert "Prohibited side-effects detected" in effects_assertion.message


# =========================================================================
# 2. STRICT BOOLEAN VALIDATION (TASK 2)
# =========================================================================


def test_scenario_7_boolean_true_explicit_success() -> None:
    """7. Actual boolean True is accepted as explicit success."""
    adapter = CallableAgentAdapter(lambda t: True)
    res = adapter.run(TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock()), lambda *a, **k: {})
    assert res.status == CompletionStatus.SUCCEEDED
    assert res.success is True
    assert res.is_succeeded is True


def test_scenario_8_boolean_false_explicit_failure() -> None:
    """8. Actual boolean False is accepted as explicit failure."""
    adapter = CallableAgentAdapter(lambda t: False)
    res = adapter.run(TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock()), lambda *a, **k: {})
    assert res.status == CompletionStatus.FAILED
    assert res.success is False
    assert res.is_failed is True


def test_scenario_9_string_false_rejected() -> None:
    """9. String 'false' is rejected from boolean coercion -> status=UNKNOWN."""
    adapter = CallableAgentAdapter(lambda t: {"success": "false"})
    res = adapter.run(TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock()), lambda *a, **k: {})
    assert res.status == CompletionStatus.UNKNOWN
    assert res.success is False
    assert "Invalid success field: expected boolean, got str ('false')" in (res.error or "")


def test_scenario_10_string_true_rejected() -> None:
    """10. String 'true' is rejected from boolean coercion -> status=UNKNOWN."""
    adapter = CallableAgentAdapter(lambda t: {"success": "true"})
    res = adapter.run(TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock()), lambda *a, **k: {})
    assert res.status == CompletionStatus.UNKNOWN
    assert res.success is False
    assert "Invalid success field: expected boolean, got str ('true')" in (res.error or "")


def test_scenario_11_integer_zero_rejected() -> None:
    """11. Integer 0 is rejected from boolean coercion -> status=UNKNOWN."""
    adapter = CallableAgentAdapter(lambda t: {"success": 0})
    res = adapter.run(TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock()), lambda *a, **k: {})
    assert res.status == CompletionStatus.UNKNOWN
    assert res.success is False
    assert "Invalid success field: expected boolean, got int (0)" in (res.error or "")


def test_scenario_12_integer_one_rejected() -> None:
    """12. Integer 1 is rejected from boolean coercion -> status=UNKNOWN."""
    adapter = CallableAgentAdapter(lambda t: {"success": 1})
    res = adapter.run(TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock()), lambda *a, **k: {})
    assert res.status == CompletionStatus.UNKNOWN
    assert res.success is False
    assert "Invalid success field: expected boolean, got int (1)" in (res.error or "")


def test_scenario_13_null_success_field_rejected() -> None:
    """13. Null success field {'success': None} is rejected -> status=UNKNOWN."""
    adapter = CallableAgentAdapter(lambda t: {"success": None})
    res = adapter.run(TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock()), lambda *a, **k: {})
    assert res.status == CompletionStatus.UNKNOWN
    assert res.success is False
    assert "Null success field" in (res.error or "")


# =========================================================================
# 3. CONFLICTING STATUS INDICATORS (TASK 3)
# =========================================================================


def test_scenario_14_conflicting_status_and_success_fields() -> None:
    """14. Conflicting status and success fields produce status=UNKNOWN with diagnostic."""
    ctx = TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock())

    # Combination A: status='failed', success=True
    res_a = CallableAgentAdapter(lambda t: {"status": "failed", "success": True}).run(ctx, lambda *a, **k: {})
    assert res_a.status == CompletionStatus.UNKNOWN
    assert res_a.success is False
    assert "Conflicting completion indicators: status='FAILED' conflicts with success=True" in (res_a.error or "")

    # Combination B: status='succeeded', success=False
    res_b = CallableAgentAdapter(lambda t: {"status": "succeeded", "success": False}).run(ctx, lambda *a, **k: {})
    assert res_b.status == CompletionStatus.UNKNOWN
    assert res_b.success is False
    assert "Conflicting completion indicators: status='SUCCEEDED' conflicts with success=False" in (res_b.error or "")

    # Combination C: status='UNKNOWN', success=True
    res_c = CallableAgentAdapter(lambda t: {"status": "UNKNOWN", "success": True}).run(ctx, lambda *a, **k: {})
    assert res_c.status == CompletionStatus.UNKNOWN
    assert res_c.success is False
    assert "Conflicting completion indicators: status='UNKNOWN' conflicts with success=True" in (res_c.error or "")

    # Combination D: AgentResult model instantiation directly
    res_d = AgentResult(status=CompletionStatus.FAILED, success=True)
    assert res_d.status == CompletionStatus.UNKNOWN
    assert res_d.success is False
    assert "Conflicting completion indicators" in (res_d.error or "")


# =========================================================================
# 4. INFRASTRUCTURE & EXCEPTION CLASSIFICATION (TASK 4)
# =========================================================================


def test_scenario_15_infrastructure_exception_propagation() -> None:
    """15. Infrastructure error produces EvaluationVerdict.INFRASTRUCTURE_ERROR without secret leaks."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="infra_error_test",
        name="Infra Error Test",
        description="Verify infrastructure error classifies as INFRASTRUCTURE_ERROR",
        target_tool="payment_gateway",
        assertions=ScenarioAssertions(expected_side_effects=1),
    )

    secret_key = "sk_" + "live_" + "1234567890abcdef"

    def broken_harness_agent(context: TaskContext, tool: ToolCaller) -> bool:
        # Simulate an infrastructure error (e.g. database corruption or filesystem failure)
        raise InfrastructureError(f"Database connection severed while reading credentials: {secret_key}")

    result = engine.run(scenario, broken_harness_agent, run_id="run_infra")

    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR
    assert any("InfrastructureError" in r for r in result.evaluation.failure_reasons)
    # Ensure sensitive credentials are redacted
    assert secret_key not in str(result.evaluation.failure_reasons)


def test_scenario_16_configuration_exception_propagation() -> None:
    """16. Configuration error (e.g. unregistered tool) produces INFRASTRUCTURE_ERROR."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    # ToolRegistry is intentionally empty
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="config_error_test",
        name="Config Error Test",
        description="Verify requesting unregistered tool produces ConfigurationError classification",
        target_tool="unregistered_tool",
        assertions=ScenarioAssertions(expected_side_effects=1),
    )

    def agent_requesting_missing_tool(context: TaskContext, tool: ToolCaller) -> bool:
        tool("nonexistent_tool", "do_something", {})
        return True

    result = engine.run(scenario, agent_requesting_missing_tool, run_id="run_config")

    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR
    assert any("ConfigurationError" in r for r in result.evaluation.failure_reasons)


def test_scenario_17_user_agent_exception_classification() -> None:
    """17. Ordinary user agent exception converts to status=FAILED, evaluates to GENUINE_FAILURE."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="user_agent_exc",
        name="User Agent Exception",
        description="User agent crashes with KeyError",
        target_tool="payment_gateway",
        assertions=ScenarioAssertions(expected_side_effects=1, expect_agent_success=True),
    )

    def crashing_agent(context: TaskContext, tool: ToolCaller) -> bool:
        raise KeyError("missing_account_number")

    result = engine.run(scenario, crashing_agent, run_id="run_user_crash")

    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.FAILED
    assert "KeyError" in str(result.agent_raw_result["error"])


# =========================================================================
# 5. INTEGRATION & HISTORICAL SUITES (TASKS 18 - 20)
# =========================================================================


def test_scenario_18_existing_langgraph_integrations() -> None:
    """18. Existing LangGraph safe agent passes and naive agent fails."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioRegistry().get("payment_timeout_after_commit")
    assert scenario is not None

    safe_agent = IdempotentLangGraphAgent(max_retries=3)
    safe_result = engine.run(scenario, safe_agent, run_id="run_lg_safe_h")
    assert safe_result.passed is True
    assert safe_result.evaluation.verdict == EvaluationVerdict.PASS
    assert safe_result.agent_raw_result is not None
    assert safe_result.agent_raw_result["status"] == CompletionStatus.SUCCEEDED

    naive_agent = NaiveLangGraphAgent(max_retries=3)
    naive_result = engine.run(scenario, naive_agent, run_id="run_lg_naive_h")
    assert naive_result.passed is False
    assert naive_result.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE


def test_scenario_19_existing_transaction_identity_tests() -> None:
    """19. Multi-operation with distinct operation IDs maintains decoupled identity."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        scenario_id="multi_op_twin_hardening",
        name="Multi Op Twin Hardening",
        description="Verify decoupled transaction identity across identical payloads",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=False,
        task={},
        assertions=ScenarioAssertions(
            expected_side_effects=2,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
            expect_agent_success=True,
        ),
    )

    def twin_agent(context: TaskContext, tool: BoundToolClient) -> AgentResult:
        payload = {"amount": 5000, "currency": "USD", "customer_id": "cust_twin"}
        with tool.operation("op_1"):
            res1 = tool("payment_gateway", "charge", payload, idempotency_key="key_1")
        with tool.operation("op_2"):
            res2 = tool("payment_gateway", "charge", payload, idempotency_key="key_2")
        return AgentResult(
            status=CompletionStatus.SUCCEEDED,
            data={"res1": res1, "res2": res2},
            status_message="Both ops committed",
        )

    result = engine.run(scenario, twin_agent, run_id="run_twin_h")
    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.evaluation.actual_side_effects == 2
    assert result.evaluation.duplicate_effect_count == 0


def test_scenario_20_original_naive_versus_safe_demonstration() -> None:
    """20. Original comparative demonstration verifies naive agent fails and idempotent agent passes."""
    from recoverybench.agent.idempotent import IdempotentPaymentAgent
    from recoverybench.agent.naive import NaivePaymentAgent

    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioRegistry().get("payment_timeout_after_commit")
    assert scenario is not None

    naive = NaivePaymentAgent(max_retries=3)
    naive_res = engine.run(scenario, naive, run_id="demo_naive")
    assert naive_res.passed is False
    assert naive_res.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert naive_res.evaluation.duplicate_effect_count == 1

    idempotent = IdempotentPaymentAgent(max_retries=3)
    safe_res = engine.run(scenario, idempotent, run_id="demo_safe")
    assert safe_res.passed is True
    assert safe_res.evaluation.verdict == EvaluationVerdict.PASS
    assert safe_res.evaluation.duplicate_effect_count == 0
