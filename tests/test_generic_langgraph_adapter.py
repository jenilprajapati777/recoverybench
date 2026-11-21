"""Automated integration tests for generic, domain-independent LangGraphAdapter (Workstream D)."""

from typing import Annotated, Any, TypedDict

import pytest

import recoverybench as rb
from recoverybench.adapters.langgraph import (
    IdempotentLangGraphAgent,
    LangGraphAdapter,
    NaiveLangGraphAgent,
    create_langchain_tool,
)

try:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.prebuilt import ToolNode, tools_condition

    LANGGRAPH_PRESENT = True
except ImportError:
    LANGGRAPH_PRESENT = False
    add_messages = None  # type: ignore[assignment]


pytestmark = pytest.mark.skipif(not LANGGRAPH_PRESENT, reason="LangGraph is not installed")


class OrderProcessingState(TypedDict, total=False):
    """Domain-independent state schema for an order processing graph."""
    messages: Annotated[list[Any], add_messages]
    order_id: str
    item_id: str
    status: str
    success: bool
    attempts: int


def build_generic_order_graph(tools: list[Any]) -> Any:
    """Build a non-payment LangGraph agent using generic order reservation tools."""
    def agent_node(state: OrderProcessingState) -> dict[str, Any]:
        messages = state.get("messages", [])
        attempts = state.get("attempts", 0) + 1
        last_msg = messages[-1] if messages else None

        # Check if tool completed
        if last_msg and isinstance(last_msg, ToolMessage) and getattr(last_msg, "status", "") != "error":
            return {
                "messages": [AIMessage(content="Order confirmed successfully.")],
                "status": "COMPLETED",
                "success": True,
                "attempts": attempts,
            }

        if attempts > 3:
            return {
                "messages": [AIMessage(content="Order failed after max attempts.")],
                "status": "FAILED",
                "success": False,
                "attempts": attempts,
            }

        # Issue order reservation tool call
        tool_call = {
            "name": "inventory_service_reserve",
            "args": {"sku": state.get("item_id", "item_123"), "qty": 1, "idempotency_key": "resv_key_1"},
            "id": f"call_resv_{attempts}",
            "type": "tool_call",
        }
        return {
            "messages": [AIMessage(content="", tool_calls=[tool_call])],
            "attempts": attempts,
        }

    builder = StateGraph(OrderProcessingState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": END})
    builder.add_edge("tools", "agent")
    return builder.compile()


def test_generic_non_payment_langgraph_agent() -> None:
    """Verify testing a completely non-payment LangGraph agent with custom tool bridging."""
    @rb.mock_tool(name="inventory_service", action="reserve", mutates=True)
    def reserve_tool(sku: str, qty: int = 1) -> dict[str, Any]:
        return {"reserved": True, "sku": sku, "qty": qty}

    def tools_factory(tool_caller: rb.ToolCaller) -> list[Any]:
        # Custom generic tool bridging using create_langchain_tool
        return [
            create_langchain_tool(
                tool_name="inventory_service",
                action="reserve",
                tool_call=tool_caller,
                description="Reserve stock for an inventory item.",
            )
        ]

    def input_factory(task: rb.TaskContext) -> dict[str, Any]:
        return {
            "messages": [HumanMessage(content="Reserve inventory item")],
            "order_id": "ord_9999",
            "item_id": "sku_premium_gadget",
            "attempts": 0,
        }

    adapter = LangGraphAdapter(
        graph_factory=build_generic_order_graph,
        agent_id="order_fulfillment_agent",
        tools_factory=tools_factory,
        input_factory=input_factory,
    )

    result = rb.test_agent(
        "payment_timeout_before_commit",
        adapter,
        custom_tools=reserve_tool,
    )

    assert result.passed is True
    assert result.verdict == "PASS"
    assert result.agent_id == "order_fulfillment_agent"


def test_custom_result_extractor_and_success_evaluator() -> None:
    """Verify custom result_extractor overrides default parsing."""
    def custom_extractor(state: dict[str, Any], task: rb.TaskContext) -> rb.AgentResult:
        # Custom business logic extraction
        is_done = state.get("status") == "COMPLETED"
        return rb.AgentResult(
            success=is_done,
            status=rb.CompletionStatus.SUCCEEDED if is_done else rb.CompletionStatus.FAILED,
            data={"extracted_order": state.get("order_id")},
            status_message="Custom extractor verified completion",
        )

    @rb.mock_tool(name="inventory_service", action="reserve", mutates=True)
    def reserve_tool(sku: str, qty: int = 1) -> dict[str, Any]:
        return {"reserved": True}

    adapter = LangGraphAdapter(
        graph_factory=build_generic_order_graph,
        agent_id="custom_extracted_agent",
        tools_factory=lambda tc: [create_langchain_tool("inventory_service", "reserve", tc)],
        input_factory=lambda task: {
            "messages": [HumanMessage(content="Order request")],
            "order_id": "ord_special_42",
            "item_id": "sku_xyz",
        },
        result_extractor=custom_extractor,
    )

    result = rb.test_agent(
        "payment_timeout_before_commit",
        adapter,
        custom_tools=reserve_tool,
    )

    assert result.passed is True
    assert result.agent_raw_result is not None
    raw_data = result.agent_raw_result.get("data")
    assert isinstance(raw_data, dict)
    assert raw_data.get("extracted_order") == "ord_special_42"


def test_async_langgraph_agent_execution() -> None:
    """Verify asynchronous LangGraph execution via ainvoke and test_agent_async()."""
    async def _test() -> None:
        @rb.mock_tool(name="inventory_service", action="reserve", mutates=True)
        def reserve_tool(sku: str, qty: int = 1) -> dict[str, Any]:
            return {"reserved": True}

        adapter = LangGraphAdapter(
            graph_factory=build_generic_order_graph,
            agent_id="async_langgraph_order_agent",
            tools_factory=lambda tc: [create_langchain_tool("inventory_service", "reserve", tc)],
            input_factory=lambda task: {
                "messages": [HumanMessage(content="Async order request")],
                "order_id": "ord_async_01",
                "item_id": "sku_async",
            },
        )

        result = await rb.test_agent_async(
            "payment_timeout_before_commit",
            adapter,
            custom_tools=reserve_tool,
        )

        assert result.passed is True
        assert result.verdict == "PASS"

    import asyncio
    asyncio.run(_test())


def test_payment_compatibility_intact() -> None:
    """Verify that existing IdempotentLangGraphAgent and NaiveLangGraphAgent continue to work."""
    safe_agent = IdempotentLangGraphAgent()
    safe_result = rb.test_agent("payment_timeout_after_commit", safe_agent)
    assert safe_result.passed is True
    assert safe_result.evaluation.duplicate_effect_count == 0

    naive_agent = NaiveLangGraphAgent()
    naive_result = rb.test_agent("payment_timeout_after_commit", naive_agent)
    assert naive_result.passed is False
    assert naive_result.evaluation.duplicate_effect_count == 1
