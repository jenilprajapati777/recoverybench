"""Tests for CrewAI Framework Adapter (Phase 4.4)."""

import json
from typing import Any

import pytest

from recoverybench.adapters.crewai import (
    BridgedCrewAITool,
    CrewAIAdapter,
    create_crewai_payment_tool,
    parse_crew_output,
)
from recoverybench.agent.base import AgentResult, CompletionStatus, TaskContext
from recoverybench.clock import VirtualClock
from recoverybench.errors import ConfigurationError
from recoverybench.sdk.tester import test_agent as rb_test_agent


class MockCrewOutput:
    """Mock representing CrewAI CrewOutput return."""

    def __init__(
        self,
        raw: str = "Task completed successfully. Charge confirmed.",
        json_dict: dict[str, Any] | None = None,
    ) -> None:
        self.raw = raw
        self.json_dict = json_dict or {"status": "success", "confirmed": True}


class MockCrewAgent:
    """Mock CrewAI Crew implementing kickoff and kickoff_async."""

    def __init__(self, tools: list[Any], behavior: str = "success") -> None:
        self.tools = tools
        self.behavior = behavior

    def kickoff(self, inputs: dict[str, Any]) -> Any:
        if self.behavior == "fail":
            raise RuntimeError("Underlying LLM API rate limited")

        # Invoke payment tool
        payment_tool = self.tools[0]
        key = f"crew_key_{inputs.get('order_id', 1)}"
        tool_res = payment_tool._run(
            amount=inputs.get("amount", 50.0),
            currency="USD",
            customer_id="cust_crew_1",
            idempotency_key=key,
            operation_id="op_crew_1",
        )
        return MockCrewOutput(
            raw="Payment successfully processed: " + tool_res,
            json_dict={"tool_result": tool_res},
        )

    async def kickoff_async(self, inputs: dict[str, Any]) -> Any:
        return self.kickoff(inputs)


class TestCrewAIAdapter:
    """Comprehensive test suite for CrewAI integration adapter."""

    def test_missing_dependency_actionable_error(self) -> None:
        """Verify require_crewai=True raises actionable ConfigurationError if crewai is missing."""
        from recoverybench.adapters.crewai import CREWAI_AVAILABLE

        if not CREWAI_AVAILABLE:
            with pytest.raises(ConfigurationError) as exc_info:
                CrewAIAdapter(lambda tools: None, require_crewai=True)
            assert "CrewAI is not installed" in str(exc_info.value)
            assert "pip install recoverybench[crewai]" in str(exc_info.value)

    def test_bridged_tool_execution(self) -> None:
        """Verify create_crewai_payment_tool dispatches parameters to ToolCaller correctly."""
        calls: list[tuple[str, str, dict[str, Any], str | None]] = []

        def mock_tool_caller(
            tool_name: str,
            action: str,
            params: dict[str, Any],
            idempotency_key: str | None = None,
            operation_id: str | None = None,
        ) -> dict[str, Any]:
            calls.append((tool_name, action, params, idempotency_key))
            return {"status": "succeeded", "id": "ch_123"}

        tool = create_crewai_payment_tool(mock_tool_caller)
        assert isinstance(tool, BridgedCrewAITool)
        assert tool.name == "payment_gateway_charge"

        # Execute through CrewAI tool protocol
        res_str = tool._run(
            amount=150.0,
            currency="USD",
            idempotency_key="idemp_101",
        )
        assert len(calls) == 1
        assert calls[0][0] == "payment_gateway"
        assert calls[0][1] == "charge"
        assert calls[0][2]["amount"] == 150.0
        assert calls[0][3] == "idemp_101"
        res_dict = json.loads(res_str)
        assert res_dict["status"] == "succeeded"

    def test_parse_crew_output(self) -> None:
        """Verify parse_crew_output correctly extracts results and handles errors."""
        # Success output
        out_ok = MockCrewOutput(raw="Order fulfillment confirmed")
        success, data = parse_crew_output(out_ok)
        assert success is True
        assert data == {"status": "success", "confirmed": True}

        # Failure output
        out_err = MockCrewOutput(raw="Fatal Error: LLM connection timeout")
        success_err, data_err = parse_crew_output(out_err)
        assert success_err is False

        # Raw string
        success_str, data_str = parse_crew_output("Operation was successful and confirmed")
        assert success_str is True
        assert data_str == "Operation was successful and confirmed"

    def test_crewai_adapter_sync_execution(self) -> None:
        """Verify CrewAIAdapter executes crew synchronously and normalizes AgentResult."""
        invocations = []

        def mock_tool_call(tool_name: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            invocations.append((tool_name, action, params))
            return {"status": "succeeded", "charge_id": "ch_999"}

        adapter = CrewAIAdapter(lambda tools: MockCrewAgent(tools, behavior="success"))
        task = TaskContext(
            scenario_id="crewai_test",
            task_params={"order_id": 42, "amount": 88.0},
            clock=VirtualClock(),
        )

        result = adapter.execute(task, mock_tool_call)
        assert isinstance(result, AgentResult)
        assert result.success is True
        assert result.status == CompletionStatus.SUCCEEDED
        assert len(invocations) == 1
        assert invocations[0][0] == "payment_gateway"

    @pytest.mark.anyio
    async def test_crewai_adapter_async_execution(self) -> None:
        """Verify CrewAIAdapter executes crew asynchronously."""
        adapter = CrewAIAdapter(lambda tools: MockCrewAgent(tools, behavior="success"))
        task = TaskContext(
            scenario_id="crewai_async_test",
            task_params={"order_id": 7, "amount": 45.0},
            clock=VirtualClock(),
        )

        def mock_tool_call(tool_name: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            return {"status": "succeeded"}

        result = await adapter.execute_async(task, mock_tool_call)
        assert result.success is True
        assert result.status == CompletionStatus.SUCCEEDED

    def test_crewai_adapter_error_quarantine(self) -> None:
        """Verify runtime exceptions inside crew execution are quarantined into AgentResult(FAILED)."""
        adapter = CrewAIAdapter(lambda tools: MockCrewAgent(tools, behavior="fail"))
        task = TaskContext(
            scenario_id="crewai_fail_test",
            task_params={},
            clock=VirtualClock(),
        )

        result = adapter.execute(task, lambda *args, **kwargs: {})
        assert result.success is False
        assert result.status == CompletionStatus.FAILED
        assert "RuntimeError" in (result.error or "")

    def test_crewai_with_sdk_test_agent(self) -> None:
        """Verify CrewAIAdapter runs end-to-end through test_agent() public entry point."""
        adapter = CrewAIAdapter(lambda tools: MockCrewAgent(tools, behavior="success"))
        run_res = rb_test_agent(
            scenario="payment_timeout_after_commit",
            agent=adapter,
        )
        assert run_res.run_id is not None
        # Verify ledger has records
        assert len(run_res.events) > 0
        assert any(e.tool_name == "payment_gateway" for e in run_res.events)
