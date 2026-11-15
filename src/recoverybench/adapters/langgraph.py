"""LangGraph framework adapter for RecoveryBench.

Provides first-class integration with LangGraph (by LangChain):
- Bridges RecoveryBench mock tools into LangChain @tool definitions.
- Executes LangGraph StateGraph workflows inside the fault-injected sandbox.
- Evaluates recovery resilience, retry behavior, and idempotency key preservation.
"""

import json
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

try:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.tools import BaseTool, tool
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover
    LANGGRAPH_AVAILABLE = False
    BaseTool = Any  # type: ignore[misc, assignment]
    CompiledStateGraph = Any  # type: ignore[misc, assignment]
    add_messages = Any  # type: ignore
    tool = Any  # type: ignore
    AIMessage = Any  # type: ignore
    HumanMessage = Any  # type: ignore
    ToolMessage = Any  # type: ignore
    StateGraph = Any  # type: ignore
    END = Any  # type: ignore
    START = Any  # type: ignore
    ToolNode = Any  # type: ignore
    tools_condition = Any  # type: ignore

from recoverybench.agent.base import AgentAdapter, AgentResult, TaskContext, ToolCaller
from recoverybench.errors import ConfigurationError


def create_langchain_payment_tool(tool_call: ToolCaller) -> Any:
    """Create a LangChain @tool that dispatches to RecoveryBench's tool_call callback."""
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover
        raise ConfigurationError(
            "LangGraph is not installed. Install with 'pip install recoverybench[langgraph]' "
            "or 'pip install langgraph langchain-core'."
        )

    @tool
    def payment_gateway_charge(
        amount: float = 100.0,
        currency: str = "USD",
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        """Process a payment charge through the payment gateway.

        Args:
            amount: Monetary amount to charge.
            currency: Three-letter currency code (e.g., USD).
            idempotency_key: Unique client-side token to guarantee idempotency on retries.
            operation_id: Logical business operation identifier.
        """
        params = {"amount": amount, "currency": currency}
        try:
            result = tool_call("payment_gateway", "charge", params, idempotency_key, operation_id)
        except TypeError:
            result = tool_call("payment_gateway", "charge", params, idempotency_key)
        return json.dumps(result)

    return payment_gateway_charge


def bridge_tools(tool_call: ToolCaller, tool_names: list[str] | None = None) -> list[Any]:
    """Bridge RecoveryBench tools into a list of LangChain BaseTool instances."""
    # Currently creates payment gateway charge tool; can be extended for other tools
    return [create_langchain_payment_tool(tool_call)]


# --- State Definitions for Reference LangGraph Agents ---


class PaymentAgentState(TypedDict, total=False):
    """State schema for payment recovery graphs."""

    messages: Annotated[list[Any], add_messages]
    idempotency_key: str
    attempts: int
    charge_confirmed: bool


def build_naive_langgraph_agent(
    tools: list[Any],
    max_retries: int = 3,
) -> Any:
    """Construct a LangGraph agent that does NOT preserve idempotency keys on retry (Unsafe).

    When a tool returns an error (such as a timeout after commit), this agent retries
    with a newly generated idempotency key, triggering duplicate side effects in production.
    """
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover
        raise ConfigurationError("LangGraph is not installed.")

    def naive_agent_node(state: PaymentAgentState) -> dict[str, Any]:
        messages = state.get("messages", [])
        attempts = state.get("attempts", 0)
        last_msg = messages[-1] if messages else None

        # Check for tool failure
        if last_msg and isinstance(last_msg, ToolMessage) and getattr(last_msg, "status", "") == "error":
            attempts += 1
            if attempts > max_retries:
                return {
                    "messages": [AIMessage(content=f"Payment failed after {attempts} attempts.")],
                    "attempts": attempts,
                }
            # BUG (Naive retry): generates a brand new key for each retry attempt
            new_key = f"naive_idem_attempt_{attempts}"
            retry_tool_call = {
                "name": "payment_gateway_charge",
                "args": {"amount": 100.0, "currency": "USD", "idempotency_key": new_key},
                "id": f"call_naive_retry_{attempts}",
                "type": "tool_call",
            }
            return {
                "messages": [AIMessage(content="", tool_calls=[retry_tool_call])],
                "attempts": attempts,
            }

        # Check for tool success
        if last_msg and isinstance(last_msg, ToolMessage) and getattr(last_msg, "status", "") != "error":
            return {
                "messages": [AIMessage(content=f"Payment confirmed: {last_msg.content}")],
                "charge_confirmed": True,
                "attempts": attempts,
            }

        # Initial turn: invoke tool with initial key
        initial_key = "naive_initial_key"
        initial_tool_call = {
            "name": "payment_gateway_charge",
            "args": {"amount": 100.0, "currency": "USD", "idempotency_key": initial_key},
            "id": "call_naive_1",
            "type": "tool_call",
        }
        return {
            "messages": [AIMessage(content="", tool_calls=[initial_tool_call])],
            "idempotency_key": initial_key,
            "attempts": 1,
        }

    builder = StateGraph(PaymentAgentState)
    builder.add_node("agent", naive_agent_node)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": END})
    builder.add_edge("tools", "agent")
    return builder.compile()


def build_idempotent_langgraph_agent(
    tools: list[Any],
    max_retries: int = 3,
) -> Any:
    """Construct a LangGraph agent that preserves its idempotency key across retries (Safe).

    When a tool returns an error, this agent retrieves its stable idempotency key
    from the graph state, safely reconciling unknown transaction outcomes.
    """
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover
        raise ConfigurationError("LangGraph is not installed.")

    def idempotent_agent_node(state: PaymentAgentState) -> dict[str, Any]:
        messages = state.get("messages", [])
        attempts = state.get("attempts", 0)
        stable_key = state.get("idempotency_key", "lg_stable_idem_key_001")
        last_msg = messages[-1] if messages else None

        # Check for tool failure
        if last_msg and isinstance(last_msg, ToolMessage) and getattr(last_msg, "status", "") == "error":
            attempts += 1
            if attempts > max_retries:
                return {
                    "messages": [AIMessage(content=f"Payment failed after {attempts} attempts.")],
                    "attempts": attempts,
                }
            # RESILIENT: re-uses the exact same stable idempotency key from graph state
            retry_tool_call = {
                "name": "payment_gateway_charge",
                "args": {"amount": 100.0, "currency": "USD", "idempotency_key": stable_key},
                "id": f"call_safe_retry_{attempts}",
                "type": "tool_call",
            }
            return {
                "messages": [AIMessage(content="", tool_calls=[retry_tool_call])],
                "attempts": attempts,
            }

        # Check for tool success
        if last_msg and isinstance(last_msg, ToolMessage) and getattr(last_msg, "status", "") != "error":
            return {
                "messages": [AIMessage(content=f"Payment confirmed: {last_msg.content}")],
                "charge_confirmed": True,
                "attempts": attempts,
            }

        # Initial turn: establish stable key in state
        initial_tool_call = {
            "name": "payment_gateway_charge",
            "args": {"amount": 100.0, "currency": "USD", "idempotency_key": stable_key},
            "id": "call_safe_1",
            "type": "tool_call",
        }
        return {
            "messages": [AIMessage(content="", tool_calls=[initial_tool_call])],
            "idempotency_key": stable_key,
            "attempts": 1,
        }

    builder = StateGraph(PaymentAgentState)
    builder.add_node("agent", idempotent_agent_node)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": END})
    builder.add_edge("tools", "agent")
    return builder.compile()


class LangGraphAdapter(AgentAdapter):
    """Adapter bridging LangGraph CompiledStateGraph applications to RecoveryBench."""

    def __init__(
        self,
        graph_factory: Callable[[list[Any]], Any],
        agent_id: str = "langgraph_agent",
        input_factory: Callable[[TaskContext], dict[str, Any]] | None = None,
    ) -> None:
        if not LANGGRAPH_AVAILABLE:  # pragma: no cover
            raise ConfigurationError(
                "LangGraph is not installed. Install with 'pip install recoverybench[langgraph]'."
            )
        self.graph_factory = graph_factory
        self.agent_id = agent_id
        self.input_factory = input_factory

    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Bridge RecoveryBench tools into LangGraph and execute the graph workflow."""
        bridged_tools = bridge_tools(tool_call)
        compiled_graph = self.graph_factory(bridged_tools)

        # Build initial input state
        if self.input_factory:
            initial_state = self.input_factory(task)
        else:
            initial_state = {
                "messages": [HumanMessage(content=f"Process payment for scenario {task.scenario_id}")],
                "attempts": 0,
            }

        try:
            final_state = compiled_graph.invoke(initial_state)
            return self._parse_langgraph_result(final_state)
        except Exception as exc:
            return AgentResult(
                success=False,
                error=f"LangGraph execution error: {exc}",
                status_message=f"LangGraph execution crashed: {exc}",
            )

    def _parse_langgraph_result(self, state: dict[str, Any]) -> AgentResult:
        """Extract AgentResult metrics and status from the final LangGraph state."""
        messages = state.get("messages", [])
        attempts = int(state.get("attempts", 1))

        # Check for confirmed charge flag in state
        confirmed = bool(state.get("charge_confirmed", False))
        last_message = messages[-1] if messages else None
        last_content = getattr(last_message, "content", "") if last_message else ""

        # Determine success from messages if not explicitly flagged
        if not confirmed and last_message:
            confirmed = "confirmed" in str(last_content).lower() or "succeeded" in str(last_content).lower()

        # Count tool invocations from messages
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        if tool_messages and attempts <= 1:
            attempts = len(tool_messages)

        error_message = None
        if not confirmed:
            error_message = last_content or "LangGraph workflow terminated without confirmation"

        return AgentResult(
            success=confirmed,
            data={"final_state_keys": list(state.keys()), "message_count": len(messages)},
            error=error_message,
            attempts=max(1, attempts),
            status_message=last_content or ("Payment succeeded" if confirmed else "Payment failed"),
        )


class NaiveLangGraphAgent(LangGraphAdapter):
    """Prebuilt Naive LangGraph agent that duplicates side effects upon timeout retries."""

    def __init__(self, agent_id: str = "naive_langgraph_agent", max_retries: int = 3) -> None:
        super().__init__(
            graph_factory=lambda tools: build_naive_langgraph_agent(tools, max_retries=max_retries),
            agent_id=agent_id,
        )


class IdempotentLangGraphAgent(LangGraphAdapter):
    """Prebuilt Idempotent LangGraph agent that safely recovers without duplicating side effects."""

    def __init__(self, agent_id: str = "idempotent_langgraph_agent", max_retries: int = 3) -> None:
        super().__init__(
            graph_factory=lambda tools: build_idempotent_langgraph_agent(tools, max_retries=max_retries),
            agent_id=agent_id,
        )
