"""Agent adapter interfaces and execution context."""

from abc import ABC, abstractmethod
from collections.abc import Callable, Generator
from contextlib import contextmanager
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recoverybench.clock import VirtualClock


class TaskContext(BaseModel):
    """Context passed to the agent under test."""

    model_config = {"arbitrary_types_allowed": True}

    scenario_id: str
    task_params: dict[str, Any] = Field(default_factory=dict)
    clock: VirtualClock
    operation_id: str | None = None

    @property
    def is_single_operation(self) -> bool:
        """True if this context has a default single-operation boundary established."""
        return self.operation_id is not None


class CompletionStatus(StrEnum):
    """Explicit semantic status of agent task completion."""

    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class AgentResult(BaseModel):
    """Outcome reported by the agent after attempting task."""

    model_config = ConfigDict(extra="allow")

    success: bool = False
    status: CompletionStatus = CompletionStatus.UNKNOWN
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = Field(default=1, ge=1)
    status_message: str = Field(default="")

    @model_validator(mode="before")
    @classmethod
    def _reconcile_status_and_success(cls, data: Any) -> Any:
        if isinstance(data, dict):
            has_success = "success" in data
            raw_success = data.get("success")
            has_status = "status" in data and data["status"] is not None
            raw_status = data.get("status")

            # 1. Strictly validate success field if present (Task 2)
            success_val: bool | None = None
            if has_success:
                if raw_success is None:
                    data["status"] = CompletionStatus.UNKNOWN
                    data["success"] = False
                    if not data.get("error"):
                        data["error"] = "Null success field: success must be boolean True or False"
                    return data
                elif type(raw_success) is bool:
                    success_val = raw_success
                else:
                    data["status"] = CompletionStatus.UNKNOWN
                    data["success"] = False
                    if not data.get("error"):
                        data["error"] = (
                            f"Invalid success field: expected boolean, got {type(raw_success).__name__} ({raw_success!r})"
                        )
                    return data

            # 2. Strictly validate status field if present
            status_val: CompletionStatus | None = None
            if has_status:
                if isinstance(raw_status, CompletionStatus):
                    status_val = raw_status
                elif isinstance(raw_status, str):
                    upper_st = raw_status.strip().upper()
                    if upper_st in ("SUCCEEDED", "SUCCESS"):
                        status_val = CompletionStatus.SUCCEEDED
                    elif upper_st in ("FAILED", "FAILURE", "ERROR"):
                        status_val = CompletionStatus.FAILED
                    elif upper_st == "UNKNOWN":
                        status_val = CompletionStatus.UNKNOWN
                    else:
                        data["status"] = CompletionStatus.UNKNOWN
                        data["success"] = False
                        if not data.get("error"):
                            data["error"] = f"Invalid status field value: {raw_status!r}"
                        return data
                else:
                    data["status"] = CompletionStatus.UNKNOWN
                    data["success"] = False
                    if not data.get("error"):
                        data["error"] = (
                            f"Invalid status field type: expected string, got {type(raw_status).__name__} ({raw_status!r})"
                        )
                    return data

            # 3. Detect conflicting completion indicators (Task 3)
            if status_val is not None and success_val is not None:
                if status_val == CompletionStatus.SUCCEEDED and not success_val:
                    data["status"] = CompletionStatus.UNKNOWN
                    data["success"] = False
                    data["error"] = "Conflicting completion indicators: status='SUCCEEDED' conflicts with success=False"
                    return data
                if status_val == CompletionStatus.FAILED and success_val:
                    data["status"] = CompletionStatus.UNKNOWN
                    data["success"] = False
                    data["error"] = "Conflicting completion indicators: status='FAILED' conflicts with success=True"
                    return data
                if status_val == CompletionStatus.UNKNOWN and success_val:
                    data["status"] = CompletionStatus.UNKNOWN
                    data["success"] = False
                    data["error"] = "Conflicting completion indicators: status='UNKNOWN' conflicts with success=True"
                    return data
                # Agreement
                data["status"] = status_val
                data["success"] = (status_val == CompletionStatus.SUCCEEDED)
                return data

            # 4. Only status provided
            if status_val is not None:
                data["status"] = status_val
                data["success"] = (status_val == CompletionStatus.SUCCEEDED)
                return data

            # 5. Only success provided
            if success_val is not None:
                data["status"] = CompletionStatus.SUCCEEDED if success_val else CompletionStatus.FAILED
                data["success"] = success_val
                return data

            # 6. Neither provided
            data["status"] = CompletionStatus.UNKNOWN
            data["success"] = False
        return data

    @property
    def is_succeeded(self) -> bool:
        """True if the agent explicitly reported successful completion."""
        return self.status == CompletionStatus.SUCCEEDED

    @property
    def is_failed(self) -> bool:
        """True if the agent explicitly reported failure."""
        return self.status == CompletionStatus.FAILED

    @property
    def is_unknown(self) -> bool:
        """True if the agent return was ambiguous or lacked explicit completion evidence."""
        return self.status == CompletionStatus.UNKNOWN


class ToolClient(ABC):
    """Tool client interface providing tool dispatching to agents."""

    @abstractmethod
    def call(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch a tool call with explicit operation and idempotency identifiers."""
        raise NotImplementedError

    @abstractmethod
    def __call__(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Callable dispatch protocol for backwards compatibility."""
        raise NotImplementedError

    @contextmanager
    def operation(self, operation_id: str) -> Generator[str, None, None]:
        """Establish an active operation_id context scope for tool calls."""
        raise NotImplementedError

    @property
    def current_operation_id(self) -> str | None:
        """Return the currently active operation_id in scope, if any."""
        return None


# Type alias for tool invocation function or client provided to agents
ToolCaller = Callable[..., dict[str, Any]] | ToolClient


class AgentAdapter(ABC):
    """Base interface for agents evaluated by RecoveryBench."""

    agent_id: str

    @abstractmethod
    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Execute agent task logic using the provided tool caller."""
        raise NotImplementedError
