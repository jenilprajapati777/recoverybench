"""LangGraph framework adapter for RecoveryBench.

Provides first-class integration with LangGraph (by LangChain):
- Bridges RecoveryBench mock tools into LangChain @tool definitions.
- Executes LangGraph StateGraph workflows inside the fault-injected sandbox.
- Evaluates recovery resilience, retry behavior, and idempotency key preservation.
"""

import asyncio
import json
from collections.abc import Callable
from typing import Annotated, Any, TypedDict

from pydantic import BaseModel

try:
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langchain_core.tools import BaseTool, StructuredTool, tool
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages
    from langgraph.graph.state import CompiledStateGraph
    from langgraph.prebuilt import ToolNode, tools_condition

    LANGGRAPH_AVAILABLE = True
except ImportError:  # pragma: no cover
    LANGGRAPH_AVAILABLE = False
    BaseTool = Any  # type: ignore[misc, assignment]
    StructuredTool = Any  # type: ignore[misc, assignment]
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

from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolCaller,
)
from recoverybench.errors import ConfigurationError


class GenericToolInput(BaseModel):
    """Input schema allowing arbitrary keyword arguments for generic tools."""

    model_config = {"extra": "allow"}
    params: dict[str, Any] | None = None
    idempotency_key: str | None = None
    operation_id: str | None = None


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


def create_langchain_tool(
    tool_name: str,
    action: str,
    tool_call: ToolCaller,
    *,
    description: str | None = None,
    args_schema: type[BaseModel] | None = None,
) -> Any:
    """Create a generic LangChain tool that dispatches to RecoveryBench's tool_call callback."""
    if not LANGGRAPH_AVAILABLE:  # pragma: no cover
        raise ConfigurationError(
            "LangGraph is not installed. Install with 'pip install recoverybench[langgraph]'."
        )

    clean_name = f"{tool_name}_{action}".replace("-", "_")
    tool_desc = description or f"Execute {action} on {tool_name}."

    def _sync_run(**kwargs: Any) -> str:
        params = dict(kwargs.pop("params", None) or {})
        idempotency_key = kwargs.pop("idempotency_key", None)
        operation_id = kwargs.pop("operation_id", None)
        params.update(kwargs)
        try:
            result = tool_call(tool_name, action, params, idempotency_key, operation_id)
        except TypeError:
            result = tool_call(tool_name, action, params, idempotency_key)
        return result if isinstance(result, str) else json.dumps(result)

    async def _async_run(**kwargs: Any) -> str:
        params = dict(kwargs.pop("params", None) or {})
        idempotency_key = kwargs.pop("idempotency_key", None)
        operation_id = kwargs.pop("operation_id", None)
        params.update(kwargs)
        try:
            raw = tool_call(tool_name, action, params, idempotency_key, operation_id)
        except TypeError:
            raw = tool_call(tool_name, action, params, idempotency_key)
        if asyncio.iscoroutine(raw) or hasattr(raw, "__await__"):
            result = await raw
        else:
            result = raw
        return result if isinstance(result, str) else json.dumps(result)

    schema = args_schema or GenericToolInput
    return StructuredTool.from_function(
        func=_sync_run,
        coroutine=_async_run,
        name=clean_name,
        description=tool_desc,
        args_schema=schema,
    )


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
    """Adapter bridging LangGraph CompiledStateGraph applications to RecoveryBench.

    Supports domain-independent, configurable graph workflows:
    - graph_factory: callable accepting list[BaseTool] and returning a CompiledStateGraph,
      OR a pre-compiled CompiledStateGraph directly.
    - tools_factory: optional callable(tool_call: ToolCaller) -> list[BaseTool]. Defaults to bridge_tools.
    - input_factory: optional callable(task: TaskContext) -> dict[str, Any].
    - result_extractor: optional callable(final_state: dict[str, Any], task: TaskContext) -> AgentResult.
    - success_evaluator: optional callable(final_state: dict[str, Any]) -> bool.
    """

    def __init__(
        self,
        graph_factory: Callable[[list[Any]], Any] | Any,
        agent_id: str = "langgraph_agent",
        *,
        tools_factory: Callable[[ToolCaller], list[Any]] | None = None,
        input_factory: Callable[[TaskContext], dict[str, Any]] | None = None,
        result_extractor: Callable[[dict[str, Any], TaskContext], AgentResult] | None = None,
        success_evaluator: Callable[[dict[str, Any]], bool] | None = None,
    ) -> None:
        if not LANGGRAPH_AVAILABLE:  # pragma: no cover
            raise ConfigurationError(
                "LangGraph is not installed. Install with 'pip install recoverybench[langgraph]'."
            )
        self.graph_factory = graph_factory
        self.agent_id = agent_id
        self.tools_factory = tools_factory
        self.input_factory = input_factory
        self.result_extractor = result_extractor
        self.success_evaluator = success_evaluator

    def _resolve_graph(self, tool_call: ToolCaller) -> Any:
        if callable(self.graph_factory):
            tools = self.tools_factory(tool_call) if self.tools_factory else bridge_tools(tool_call)
            return self.graph_factory(tools)
        return self.graph_factory

    def _build_initial_state(self, task: TaskContext) -> dict[str, Any]:
        if self.input_factory:
            return self.input_factory(task)
        # Generic input state: provides task_params and standard message if HumanMessage available
        params = dict(task.task_params)
        state: dict[str, Any] = {
            "task": params,
            "scenario_id": task.scenario_id,
            "operation_id": task.operation_id,
            "attempts": 0,
        }
        if LANGGRAPH_AVAILABLE:
            state["messages"] = [HumanMessage(content=f"Execute task for scenario {task.scenario_id}")]
        return state

    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Synchronously execute the LangGraph workflow."""
        compiled_graph = self._resolve_graph(tool_call)
        initial_state = self._build_initial_state(task)

        try:
            final_state = compiled_graph.invoke(initial_state)
            return self._parse_langgraph_result(final_state, task)
        except Exception as exc:
            return AgentResult(
                success=False,
                error=f"LangGraph execution error: {exc}",
                status_message=f"LangGraph execution crashed: {exc}",
            )

    async def run_async(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Asynchronously execute the LangGraph workflow using ainvoke if available."""
        compiled_graph = self._resolve_graph(tool_call)
        initial_state = self._build_initial_state(task)

        try:
            if hasattr(compiled_graph, "ainvoke") and callable(compiled_graph.ainvoke):
                final_state = await compiled_graph.ainvoke(initial_state)
            else:
                final_state = compiled_graph.invoke(initial_state)
            return self._parse_langgraph_result(final_state, task)
        except Exception as exc:
            return AgentResult(
                success=False,
                error=f"LangGraph execution error: {exc}",
                status_message=f"LangGraph execution crashed: {exc}",
            )

    def _parse_langgraph_result(self, state: dict[str, Any], task: TaskContext) -> AgentResult:
        """Extract AgentResult metrics and status from the final LangGraph state."""
        # 1. Custom developer-provided result extractor
        if self.result_extractor:
            custom_res = self.result_extractor(state, task)
            if isinstance(custom_res, AgentResult):
                return custom_res
            if isinstance(custom_res, dict):
                return AgentResult(**custom_res)
            return AgentResult(success=bool(custom_res), data={"state": state})

        # 2. Custom developer-provided success evaluator
        if self.success_evaluator:
            success = bool(self.success_evaluator(state))
            return AgentResult(
                success=success,
                status=CompletionStatus.SUCCEEDED if success else CompletionStatus.FAILED,
                data={"final_state_keys": list(state.keys())},
                status_message="Evaluated by custom success_evaluator",
            )

        # 3. Generic State Inspection (Domain-Independent)
        # Check for explicit completion indicators in state
        if "success" in state and isinstance(state["success"], bool):
            success = state["success"]
            st = CompletionStatus.SUCCEEDED if success else CompletionStatus.FAILED
            return AgentResult(
                success=success,
                status=st,
                data=state,
                status_message=str(state.get("status_message", "Completed with state.success")),
            )

        if "status" in state and isinstance(state["status"], str):
            up = state["status"].strip().upper()
            if up in ("SUCCEEDED", "SUCCESS", "PASS", "COMPLETED"):
                return AgentResult(
                    success=True,
                    status=CompletionStatus.SUCCEEDED,
                    data=state,
                    status_message=f"Completed with status '{state['status']}'",
                )
            elif up in ("FAILED", "FAILURE", "ERROR"):
                return AgentResult(
                    success=False,
                    status=CompletionStatus.FAILED,
                    data=state,
                    status_message=f"Failed with status '{state['status']}'",
                )

        # 4. Backward-compatible reference fallback for payment agents
        if "charge_confirmed" in state:
            confirmed = bool(state["charge_confirmed"])
            messages = state.get("messages", [])
            last_message = messages[-1] if messages else None
            last_content = getattr(last_message, "content", "") if last_message else ""
            tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
            attempts = max(1, len(tool_messages) if tool_messages else int(state.get("attempts", 1)))
            return AgentResult(
                success=confirmed,
                data={"final_state_keys": list(state.keys()), "message_count": len(messages)},
                error=None if confirmed else (last_content or "Payment charge not confirmed"),
                attempts=attempts,
                status_message=last_content or ("Payment succeeded" if confirmed else "Payment failed"),
            )

        # 5. Default generic message inspection if state contains messages
        messages = state.get("messages", [])
        tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
        attempts = max(1, len(tool_messages) if tool_messages else int(state.get("attempts", 1)))
        last_message = messages[-1] if messages else None
        last_content = getattr(last_message, "content", "") if last_message else ""

        # If any tool message reported an unhandled error on the final turn, consider failed
        if last_message and isinstance(last_message, ToolMessage) and getattr(last_message, "status", "") == "error":
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"Graph ended with tool error: {last_content}",
                attempts=attempts,
                status_message=f"Tool error: {last_content}",
            )

        # Fallback when no explicit indicator is provided
        return AgentResult(
            success=False,
            status=CompletionStatus.UNKNOWN,
            data={"final_state_keys": list(state.keys()), "message_count": len(messages)},
            error="LangGraph state lacks explicit 'success', 'status', or custom result_extractor",
            attempts=attempts,
            status_message="Ambiguous graph completion: state lacks completion indicator",
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
