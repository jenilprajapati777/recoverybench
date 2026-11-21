"""Test tool implementations for process isolation and execution model verification."""

import os
import time
from typing import Any

from recoverybench.errors import ToolError
from recoverybench.models import (
    CapacityProvenanceTier,
    IdempotencyContract,
    IdempotencyGuarantee,
    ToolExecutionModel,
)
from recoverybench.tools.base import ToolResult, ToolService


class UncooperativePidTool(ToolService):
    """Uncooperative sync tool that returns its operating system PID."""

    def __init__(self, name: str = "pid_tool", timeout_seconds: float = 15.0) -> None:
        super().__init__()
        self.name = name
        self.execution_model = ToolExecutionModel.UNCOOPERATIVE_SYNC
        self.timeout_seconds = timeout_seconds

    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            success=True,
            data={"pid": os.getpid(), "parent_pid": os.getppid(), "action": action},
            committed=False,
        )

    def reset(self) -> None:
        pass


class UncooperativeHangingTool(ToolService):
    """Uncooperative sync tool that sleeps indefinitely and ignores cooperative cancellation."""

    def __init__(self, name: str = "hanging_tool", timeout_seconds: float = 0.1, sleep_duration: float = 30.0) -> None:
        super().__init__()
        self.name = name
        self.execution_model = ToolExecutionModel.UNCOOPERATIVE_SYNC
        self.timeout_seconds = timeout_seconds
        self.sleep_duration = sleep_duration

    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        time.sleep(self.sleep_duration)
        return ToolResult(success=True, data={"finished": True}, committed=True)

    def reset(self) -> None:
        pass


class UncooperativeErrorTool(ToolService):
    """Uncooperative sync tool that raises an error in the child process."""

    def __init__(self, name: str = "error_tool", timeout_seconds: float = 15.0) -> None:
        super().__init__()
        self.name = name
        self.execution_model = ToolExecutionModel.UNCOOPERATIVE_SYNC
        self.timeout_seconds = timeout_seconds

    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        err_msg = params.get("message", "Simulated uncooperative child failure")
        raise ToolError(err_msg)

    def reset(self) -> None:
        pass


class UncooperativeSuccessTool(ToolService):
    """Uncooperative sync tool that executes successfully and returns data."""

    def __init__(
        self,
        name: str = "success_tool",
        timeout_seconds: float = 15.0,
        committed: bool = True,
        effect_id: str | None = "eff_uncoop_01",
    ) -> None:
        super().__init__()
        self.name = name
        self.execution_model = ToolExecutionModel.UNCOOPERATIVE_SYNC
        self.timeout_seconds = timeout_seconds
        self.committed = committed
        self.effect_id = effect_id
        self.max_side_effect_capacity = 1
        self.capacity_provenance = CapacityProvenanceTier.TIER_C3
        self.idempotency_contract = IdempotencyContract(
            guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
            verified_service_deduplication=True,
        )

    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            success=True,
            data={"result": params.get("value", 42), "pid": os.getpid()},
            committed=self.committed,
            effect_id=self.effect_id,
        )

    def reset(self) -> None:
        pass
