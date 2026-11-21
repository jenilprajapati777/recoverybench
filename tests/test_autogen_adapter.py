"""Tests for AutoGen Framework Adapter (Phase 4.4)."""

import json
from typing import Any

import pytest

from recoverybench.adapters.autogen import (
    AutoGenAdapter,
    create_autogen_payment_tool,
    parse_chat_result,
)
from recoverybench.agent.base import AgentResult, CompletionStatus, TaskContext
from recoverybench.clock import VirtualClock
from recoverybench.errors import ConfigurationError
from recoverybench.sdk.tester import test_agent as rb_test_agent


class MockChatResult:
    """Mock representing AutoGen ChatResult return."""

    def __init__(
        self,
        summary: str = "Payment completed. Terminating conversation.",
        chat_history: list[dict[str, Any]] | None = None,
    ) -> None:
        self.summary = summary
        self.chat_history = chat_history or [
            {"role": "user", "content": "Please charge order 42 for $100"},
            {"role": "assistant", "content": "Charge processed successfully. Terminate."},
        ]
        self.cost = {"total_cost": 0.001}


class MockUserProxyAgent:
    """Mock AutoGen UserProxyAgent executing initiate_chat."""

    def __init__(self, tools: list[Any], behavior: str = "success") -> None:
        self.tools = tools
        self.behavior = behavior

    def initiate_chat(self, recipient: Any, message: str) -> MockChatResult:
        if self.behavior == "fail":
            raise ConnectionError("AutoGen message broker unavailable")

        # Invoke payment tool function
        payment_tool = self.tools[0]
        res = payment_tool(
            amount=100.0,
            currency="USD",
            customer_id="cust_autogen",
            idempotency_key="autogen_key_42",
            operation_id="op_autogen_42",
        )
        return MockChatResult(
            summary=f"Success: {res}",
            chat_history=[
                {"role": "user", "content": message},
                {"role": "assistant", "content": f"Result: {res}. Operation confirmed. Terminate."},
            ],
        )

    async def a_initiate_chat(self, recipient: Any, message: str) -> MockChatResult:
        return self.initiate_chat(recipient, message)


class MockAssistantAgent:
    """Mock AutoGen AssistantAgent pair counterpart."""
    pass


class TestAutoGenAdapter:
    """Comprehensive test suite for AutoGen integration adapter."""

    def test_missing_dependency_actionable_error(self) -> None:
        """Verify require_autogen=True raises actionable ConfigurationError if autogen is missing."""
        from recoverybench.adapters.autogen import AUTOGEN_AVAILABLE

        if not AUTOGEN_AVAILABLE:
            with pytest.raises(ConfigurationError) as exc_info:
                AutoGenAdapter(lambda tools: None, require_autogen=True)
            assert "AutoGen is not installed" in str(exc_info.value)
            assert "pip install recoverybench[autogen]" in str(exc_info.value)

    def test_bridged_tool_function(self) -> None:
        """Verify create_autogen_payment_tool generates a callable function dispatching to ToolCaller."""
        calls = []

        def mock_caller(tool_name: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            calls.append((tool_name, action, params, kwargs.get("idempotency_key")))
            return {"status": "succeeded", "tx_id": "tx_99"}

        fn = create_autogen_payment_tool(mock_caller)
        assert callable(fn)
        assert fn.__name__ == "payment_gateway_charge"

        result_str = fn(
            amount=250.0,
            currency="USD",
            customer_id="cust_auto",
            idempotency_key="key_auto_1",
            operation_id="op_auto_1",
        )
        assert len(calls) == 1
        assert calls[0][0] == "payment_gateway"
        assert calls[0][1] == "charge"
        assert calls[0][2]["amount"] == 250.0
        assert calls[0][3] == "key_auto_1"
        res_data = json.loads(result_str)
        assert res_data["status"] == "succeeded"

    def test_parse_chat_result(self) -> None:
        """Verify parse_chat_result analyzes chat history and termination keywords."""
        # Success with terminate keyword
        chat_ok = MockChatResult()
        success, data = parse_chat_result(chat_ok)
        assert success is True
        assert data["history_count"] == 2

        # Failure in last message
        chat_fail = MockChatResult(
            chat_history=[
                {"role": "user", "content": "Process refund"},
                {"role": "assistant", "content": "Transaction failed with unhandled error"},
            ]
        )
        success_f, _ = parse_chat_result(chat_fail)
        assert success_f is False

        # Raw string handling
        succ_raw, _ = parse_chat_result("Execution finished and confirmed")
        assert succ_raw is True

    def test_autogen_adapter_sync_execution(self) -> None:
        """Verify AutoGenAdapter executes (user_proxy, assistant) pair synchronously."""
        def agent_pair_factory(tools: list[Any]) -> tuple[MockUserProxyAgent, MockAssistantAgent]:
            return MockUserProxyAgent(tools, behavior="success"), MockAssistantAgent()

        adapter = AutoGenAdapter(agent_pair_factory)
        task = TaskContext(
            scenario_id="autogen_sync_test",
            task_params={"amount": 100.0},
            clock=VirtualClock(),
        )

        invocations = []
        def mock_call(tool_name: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            invocations.append((tool_name, action))
            return {"status": "succeeded"}

        result = adapter.execute(task, mock_call)
        assert isinstance(result, AgentResult)
        assert result.success is True
        assert result.status == CompletionStatus.SUCCEEDED
        assert len(invocations) == 1

    @pytest.mark.anyio
    async def test_autogen_adapter_async_execution(self) -> None:
        """Verify AutoGenAdapter executes a_initiate_chat asynchronously."""
        def agent_pair_factory(tools: list[Any]) -> tuple[MockUserProxyAgent, MockAssistantAgent]:
            return MockUserProxyAgent(tools, behavior="success"), MockAssistantAgent()

        adapter = AutoGenAdapter(agent_pair_factory)
        task = TaskContext(
            scenario_id="autogen_async_test",
            task_params={"amount": 75.0},
            clock=VirtualClock(),
        )

        result = await adapter.execute_async(task, lambda *args, **kwargs: {"status": "succeeded"})
        assert result.success is True
        assert result.status == CompletionStatus.SUCCEEDED

    def test_autogen_adapter_error_quarantine(self) -> None:
        """Verify AutoGen connection/runtime errors are mapped to AgentResult(FAILED)."""
        def failing_factory(tools: list[Any]) -> tuple[MockUserProxyAgent, MockAssistantAgent]:
            return MockUserProxyAgent(tools, behavior="fail"), MockAssistantAgent()

        adapter = AutoGenAdapter(failing_factory)
        task = TaskContext(
            scenario_id="autogen_fail_test",
            task_params={},
            clock=VirtualClock(),
        )

        result = adapter.execute(task, lambda *args, **kwargs: {})
        assert result.success is False
        assert result.status == CompletionStatus.FAILED
        assert "ConnectionError" in (result.error or "")

    def test_autogen_with_sdk_test_agent(self) -> None:
        """Verify AutoGenAdapter executes through public test_agent() harness."""
        def agent_pair_factory(tools: list[Any]) -> tuple[MockUserProxyAgent, MockAssistantAgent]:
            return MockUserProxyAgent(tools, behavior="success"), MockAssistantAgent()

        adapter = AutoGenAdapter(agent_pair_factory)
        run_res = rb_test_agent(
            scenario="payment_timeout_after_commit",
            agent=adapter,
        )
        assert run_res.run_id is not None
        assert len(run_res.events) > 0
