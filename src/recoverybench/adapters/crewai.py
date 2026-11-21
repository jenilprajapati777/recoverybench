"""CrewAI framework adapter for RecoveryBench.

Provides integration with CrewAI:
- Bridges RecoveryBench mock tools into CrewAI / LangChain BaseTool definitions.
- Executes CrewAI crews inside RecoveryBench's fault-injected sandbox.
- Tracks tool attempts, effect identities, idempotency keys, and recovery behavior.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolCaller,
)
from recoverybench.errors import ConfigurationError

# Optional CrewAI dependency detection
try:
    import crewai  # type: ignore[import-not-found]
    from crewai.tools import BaseTool as CrewAIBaseTool  # type: ignore[import-not-found]

    CREWAI_AVAILABLE = True
except ImportError:
    CREWAI_AVAILABLE = False
    CrewAIBaseTool = object


class CrewAIToolInput(BaseModel):
    """Input schema for bridged CrewAI tool calls."""

    model_config = {"extra": "allow"}
    amount: float | None = Field(default=None, description="Amount for financial transactions")
    currency: str | None = Field(default="USD", description="Currency code")
    idempotency_key: str | None = Field(default=None, description="Unique idempotency token")
    operation_id: str | None = Field(default=None, description="Logical operation identifier")
    params: dict[str, Any] | None = Field(default=None, description="Raw parameter dictionary")


class BridgedCrewAITool:
    """Standalone duck-typed CrewAI BaseTool wrapper for RecoveryBench tools."""

    def __init__(
        self,
        name: str,
        description: str,
        tool_call: ToolCaller,
        target_tool: str,
        target_action: str,
        args_schema: type[BaseModel] | None = None,
    ) -> None:
        self.name = name
        self.description = description
        self.tool_call = tool_call
        self.target_tool = target_tool
        self.target_action = target_action
        self.args_schema = args_schema or CrewAIToolInput

    def _run(self, *args: Any, **kwargs: Any) -> str:
        """Execute synchronous tool invocation through RecoveryBench tool caller."""
        # Unpack parameters
        params = kwargs.get("params") or {}
        if not params:
            # Flatten kwargs excluding protocol attributes
            params = {
                k: v
                for k, v in kwargs.items()
                if k not in ("idempotency_key", "operation_id")
            }

        idempotency_key = kwargs.get("idempotency_key")
        operation_id = kwargs.get("operation_id")

        try:
            result = self.tool_call(
                self.target_tool,
                self.target_action,
                params,
                idempotency_key=idempotency_key,
                operation_id=operation_id,
            )
        except TypeError:
            result = self.tool_call(
                self.target_tool,
                self.target_action,
                params,
                idempotency_key=idempotency_key,
            )

        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return str(result)

    def run(self, *args: Any, **kwargs: Any) -> str:
        """Public execution entry point conforming to CrewAI BaseTool protocol."""
        return self._run(*args, **kwargs)

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        return self._run(*args, **kwargs)


def create_crewai_tool(
    tool_name: str,
    action: str,
    tool_call: ToolCaller,
    *,
    name: str | None = None,
    description: str | None = None,
    args_schema: type[BaseModel] | None = None,
) -> Any:
    """Create a CrewAI-compatible tool bridging to a RecoveryBench ToolService."""
    t_name = name or f"{tool_name}_{action}"
    t_desc = description or f"RecoveryBench mock tool: {tool_name}.{action}"

    if CREWAI_AVAILABLE and hasattr(crewai, "tools") and hasattr(crewai.tools, "tool"):
        try:
            @crewai.tools.tool(t_name)  # type: ignore[untyped-decorator]
            def dynamic_tool(*args: Any, **kwargs: Any) -> str:
                wrapper = BridgedCrewAITool(
                    name=t_name,
                    description=t_desc,
                    tool_call=tool_call,
                    target_tool=tool_name,
                    target_action=action,
                    args_schema=args_schema,
                )
                return wrapper.run(*args, **kwargs)

            return dynamic_tool
        except Exception:
            pass

    return BridgedCrewAITool(
        name=t_name,
        description=t_desc,
        tool_call=tool_call,
        target_tool=tool_name,
        target_action=action,
        args_schema=args_schema,
    )


def create_crewai_payment_tool(tool_call: ToolCaller) -> Any:
    """Create a standard CrewAI payment charge tool."""
    return create_crewai_tool(
        tool_name="payment_gateway",
        action="charge",
        tool_call=tool_call,
        name="payment_gateway_charge",
        description="Process a credit card payment through the mock payment gateway.",
        args_schema=CrewAIToolInput,
    )


def parse_crew_output(crew_output: Any) -> tuple[bool, Any]:
    """Parse CrewOutput or raw string return into (success: bool, data: Any)."""
    if crew_output is None:
        return False, {"error": "Crew returned None"}

    # Extract raw text or pydantic data
    raw = getattr(crew_output, "raw", None)
    if raw is None and isinstance(crew_output, str):
        raw = crew_output

    json_dict = getattr(crew_output, "json_dict", None)
    pydantic_obj = getattr(crew_output, "pydantic", None)

    data = pydantic_obj or json_dict or raw or str(crew_output)

    # Detect failure keywords in raw output if not explicitly successful
    if isinstance(raw, str):
        raw_lower = raw.lower()
        if "error" in raw_lower or "failed" in raw_lower or "unable to" in raw_lower:
            # If it explicitly says failed without confirming completion
            if "success" not in raw_lower and "confirmed" not in raw_lower:
                return False, data

    return True, data


class CrewAIAdapter(AgentAdapter):
    """Adapter executing a CrewAI Crew against RecoveryBench."""

    def __init__(
        self,
        crew_factory: Callable[[list[Any]], Any] | Any,
        *,
        agent_id: str = "crewai_adapter",
        require_crewai: bool = False,
    ) -> None:
        if require_crewai and not CREWAI_AVAILABLE:
            raise ConfigurationError(
                "CrewAI is not installed. Install with 'pip install recoverybench[crewai]' "
                "or 'pip install crewai'."
            )
        self.agent_id = agent_id
        self.crew_factory = crew_factory

    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Execute the CrewAI crew synchronously."""
        # 1. Bridge tools
        tools = [create_crewai_payment_tool(tool_call)]

        # 2. Instantiate crew
        if callable(self.crew_factory):
            crew = self.crew_factory(tools)
        else:
            crew = self.crew_factory

        # 3. Kickoff crew
        inputs = task.task_params or {}
        try:
            if hasattr(crew, "kickoff"):
                output = crew.kickoff(inputs=inputs)
            elif callable(crew):
                output = crew(inputs)
            else:
                raise ConfigurationError(f"Target crew '{type(crew).__name__}' has no kickoff() method")

            success, data = parse_crew_output(output)
            status = CompletionStatus.SUCCEEDED if success else CompletionStatus.FAILED
            return AgentResult(success=success, status=status, data=data)

        except Exception as exc:
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"CrewAI execution failed: {type(exc).__name__}: {exc}",
            )

    def execute(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Alias for run() for compatibility."""
        return self.run(task, tool_call)

    async def run_async(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Execute the CrewAI crew asynchronously."""
        tools = [create_crewai_payment_tool(tool_call)]

        if callable(self.crew_factory):
            crew = self.crew_factory(tools)
        else:
            crew = self.crew_factory

        inputs = task.task_params or {}
        try:
            if hasattr(crew, "kickoff_async"):
                output = await crew.kickoff_async(inputs=inputs)
            elif hasattr(crew, "kickoff"):
                # Run sync kickoff in thread if no kickoff_async
                output = await asyncio.to_thread(crew.kickoff, inputs=inputs)
            elif callable(crew):
                res = crew(inputs)
                if inspect.isawaitable(res):
                    output = await res
                else:
                    output = res
            else:
                raise ConfigurationError(f"Target crew '{type(crew).__name__}' cannot be executed")

            success, data = parse_crew_output(output)
            status = CompletionStatus.SUCCEEDED if success else CompletionStatus.FAILED
            return AgentResult(success=success, status=status, data=data)

        except Exception as exc:
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"CrewAI async execution failed: {type(exc).__name__}: {exc}",
            )

    async def execute_async(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Alias for run_async() for compatibility."""
        return await self.run_async(task, tool_call)
