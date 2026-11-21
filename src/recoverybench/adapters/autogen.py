"""AutoGen framework adapter for RecoveryBench.

Provides integration with Microsoft AutoGen / AG2:
- Bridges RecoveryBench mock tools into AutoGen registerable Python functions.
- Executes AutoGen conversational workflows (UserProxyAgent, AssistantAgent, GroupChat).
- Extracts ChatResult, tracks tool calls in the ledger, and evaluates recovery resilience.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from typing import Any

from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolCaller,
)
from recoverybench.errors import ConfigurationError

# Optional AutoGen dependency detection
try:
    from autogen import ConversableAgent  # type: ignore[import-not-found]

    AUTOGEN_AVAILABLE = True
except ImportError:
    try:
        from ag2 import ConversableAgent  # type: ignore[import-not-found]

        AUTOGEN_AVAILABLE = True
    except ImportError:
        AUTOGEN_AVAILABLE = False
        ConversableAgent = object


def create_autogen_tool(
    tool_name: str,
    action: str,
    tool_call: ToolCaller,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Callable[..., str]:
    """Create a typed Python callable suitable for AutoGen tool registration."""
    fn_name = name or f"{tool_name}_{action}"
    fn_desc = description or f"RecoveryBench tool: {tool_name}.{action}"

    def bridged_tool_function(
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Execute RecoveryBench tool call from AutoGen agent."""
        params = kwargs.get("params") or kwargs
        try:
            result = tool_call(
                tool_name,
                action,
                params,
                idempotency_key=idempotency_key,
                operation_id=operation_id,
            )
        except TypeError:
            result = tool_call(
                tool_name,
                action,
                params,
                idempotency_key=idempotency_key,
            )

        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return str(result)

    bridged_tool_function.__name__ = fn_name
    bridged_tool_function.__doc__ = fn_desc
    return bridged_tool_function


def create_autogen_payment_tool(tool_call: ToolCaller) -> Callable[..., str]:
    """Create standard AutoGen payment charge function."""
    def payment_gateway_charge(
        amount: float = 100.0,
        currency: str = "USD",
        customer_id: str = "default_customer",
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> str:
        """Process a credit card payment charge through the payment gateway."""
        params = {"amount": amount, "currency": currency, "customer_id": customer_id}
        try:
            result = tool_call(
                "payment_gateway",
                "charge",
                params,
                idempotency_key=idempotency_key,
                operation_id=operation_id,
            )
        except TypeError:
            result = tool_call(
                "payment_gateway",
                "charge",
                params,
                idempotency_key=idempotency_key,
            )

        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return str(result)

    payment_gateway_charge.__name__ = "payment_gateway_charge"
    return payment_gateway_charge


def parse_chat_result(chat_result: Any) -> tuple[bool, Any]:
    """Parse AutoGen ChatResult, conversation list, or raw return into (success: bool, data: Any)."""
    if chat_result is None:
        return False, {"error": "AutoGen chat returned None"}

    summary = getattr(chat_result, "summary", None)
    history = getattr(chat_result, "chat_history", None)

    if summary is None and isinstance(chat_result, str):
        summary = chat_result
    if history is None and isinstance(chat_result, list):
        history = chat_result

    data: dict[str, Any] = {
        "summary": summary,
        "history_count": len(history) if history else 0,
        "cost": getattr(chat_result, "cost", {}),
    }

    # Analyze conversation history for tool execution confirmations or aborts
    if history:
        last_message = history[-1] if len(history) > 0 else {}
        content = last_message.get("content", "") if isinstance(last_message, dict) else str(last_message)
        content_lower = str(content).lower()
        if "terminate" in content_lower or "success" in content_lower or "confirmed" in content_lower:
            return True, data
        if "error" in content_lower or "failed" in content_lower or "abort" in content_lower:
            return False, data

    if summary and isinstance(summary, str):
        summary_lower = summary.lower()
        if "failed" in summary_lower or "error" in summary_lower:
            return False, data

    return True, data


class AutoGenAdapter(AgentAdapter):
    """Adapter executing an AutoGen agent pair or conversation against RecoveryBench."""

    def __init__(
        self,
        agent_factory: Callable[[list[Callable[..., Any]]], Any] | Any,
        *,
        agent_id: str = "autogen_adapter",
        message_prompt: str | None = None,
        require_autogen: bool = False,
    ) -> None:
        if require_autogen and not AUTOGEN_AVAILABLE:
            raise ConfigurationError(
                "AutoGen is not installed. Install with 'pip install recoverybench[autogen]' "
                "or 'pip install pyautogen autogen-agentchat'."
            )
        self.agent_id = agent_id
        self.agent_factory = agent_factory
        self.message_prompt = message_prompt

    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Execute AutoGen conversation synchronously."""
        tools = [create_autogen_payment_tool(tool_call)]

        # 1. Instantiate agent or agent pair
        if callable(self.agent_factory):
            runner_or_pair = self.agent_factory(tools)
        else:
            runner_or_pair = self.agent_factory

        # 2. Extract message prompt
        message = (
            self.message_prompt
            or task.task_params.get("message")
            or f"Execute task {task.scenario_id}: {json.dumps(task.task_params)}"
        )

        try:
            # Case A: Factory returned (user_proxy, assistant) pair
            if isinstance(runner_or_pair, (tuple, list)) and len(runner_or_pair) == 2:
                user_proxy, assistant = runner_or_pair
                chat_res = user_proxy.initiate_chat(assistant, message=message)
            # Case B: Object with initiate_chat method
            elif hasattr(runner_or_pair, "initiate_chat"):
                chat_res = runner_or_pair.initiate_chat(message=message)
            # Case C: Direct callable agent
            elif callable(runner_or_pair):
                chat_res = runner_or_pair(task.task_params)
            else:
                raise ConfigurationError(
                    f"Invalid AutoGen target: expected (user_proxy, assistant) pair or initiate_chat runner, got {type(runner_or_pair)}"
                )

            success, data = parse_chat_result(chat_res)
            status = CompletionStatus.SUCCEEDED if success else CompletionStatus.FAILED
            return AgentResult(success=success, status=status, data=data)

        except Exception as exc:
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"AutoGen execution failed: {type(exc).__name__}: {exc}",
            )

    def execute(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Alias for run() for compatibility."""
        return self.run(task, tool_call)

    async def run_async(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Execute AutoGen conversation asynchronously."""
        tools = [create_autogen_payment_tool(tool_call)]

        if callable(self.agent_factory):
            runner_or_pair = self.agent_factory(tools)
        else:
            runner_or_pair = self.agent_factory

        message = (
            self.message_prompt
            or task.task_params.get("message")
            or f"Execute task {task.scenario_id}: {json.dumps(task.task_params)}"
        )

        try:
            if isinstance(runner_or_pair, (tuple, list)) and len(runner_or_pair) == 2:
                user_proxy, assistant = runner_or_pair
                if hasattr(user_proxy, "a_initiate_chat"):
                    chat_res = await user_proxy.a_initiate_chat(assistant, message=message)
                else:
                    chat_res = await asyncio.to_thread(user_proxy.initiate_chat, assistant, message=message)
            elif hasattr(runner_or_pair, "a_initiate_chat"):
                chat_res = await runner_or_pair.a_initiate_chat(message=message)
            elif hasattr(runner_or_pair, "initiate_chat"):
                chat_res = await asyncio.to_thread(runner_or_pair.initiate_chat, message=message)
            elif callable(runner_or_pair):
                res = runner_or_pair(task.task_params)
                if inspect.isawaitable(res):
                    chat_res = await res
                else:
                    chat_res = res
            else:
                raise ConfigurationError(f"Invalid AutoGen async target: {type(runner_or_pair)}")

            success, data = parse_chat_result(chat_res)
            status = CompletionStatus.SUCCEEDED if success else CompletionStatus.FAILED
            return AgentResult(success=success, status=status, data=data)

        except Exception as exc:
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"AutoGen async execution failed: {type(exc).__name__}: {exc}",
            )

    async def execute_async(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Alias for run_async() for compatibility."""
        return await self.run_async(task, tool_call)
