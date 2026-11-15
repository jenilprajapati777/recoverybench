"""Automated integration tests for LangGraph framework adapter."""


import recoverybench as rb
from recoverybench.adapters.langgraph import (
    IdempotentLangGraphAgent,
    LangGraphAdapter,
    NaiveLangGraphAgent,
    build_idempotent_langgraph_agent,
    create_langchain_payment_tool,
)


def test_create_langchain_payment_tool() -> None:
    """Test bridging of RecoveryBench tool caller into LangChain BaseTool."""
    calls: list[dict[str, object]] = []

    def mock_caller(tool_name: str, action: str, params: dict[str, object], key: str | None = None) -> dict[str, object]:
        calls.append({"tool": tool_name, "action": action, "params": params, "key": key})
        return {"charge_id": "ch_mock_123", "status": "succeeded"}

    tool = create_langchain_payment_tool(mock_caller)
    assert tool.name == "payment_gateway_charge"
    assert "amount" in tool.args
    assert "currency" in tool.args
    assert "idempotency_key" in tool.args

    # Invoke tool
    output = tool.invoke({"amount": 250.0, "currency": "USD", "idempotency_key": "test_key_abc"})
    assert "ch_mock_123" in str(output)
    assert len(calls) == 1
    assert calls[0]["key"] == "test_key_abc"
    assert calls[0]["params"] == {"amount": 250.0, "currency": "USD"}


def test_langgraph_safe_agent_passes_timeout_after_commit() -> None:
    """Test that IdempotentLangGraphAgent passes timeout_after_commit with zero duplicates."""
    agent = IdempotentLangGraphAgent()
    result = rb.test_agent("payment_timeout_after_commit", agent)

    assert result.passed is True
    assert result.verdict == "PASS"
    assert result.evaluation.actual_side_effects == 1
    assert result.evaluation.duplicate_effect_count == 0
    # Must have 2 events in ledger: attempt 1 (failed with timeout), attempt 2 (safely recovered)
    assert len(result.events) == 2
    assert result.events[0].idempotency_key_hash == result.events[1].idempotency_key_hash


def test_langgraph_naive_agent_fails_timeout_after_commit() -> None:
    """Test that NaiveLangGraphAgent generates a duplicate charge and fails evaluation."""
    agent = NaiveLangGraphAgent()
    result = rb.test_agent("payment_timeout_after_commit", agent)

    assert result.passed is False
    assert result.verdict == "GENUINE_FAILURE"
    assert result.evaluation.actual_side_effects == 2
    assert result.evaluation.duplicate_effect_count == 1
    # Different idempotency key hashes across attempts
    assert len(result.events) == 2
    assert result.events[0].idempotency_key_hash != result.events[1].idempotency_key_hash


def test_langgraph_custom_graph_factory() -> None:
    """Test passing a custom graph factory to LangGraphAdapter."""
    def custom_factory(tools: list[object]) -> object:
        return build_idempotent_langgraph_agent(tools, max_retries=2)

    adapter = LangGraphAdapter(graph_factory=custom_factory, agent_id="custom_langgraph_agent")
    result = rb.test_agent("payment_timeout_after_commit", adapter)

    assert result.passed is True
    assert result.agent_id == "custom_langgraph_agent"
