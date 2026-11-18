"""Comprehensive test suite for P0-2 False-Success Prevention.

Verifies:
- Explicit agent completion states (SUCCEEDED, FAILED, UNKNOWN).
- Result normalization across None, bool, dict, exceptions, and unsupported types.
- Evaluator rules A through G (side-effects vs completion truth vs infrastructure integrity).
- Detection of phantom success, orphaned commits, ambiguous terminations, and independent violations.
"""



from recoverybench.adapters.callable import CallableAgentAdapter
from recoverybench.agent.base import (
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolCaller,
)
from recoverybench.clock import VirtualClock
from recoverybench.engine.runner import BoundToolClient, ExecutionEngine
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.models import (
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
    LedgerEvent,
)
from recoverybench.scenarios.registry import ScenarioRegistry
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


def test_scenario_1_agent_returns_none_before_tool_call() -> None:
    """1. Agent returns None before tool call -> empty ledger and UNKNOWN status -> INSUFFICIENT_EVIDENCE (Rule G & A)."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="none_before_tool",
        name="None Before Tool",
        description="Agent returns None without making any tool calls",
        target_tool="payment_gateway",
        target_action="charge",
        task={"amount": 1000},
        assertions=ScenarioAssertions(expected_side_effects=1, expect_agent_success=True),
    )

    def agent_returns_none(context: TaskContext, tool: ToolCaller) -> None:
        return None

    result = engine.run(scenario, agent_returns_none, run_id="run_none_before")

    # Empty ledger yields INSUFFICIENT_EVIDENCE under Rule G
    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.UNKNOWN
    assert result.agent_raw_result["success"] is False


def test_scenario_2_agent_commits_and_returns_none() -> None:
    """2. Agent commits and returns None -> side effects satisfied, completion UNKNOWN -> INSUFFICIENT_EVIDENCE (Rule A)."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="commit_then_none",
        name="Commit Then None",
        description="Agent commits side-effect then exits returning None",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=True,
        task={"amount": 1000, "currency": "USD"},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
            expect_agent_success=True,
        ),
    )

    def agent_commits_then_none(context: TaskContext, tool: ToolCaller) -> None:
        tool(
            "payment_gateway",
            "charge",
            {"amount": 1000, "currency": "USD"},
            idempotency_key="key_valid_1",
            operation_id="op_commit_none",
        )
        return None

    result = engine.run(scenario, agent_commits_then_none, run_id="run_commit_none")

    # Side-effect is satisfied (1 commit), but completion truth is UNKNOWN -> INSUFFICIENT_EVIDENCE
    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.UNKNOWN
    assert result.agent_raw_result["success"] is False
    assert result.evaluation.actual_side_effects == 1

    completion_assertion = next(a for a in result.evaluation.assertions if a.name == "agent_completion_truth")
    assert completion_assertion.passed is False
    assert completion_assertion.actual == "insufficient_evidence_ambiguous_completion"
    assert len(completion_assertion.event_ids) == 1


def test_scenario_3_agent_commits_encounters_timeout_returns_none() -> None:
    """3. Agent commits, encounters timeout after commit, swallows error and returns None -> INSUFFICIENT_EVIDENCE (Rule A)."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    # Builtin scenario: payment_timeout_after_commit
    scenario = ScenarioRegistry().get("payment_timeout_after_commit")
    assert scenario is not None

    def agent_swallowing_timeout(context: TaskContext, tool: ToolCaller) -> None:
        try:
            tool(
                "payment_gateway",
                "charge",
                {"amount": 5000, "currency": "USD"},
                idempotency_key="key_swallow_1",
            )
        except Exception:
            # Swallows exception and returns None
            pass
        return None

    result = engine.run(scenario, agent_swallowing_timeout, run_id="run_swallow_timeout")

    # State was committed (1 commit, no duplicates), but completion is UNKNOWN -> INSUFFICIENT_EVIDENCE
    assert result.passed is False
    assert result.evaluation.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.UNKNOWN
    assert result.evaluation.actual_side_effects == 1

    unknown_assertion = next(a for a in result.evaluation.assertions if a.name == "unknown_outcome_handling")
    assert unknown_assertion.passed is False
    assert unknown_assertion.actual == "insufficient_evidence_ambiguous_completion"


def test_scenario_4_agent_explicitly_returns_agent_result_succeeded() -> None:
    """4. Agent explicitly returns AgentResult(success=True) -> status=SUCCEEDED, PASS (Rule B)."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="explicit_agent_result_success",
        name="Explicit Succeeded",
        description="Agent returns AgentResult(success=True)",
        target_tool="payment_gateway",
        target_action="charge",
        single_operation=True,
        task={},
        assertions=ScenarioAssertions(expected_side_effects=1, expect_agent_success=True),
    )

    def agent_explicit_success(context: TaskContext, tool: ToolCaller) -> AgentResult:
        res = tool(
            "payment_gateway",
            "charge",
            {"amount": 1000, "currency": "USD"},
            idempotency_key="key_succ_1",
            operation_id="op_succ_1",
        )
        return AgentResult(
            status=CompletionStatus.SUCCEEDED,
            data={"charge": res},
            status_message="Transaction confirmed",
        )

    result = engine.run(scenario, agent_explicit_success, run_id="run_explicit_succ")

    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.SUCCEEDED
    assert result.agent_raw_result["success"] is True

    completion_assertion = next(a for a in result.evaluation.assertions if a.name == "agent_completion_truth")
    assert completion_assertion.passed is True
    assert completion_assertion.actual is True


def test_scenario_5_agent_explicitly_returns_agent_result_failed() -> None:
    """5. Agent explicitly returns AgentResult(success=False) when failure expected -> PASS (Rule E)."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="expected_failure_scenario",
        name="Expected Failure",
        description="Agent expected to fail cleanly without mutations",
        target_tool="payment_gateway",
        target_action="charge",
        task={},
        assertions=ScenarioAssertions(
            expected_side_effects=0,
            expect_agent_success=False,
        ),
    )

    def agent_clean_failure(context: TaskContext, tool: ToolCaller) -> AgentResult:
        return AgentResult(
            status=CompletionStatus.FAILED,
            error="Payment intentionally rejected by validation",
            status_message="Halted safely",
        )

    result = engine.run(scenario, agent_clean_failure, run_id="run_clean_failure")

    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.FAILED
    assert result.agent_raw_result["success"] is False

    completion_assertion = next(a for a in result.evaluation.assertions if a.name == "agent_completion_truth")
    assert completion_assertion.passed is True
    assert completion_assertion.actual is False


def test_scenario_6_agent_returns_boolean_true() -> None:
    """6. Agent returns boolean True -> normalized to SUCCEEDED (success=True)."""
    adapter = CallableAgentAdapter(lambda t: True)
    context = TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock())
    res = adapter.run(context, lambda *args, **kwargs: {})

    assert res.status == CompletionStatus.SUCCEEDED
    assert res.success is True
    assert res.is_succeeded is True
    assert res.is_failed is False
    assert res.is_unknown is False


def test_scenario_7_agent_returns_boolean_false() -> None:
    """7. Agent returns boolean False -> normalized to FAILED (success=False)."""
    adapter = CallableAgentAdapter(lambda t: False)
    context = TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock())
    res = adapter.run(context, lambda *args, **kwargs: {})

    assert res.status == CompletionStatus.FAILED
    assert res.success is False
    assert res.is_succeeded is False
    assert res.is_failed is True
    assert res.is_unknown is False


def test_scenario_8_agent_returns_dict_with_success_true() -> None:
    """8. Agent returns dict with success=True -> normalized to SUCCEEDED."""
    adapter = CallableAgentAdapter(lambda t: {"success": True, "order_id": "ord_123"})
    context = TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock())
    res = adapter.run(context, lambda *args, **kwargs: {})

    assert res.status == CompletionStatus.SUCCEEDED
    assert res.success is True
    assert res.data["order_id"] == "ord_123"


def test_scenario_9_agent_returns_dict_without_success_field() -> None:
    """9. Agent returns dict without success or status -> normalized to UNKNOWN."""
    adapter = CallableAgentAdapter(lambda t: {"order_id": "ord_456", "items_count": 3})
    context = TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock())
    res = adapter.run(context, lambda *args, **kwargs: {})

    assert res.status == CompletionStatus.UNKNOWN
    assert res.success is False
    assert res.is_unknown is True
    assert "lacks explicit 'success' or 'status'" in (res.error or "")


def test_scenario_10_phantom_success_detection() -> None:
    """10. Agent reports success without committing expected effect -> GENUINE_FAILURE (Rule D)."""
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)

    # Empty ledger: 0 commits
    events: list[LedgerEvent] = []

    # Agent claims success
    agent_result = AgentResult(status=CompletionStatus.SUCCEEDED, success=True)

    evaluation = evaluator.evaluate(events, assertions, agent_result)

    assert evaluation.passed is False
    assert evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert evaluation.actual_side_effects == 0

    completion_assertion = next(a for a in evaluation.assertions if a.name == "agent_completion_truth")
    assert completion_assertion.passed is False
    assert completion_assertion.actual == "phantom_success_no_commits"
    assert "False-success detected" in completion_assertion.message


def test_scenario_11_agent_reports_success_after_duplicate_effects() -> None:
    """11. Agent reports success after duplicate effects -> GENUINE_FAILURE (Rule F)."""
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(
        expected_side_effects=1,
        max_duplicate_effects=0,
        require_stable_idempotency_key=True,
        expect_agent_success=True,
    )

    events = [
        LedgerEvent(
            event_id=1,
            run_id="run_dup",
            scenario_id="s1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_1",
            attempt_id="att_1",
            effect_id="eff_1",
            idempotency_key_hash="hash_1",
            request_fingerprint="fp_dup_1",
            attempt_number=1,
            execution_status=ExecutionStatus.FAULT_INJECTED,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2024-01-01T00:00:00Z",
        ),
        LedgerEvent(
            event_id=2,
            run_id="run_dup",
            scenario_id="s1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_1",
            attempt_id="att_2",
            effect_id="eff_2",
            idempotency_key_hash="hash_2",
            request_fingerprint="fp_dup_2",
            attempt_number=2,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2024-01-01T00:00:01Z",
        ),
    ]

    agent_result = AgentResult(status=CompletionStatus.SUCCEEDED, success=True)
    evaluation = evaluator.evaluate(events, assertions, agent_result)

    assert evaluation.passed is False
    assert evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert evaluation.duplicate_effect_count == 1


def test_scenario_12_agent_reports_failure_after_valid_single_commit() -> None:
    """12. Agent reports failure after valid single commit -> GENUINE_FAILURE (Rule C)."""
    evaluator = RecoveryEvaluator()
    assertions = ScenarioAssertions(
        expected_side_effects=1,
        max_duplicate_effects=0,
        expect_agent_success=True,
    )

    events = [
        LedgerEvent(
            event_id=1,
            run_id="run_orphaned",
            scenario_id="s1",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_orphaned",
            attempt_id="att_1",
            effect_id="eff_1",
            idempotency_key_hash="hash_1",
            request_fingerprint="fp_orphaned",
            attempt_number=1,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=CommitStatus.COMMITTED,
            simulated_timestamp="2024-01-01T00:00:00Z",
        ),
    ]

    # Agent reports FAILED despite commit in ledger
    agent_result = AgentResult(
        status=CompletionStatus.FAILED,
        success=False,
        error="Gateway timeout encountered",
    )
    evaluation = evaluator.evaluate(events, assertions, agent_result)

    assert evaluation.passed is False
    assert evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE
    assert evaluation.actual_side_effects == 1

    completion_assertion = next(a for a in evaluation.assertions if a.name == "agent_completion_truth")
    assert completion_assertion.passed is False
    assert completion_assertion.actual == "agent_reported_failure_after_commit"
    assert "orphaned commit" in completion_assertion.message


def test_scenario_13_unsupported_return_type_handling() -> None:
    """13. Unsupported agent return type (e.g. 42 or list) -> UNKNOWN status."""
    adapter_int = CallableAgentAdapter(lambda t: 42)
    context = TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock())
    res = adapter_int.run(context, lambda *args, **kwargs: {})

    assert res.status == CompletionStatus.UNKNOWN
    assert res.success is False
    assert "Unsupported agent return type: int" in (res.error or "")
    assert res.data["unsupported_result"] == 42


def test_scenario_14_langgraph_safe_agent_passes_and_naive_fails() -> None:
    """14. LangGraph safe agent reports explicit success and passes; naive agent fails."""
    from recoverybench.adapters.langgraph import (
        IdempotentLangGraphAgent,
        NaiveLangGraphAgent,
    )

    ledger = InMemoryLedger()
    registry = ToolRegistry()
    payment_service = MockPaymentService()
    registry.register(payment_service)
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioRegistry().get("payment_timeout_after_commit")
    assert scenario is not None

    # Safe agent passes
    safe_agent = IdempotentLangGraphAgent(max_retries=3)
    safe_result = engine.run(scenario, safe_agent, run_id="run_lg_safe")
    assert safe_result.passed is True
    assert safe_result.evaluation.verdict == EvaluationVerdict.PASS
    assert safe_result.agent_raw_result is not None
    assert safe_result.agent_raw_result["status"] == CompletionStatus.SUCCEEDED

    # Naive agent fails
    naive_agent = NaiveLangGraphAgent(max_retries=3)
    naive_result = engine.run(scenario, naive_agent, run_id="run_lg_naive")
    assert naive_result.passed is False
    assert naive_result.evaluation.verdict == EvaluationVerdict.GENUINE_FAILURE


def test_scenario_15_multi_operation_identity_with_explicit_completion() -> None:
    """15. Multi-operation scenario with explicit completion status succeeds."""
    ledger = InMemoryLedger()
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="multi_op_explicit_test",
        name="Multi Op Explicit",
        description="Verify multi-op with explicit completion succeeds cleanly",
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

    def multi_op_explicit_agent(context: TaskContext, tool: BoundToolClient) -> AgentResult:
        with tool.operation("op_batch_1"):
            res1 = tool(
                "payment_gateway",
                "charge",
                {"amount": 1000, "customer_id": "cust_1"},
                idempotency_key="key_batch_1",
            )
        with tool.operation("op_batch_2"):
            res2 = tool(
                "payment_gateway",
                "charge",
                {"amount": 2000, "customer_id": "cust_2"},
                idempotency_key="key_batch_2",
            )
        return AgentResult(
            status=CompletionStatus.SUCCEEDED,
            data={"res1": res1, "res2": res2},
            status_message="Both orders processed",
        )

    result = engine.run(scenario, multi_op_explicit_agent, run_id="run_multi_explicit")

    assert result.passed is True
    assert result.evaluation.verdict == EvaluationVerdict.PASS
    assert result.evaluation.actual_side_effects == 2
    assert result.agent_raw_result is not None
    assert result.agent_raw_result["status"] == CompletionStatus.SUCCEEDED


def test_scenario_16_backwards_compatibility_and_demo_comparison() -> None:
    """16. Verify AgentResult(success=True) backwards-compatibility and demo comparison."""
    # Test legacy instantiation
    res_legacy_true = AgentResult(success=True)
    assert res_legacy_true.status == CompletionStatus.SUCCEEDED
    assert res_legacy_true.success is True
    assert res_legacy_true.is_succeeded is True

    res_legacy_false = AgentResult(success=False)
    assert res_legacy_false.status == CompletionStatus.FAILED
    assert res_legacy_false.success is False
    assert res_legacy_false.is_failed is True

    # Test dict normalization with string status
    adapter = CallableAgentAdapter(lambda t: {"status": "succeeded", "charge_id": "ch_123"})
    res_str_status = adapter.run(
        TaskContext(scenario_id="s1", task_params={}, clock=VirtualClock()),
        lambda *a, **k: {},
    )
    assert res_str_status.status == CompletionStatus.SUCCEEDED
    assert res_str_status.success is True
