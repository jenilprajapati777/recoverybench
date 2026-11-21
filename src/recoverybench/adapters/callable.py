"""Generic callable adapter allowing arbitrary Python functions to be tested in RecoveryBench."""

import inspect
from collections.abc import Callable
from typing import Any, TypeVar, overload

from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolCaller,
)
from recoverybench.errors import (
    CircuitBreakerError,
    ConfigurationError,
    InfrastructureError,
    ToolError,
)
from recoverybench.security import mask_string_secrets


class CallableAgentAdapter(AgentAdapter):
    """Adapts arbitrary Python callables (functions, methods, lambdas) to the AgentAdapter protocol.

    Normalizes inputs:
    - (task: TaskContext, tool_call: ToolCaller) -> AgentResult
    - (params: dict, tool_call: ToolCaller) -> AgentResult
    - (tool_call: ToolCaller) -> AgentResult

    Normalizes return values:
    - AgentResult: returned as-is
    - bool: converted to AgentResult(success=val)
    - dict: converted to AgentResult(success=val.get("success", True), data=val)
    - None: converted to AgentResult(success=True)

    Safely handles exceptions raised by the callable by converting them into
    AgentResult(success=False, error=str(exc)).
    """

    def __init__(
        self,
        target_callable: Callable[..., Any],
        agent_id: str | None = None,
    ) -> None:
        self.target_callable = target_callable
        raw_name = getattr(target_callable, "__name__", None) or "callable_agent"
        self.agent_id = str(agent_id if agent_id is not None else raw_name)
        self._param_count = self._detect_param_count(target_callable)

    def _detect_param_count(self, fn: Callable[..., Any]) -> int:
        """Inspect callable signature to determine expected argument count."""
        try:
            sig = inspect.signature(fn)
            params = [
                p
                for p in sig.parameters.values()
                if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
            ]
            return len(params)
        except (ValueError, TypeError):
            return 2

    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Execute the wrapped callable synchronously and normalize the result."""
        if inspect.iscoroutinefunction(self.target_callable):
            raise ConfigurationError(
                f"Callable '{self.agent_id}' is an asynchronous function ('async def'). "
                f"Asynchronous agents must be tested using 'await test_agent_async(...)' "
                f"or 'run_async()' rather than synchronous 'test_agent()'."
            )

        try:
            if self._param_count == 1:
                raw_result = self.target_callable(tool_call)
            elif self._param_count == 2:
                # Check if first param expects dict or TaskContext
                try:
                    sig = inspect.signature(self.target_callable)
                    first_param = list(sig.parameters.values())[0]
                    if first_param.annotation is dict or first_param.name in ("params", "task_params", "input_data"):
                        raw_result = self.target_callable(task.task_params, tool_call)
                    else:
                        raw_result = self.target_callable(task, tool_call)
                except Exception:
                    raw_result = self.target_callable(task, tool_call)
            else:
                # Fallback: pass both task and tool_call
                raw_result = self.target_callable(task, tool_call)

            if inspect.iscoroutine(raw_result):
                raw_result.close()
                raise ConfigurationError(
                    f"Callable '{self.agent_id}' returned an unawaited coroutine. "
                    "Use 'await test_agent_async(...)' to test asynchronous agents."
                )

            return self._normalize_result(raw_result)
        except (InfrastructureError, ConfigurationError, CircuitBreakerError):
            # RecoveryBench infrastructure exceptions must propagate!
            raise
        except ToolError as exc:
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"{type(exc).__name__}: {mask_string_secrets(str(exc))}",
                status_message=f"Agent unhandled tool error: {type(exc).__name__}",
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"{type(exc).__name__}: {mask_string_secrets(str(exc))}",
                status_message=f"Agent raised exception: {type(exc).__name__}",
            )

    async def run_async(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Asynchronously execute the wrapped callable and normalize the result."""
        try:
            # Determine argument dispatch
            dispatch_arg: Any
            if self._param_count == 1:
                # Single argument is tool_call
                if inspect.iscoroutinefunction(self.target_callable):
                    raw_result = await self.target_callable(tool_call)
                else:
                    raw_result = self.target_callable(tool_call)
            elif self._param_count == 2:
                # Two arguments: (task/params, tool_call)
                try:
                    sig = inspect.signature(self.target_callable)
                    first_param = list(sig.parameters.values())[0]
                    if first_param.annotation is dict or first_param.name in ("params", "task_params", "input_data"):
                        dispatch_arg = task.task_params
                    else:
                        dispatch_arg = task
                except Exception:
                    dispatch_arg = task

                if inspect.iscoroutinefunction(self.target_callable):
                    raw_result = await self.target_callable(dispatch_arg, tool_call)
                else:
                    raw_result = self.target_callable(dispatch_arg, tool_call)
            else:
                # Fallback: pass (task, tool_call)
                if inspect.iscoroutinefunction(self.target_callable):
                    raw_result = await self.target_callable(task, tool_call)
                else:
                    raw_result = self.target_callable(task, tool_call)

            # If the function was a standard function that returned a coroutine, await it
            if inspect.iscoroutine(raw_result):
                raw_result = await raw_result

            return self._normalize_result(raw_result)
        except (InfrastructureError, ConfigurationError, CircuitBreakerError):
            raise
        except ToolError as exc:
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"{type(exc).__name__}: {mask_string_secrets(str(exc))}",
                status_message=f"Agent unhandled tool error: {type(exc).__name__}",
            )
        except Exception as exc:
            return AgentResult(
                success=False,
                status=CompletionStatus.FAILED,
                error=f"{type(exc).__name__}: {mask_string_secrets(str(exc))}",
                status_message=f"Agent raised exception: {type(exc).__name__}",
            )

    def _normalize_result(self, raw_result: Any) -> AgentResult:
        """Normalize varying return types into a valid AgentResult.

        Applies strict completion state rules (Task 2 & Task 3):
        - AgentResult: Preserves explicit status/success as declared.
        - bool True: Normalized to SUCCEEDED (success=True).
        - bool False: Normalized to FAILED (success=False).
        - None: Normalized to UNKNOWN (success=False). Never silently assumed as success.
        - dict: Validated strictly via AgentResult reconciler; detects conflicts and strict booleans.
        - Unsupported types (int, str, list, etc.): Normalized to UNKNOWN with diagnostics.
        """
        if isinstance(raw_result, AgentResult):
            return raw_result

        # Exact boolean type check (Task 2: never coerce 0/1 or strings to bool)
        if type(raw_result) is bool:
            st = CompletionStatus.SUCCEEDED if raw_result else CompletionStatus.FAILED
            return AgentResult(
                success=raw_result,
                status=st,
                status_message="Completed with boolean True" if raw_result else "Failed with boolean False",
            )

        if raw_result is None:
            return AgentResult(
                success=False,
                status=CompletionStatus.UNKNOWN,
                error="Agent returned None without explicit completion status",
                status_message="Ambiguous completion: returned None",
            )

        if isinstance(raw_result, dict):
            data = dict(raw_result)
            attempts = int(data.get("attempts", 1))
            error = str(data.get("error")) if "error" in data and data["error"] is not None else None
            status_message = str(data.get("status_message", data.get("message", "")))

            # Instantiate AgentResult which invokes strict boolean validation and conflict detection
            res = AgentResult(**data)
            res.data = data
            res.attempts = max(1, attempts)
            if error and not res.error:
                res.error = error
            if status_message and not res.status_message:
                res.status_message = status_message
            if "success" not in data and "status" not in data:
                res.error = res.error or "Dictionary result lacks explicit 'success' or 'status' completion indicator"
                res.status_message = res.status_message or "Ambiguous completion: dict missing completion indicator"
            return res

        # Fallback for unsupported scalar or object returns (e.g. int, str, list)
        return AgentResult(
            success=False,
            status=CompletionStatus.UNKNOWN,
            data={"unsupported_result": raw_result, "type": type(raw_result).__name__},
            error=f"Unsupported agent return type: {type(raw_result).__name__} ({raw_result!r})",
            status_message=f"Ambiguous completion: unsupported return type {type(raw_result).__name__}",
        )


def wrap_agent(
    target_callable: Callable[..., Any] | AgentAdapter,
    agent_id: str | None = None,
) -> AgentAdapter:
    """Wrap any callable into an AgentAdapter."""
    if isinstance(target_callable, AgentAdapter):
        return target_callable
    return CallableAgentAdapter(target_callable, agent_id=agent_id)


F = TypeVar("F", bound=Callable[..., Any])


@overload
def agent(target_callable: F) -> CallableAgentAdapter: ...


@overload
def agent(*, id: str | None = None) -> Callable[[F], CallableAgentAdapter]: ...


def agent(
    target_callable: Callable[..., Any] | None = None,
    *,
    id: str | None = None,
) -> Any:
    """Decorator to mark a Python function as a RecoveryBench agent.

    Usage:
        @agent
        def my_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
            ...

        @agent(id="resilient_payment_agent")
        def custom_agent(params: dict, tool_call: ToolCaller):
            ...
    """
    def decorator(fn: Callable[..., Any]) -> CallableAgentAdapter:
        return CallableAgentAdapter(fn, agent_id=id)

    if target_callable is not None:
        return decorator(target_callable)
    return decorator
