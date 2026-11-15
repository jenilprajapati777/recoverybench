"""Automated tests for generic callable agent adapters and decorators."""


import recoverybench as rb
from recoverybench.adapters.callable import CallableAgentAdapter, agent, wrap_agent
from recoverybench.agent.base import AgentResult, TaskContext, ToolCaller


def test_callable_adapter_with_standard_signature() -> None:
    """Test function taking (task, tool_call) and returning AgentResult."""
    def sample_fn(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        res = tool_call("payment_gateway", "charge", {"amount": 50}, "key_1")
        return AgentResult(success=True, data=res, attempts=1)

    adapter = wrap_agent(sample_fn, agent_id="custom_agent")
    assert adapter.agent_id == "custom_agent"

    result = rb.test_agent("payment_timeout_before_commit", adapter)
    # timeout before commit on attempt 1 without retry fails
    assert result.verdict in ("GENUINE_FAILURE", "PASS")


def test_callable_decorator_and_resilient_retry() -> None:
    """Test @agent decorator with resilient retry loop under timeout_after_commit."""
    @agent(id="resilient_callable")
    def resilient_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        stable_key = "stable_test_key_001"
        for attempt in range(1, 4):
            try:
                res = tool_call("payment_gateway", "charge", task.task_params, stable_key)
                return AgentResult(
                    success=True,
                    data=res,
                    attempts=attempt,
                    status_message="Payment processed",
                )
            except Exception:
                continue
        return AgentResult(success=False, error="Failed after retries")

    result = rb.test_agent("payment_timeout_after_commit", resilient_agent)
    assert result.passed
    assert result.verdict == "PASS"
    assert result.evaluation.actual_side_effects == 1
    assert result.evaluation.duplicate_effect_count == 0


def test_callable_naive_retry_triggers_duplicate_detection() -> None:
    """Test @agent decorator with naive key generation failing assertion."""
    @agent(id="naive_callable")
    def naive_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        for attempt in range(1, 4):
            try:
                # Generates a new key on every attempt!
                fresh_key = f"unstable_key_{attempt}"
                res = tool_call("payment_gateway", "charge", task.task_params, fresh_key)
                return AgentResult(
                    success=True,
                    data=res,
                    attempts=attempt,
                    status_message="Payment processed",
                )
            except Exception:
                continue
        return AgentResult(success=False, error="Failed after retries")

    result = rb.test_agent("payment_timeout_after_commit", naive_agent)
    assert not result.passed
    assert result.verdict == "GENUINE_FAILURE"
    assert result.evaluation.duplicate_effect_count == 1


def test_callable_normalizes_dict_and_bool_returns() -> None:
    """Test that functions returning dict or bool are normalized into valid AgentResult."""
    def dict_fn(task: TaskContext, tool_call: ToolCaller) -> dict[str, object]:
        return {"success": True, "transaction": "ok", "attempts": 2}

    adapter = CallableAgentAdapter(dict_fn)
    # Execute dummy run
    task = TaskContext(scenario_id="test", clock=rb.clock.VirtualClock())
    res = adapter.run(task, lambda t, a, p, k: {})
    assert res.success is True
    assert res.attempts == 2

    def bool_fn(tool_call: ToolCaller) -> bool:
        return True

    adapter_bool = CallableAgentAdapter(bool_fn)
    res_bool = adapter_bool.run(task, lambda t, a, p, k: {})
    assert res_bool.success is True


def test_callable_catches_unhandled_exceptions_safely() -> None:
    """Test that agent functions raising unhandled exceptions don't crash the harness."""
    @agent
    def crashing_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        raise ValueError("Simulated unexpected crash inside agent reasoning code")

    result = rb.test_agent("payment_timeout_before_commit", crashing_agent)
    assert not result.passed
    assert result.summary.verdict in (
        rb.models.EvaluationVerdict.GENUINE_FAILURE,
        rb.models.EvaluationVerdict.INFRASTRUCTURE_ERROR,
        rb.models.EvaluationVerdict.INSUFFICIENT_EVIDENCE,
    )
