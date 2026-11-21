"""Parent-supervised OS-level process runner for UNCOOPERATIVE_SYNC tools.

Implements ADR-004 Section 6.4 and Phase 4.2 mandatory process-isolation requirements:
- Executes uncooperative synchronous tool handlers in a dedicated child OS process.
- Enforces aggregate handler deadlines independently of tool cooperation.
- Employs spawn-compatible multiprocessing architecture across Windows and POSIX.
- Performs hard OS process termination (TerminateProcess / SIGKILL) upon timeout.
- Confirms child process exit at the OS level before reporting containment.
- Fails closed if serialization fails or if termination cannot be confirmed.
"""

import asyncio
import builtins
import multiprocessing as mp
import os
import pickle
import time
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

import recoverybench.errors as errors_module
from recoverybench.errors import (
    ConfigurationError,
    InfrastructureError,
    ProcessContainmentError,
    ProcessTerminatedError,
    ToolError,
)
from recoverybench.tools.base import ToolResult, ToolService


class ProcessExecutionResult(BaseModel):
    """Result of parent-supervised tool process execution."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    success: bool
    tool_result: ToolResult | None = None
    child_pid: int | None = None
    parent_pid: int = Field(default_factory=os.getpid)
    terminated: bool = False
    exit_code: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    duration_ms: float = 0.0


def _child_process_worker(
    child_conn: Any,
    tool_bytes: bytes,
    action: str,
    params: dict[str, Any],
    idempotency_key: str | None,
    operation_id: str | None,
    attempt_id: str | None,
) -> None:
    """Entry point executed inside the spawned child OS process."""
    my_pid = os.getpid()
    try:
        # 1. Deserialize tool instance
        try:
            tool: ToolService = pickle.loads(tool_bytes)
        except Exception as exc:
            child_conn.send({
                "status": "DESERIALIZATION_ERROR",
                "error_type": type(exc).__name__,
                "error_message": f"Failed to deserialize tool in child process: {exc}",
                "child_pid": my_pid,
            })
            return

        # 2. Execute requested action against mock tool
        try:
            result = tool.execute(
                action=action,
                params=params,
                idempotency_key=idempotency_key,
                operation_id=operation_id,
                attempt_id=attempt_id,
            )
        except Exception as exc:
            child_conn.send({
                "status": "TOOL_ERROR",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "child_pid": my_pid,
                "details": getattr(exc, "details", {}),
            })
            return

        # 3. Serialize ToolResult
        try:
            res_bytes = pickle.dumps(result)
        except Exception as exc:
            child_conn.send({
                "status": "SERIALIZATION_ERROR",
                "error_type": type(exc).__name__,
                "error_message": f"ToolResult from action '{action}' is not serializable: {exc}",
                "child_pid": my_pid,
            })
            return

        # 4. Serialize updated tool state for parent state synchronization
        try:
            tool_state_bytes = pickle.dumps(tool)
        except Exception:
            tool_state_bytes = None

        # 5. Send success response back to parent
        child_conn.send({
            "status": "SUCCESS",
            "result_bytes": res_bytes,
            "tool_state_bytes": tool_state_bytes,
            "child_pid": my_pid,
        })
    except Exception as exc:
        try:
            child_conn.send({
                "status": "FATAL_CHILD_ERROR",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "child_pid": my_pid,
            })
        except Exception:
            pass
    finally:
        try:
            child_conn.close()
        except Exception:
            pass
        os._exit(0)


class ToolProcessRunner:
    """Parent-supervised process execution runner for UNCOOPERATIVE_SYNC tools."""

    def __init__(self, default_timeout_seconds: float = 10.0) -> None:
        self.default_timeout_seconds = default_timeout_seconds
        # Always use spawn context for deterministic Windows & POSIX isolation
        self._ctx = mp.get_context("spawn")

    def _validate_serialization(
        self, tool: ToolService, action: str, params: dict[str, Any]
    ) -> bytes:
        """Validate serialization of tool and parameters; fail closed if unpickleable."""
        tool_name = getattr(tool, "name", type(tool).__name__)
        try:
            tool_bytes = pickle.dumps(tool)
        except Exception as exc:
            raise ConfigurationError(
                f"Tool '{tool_name}' cannot be serialized for isolated process execution: {exc}. "
                "Uncooperative tools require serializable tool instances and handlers."
            ) from exc

        try:
            pickle.dumps(params)
        except Exception as exc:
            raise ConfigurationError(
                f"Parameters for tool '{tool_name}' action '{action}' cannot be serialized: {exc}. "
                "Uncooperative tools require serializable invocation parameters."
            ) from exc

        return tool_bytes

    def _terminate_child(self, proc: Any, timeout: float = 1.0) -> bool:
        """Forcefully terminate child process and verify exit using OS API."""
        try:
            proc.terminate()
        except Exception:
            pass

        proc.join(timeout=timeout)

        if proc.is_alive():
            try:
                proc.kill()
            except Exception:
                pass
            proc.join(timeout=timeout)

        # OS verification: proc.is_alive() queries OS process table / handle state
        return not proc.is_alive()

    def _handle_child_message(
        self,
        msg: dict[str, Any],
        tool: ToolService,
        child_pid: int,
        start_time: float,
        exit_code: int | None,
        raise_on_error: bool,
    ) -> ProcessExecutionResult:
        """Process structured message received from child process."""
        duration_ms = (time.perf_counter() - start_time) * 1000
        status = msg.get("status")

        if status == "SUCCESS":
            tool_result = pickle.loads(msg["result_bytes"])
            # Synchronize state back to parent tool if available
            if msg.get("tool_state_bytes"):
                try:
                    updated_tool = pickle.loads(msg["tool_state_bytes"])
                    tool.__dict__.update(updated_tool.__dict__)
                except Exception:
                    pass
            return ProcessExecutionResult(
                success=True,
                tool_result=tool_result,
                child_pid=child_pid,
                parent_pid=os.getpid(),
                terminated=False,
                exit_code=exit_code,
                duration_ms=duration_ms,
            )

        if status == "TOOL_ERROR":
            err_type = msg.get("error_type", "ToolError")
            err_msg = msg.get("error_message", "Tool execution error")
            details = msg.get("details", {})
            if raise_on_error:
                exc_cls = (
                    getattr(errors_module, err_type, None)
                    or getattr(builtins, err_type, None)
                    or ToolError
                )
                if issubclass(exc_cls, ToolError):
                    raise exc_cls(err_msg, details=details)
                try:
                    raise exc_cls(err_msg)
                except Exception:
                    raise ToolError(f"{err_type}: {err_msg}", details=details) from None

            return ProcessExecutionResult(
                success=False,
                child_pid=child_pid,
                parent_pid=os.getpid(),
                terminated=False,
                exit_code=exit_code,
                error_type=err_type,
                error_message=err_msg,
                duration_ms=duration_ms,
            )

        if status in ("SERIALIZATION_ERROR", "DESERIALIZATION_ERROR"):
            err_msg = msg.get("error_message", "IPC serialization failure")
            raise ConfigurationError(err_msg)

        # Unexpected or fatal child error
        err_msg = msg.get("error_message", "Fatal error in child tool process")
        if raise_on_error:
            raise InfrastructureError(err_msg)
        return ProcessExecutionResult(
            success=False,
            child_pid=child_pid,
            parent_pid=os.getpid(),
            terminated=False,
            exit_code=exit_code,
            error_type="FatalChildError",
            error_message=err_msg,
            duration_ms=duration_ms,
        )

    def execute(
        self,
        tool: ToolService,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
        timeout_seconds: float | None = None,
        raise_on_error: bool = True,
    ) -> ProcessExecutionResult:
        """Synchronously execute tool handler in an isolated child OS process."""
        tool_bytes = self._validate_serialization(tool, action, params)
        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else getattr(tool, "timeout_seconds", self.default_timeout_seconds)
        )

        parent_conn, child_conn = self._ctx.Pipe(duplex=False)
        proc = self._ctx.Process(
            target=_child_process_worker,
            args=(
                child_conn,
                tool_bytes,
                action,
                params,
                idempotency_key,
                operation_id,
                attempt_id,
            ),
        )

        start_time = time.perf_counter()
        child_pid: int | None = None

        try:
            proc.start()
            child_pid = proc.pid
            # Close child connection in parent process
            child_conn.close()

            # Poll pipe within the aggregate deadline
            has_data = parent_conn.poll(timeout=effective_timeout)

            if has_data:
                try:
                    msg = parent_conn.recv()
                except EOFError:
                    msg = None

                proc.join(timeout=5.0)
                exit_code = proc.exitcode

                if msg is not None:
                    return self._handle_child_message(
                        msg=msg,
                        tool=tool,
                        child_pid=child_pid or 0,
                        start_time=start_time,
                        exit_code=exit_code,
                        raise_on_error=raise_on_error,
                    )

                if exit_code != 0:
                    err = f"Child process PID {child_pid} terminated abruptly with exit code {exit_code}"
                    if raise_on_error:
                        raise InfrastructureError(err)
                    return ProcessExecutionResult(
                        success=False,
                        child_pid=child_pid,
                        parent_pid=os.getpid(),
                        terminated=False,
                        exit_code=exit_code,
                        error_type="AbruptChildExitError",
                        error_message=err,
                        duration_ms=(time.perf_counter() - start_time) * 1000,
                    )

            # Deadline exceeded: enforce hard OS termination
            confirmed_dead = self._terminate_child(proc, timeout=1.0)
            duration_ms = (time.perf_counter() - start_time) * 1000
            exit_code = proc.exitcode

            if not confirmed_dead:
                raise ProcessContainmentError(
                    f"Child process PID {child_pid} exceeded deadline ({effective_timeout}s) "
                    "and could NOT be confirmed terminated by the OS.",
                    child_pid=child_pid,
                )

            tool_name = getattr(tool, "name", type(tool).__name__)
            err_msg = (
                f"Tool '{tool_name}.{action}' child process PID {child_pid} "
                f"exceeded deadline ({effective_timeout}s) and was terminated by parent."
            )

            if raise_on_error:
                raise ProcessTerminatedError(err_msg, child_pid=child_pid)

            return ProcessExecutionResult(
                success=False,
                tool_result=None,
                child_pid=child_pid,
                parent_pid=os.getpid(),
                terminated=True,
                exit_code=exit_code,
                error_type="ProcessTerminatedError",
                error_message=err_msg,
                duration_ms=duration_ms,
            )
        finally:
            try:
                parent_conn.close()
            except Exception:
                pass
            try:
                proc.close()
            except Exception:
                pass

    async def execute_async(
        self,
        tool: ToolService,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
        timeout_seconds: float | None = None,
        raise_on_error: bool = True,
    ) -> ProcessExecutionResult:
        """Asynchronously execute tool handler in child OS process without host thread pool."""
        tool_bytes = self._validate_serialization(tool, action, params)
        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else getattr(tool, "timeout_seconds", self.default_timeout_seconds)
        )

        parent_conn, child_conn = self._ctx.Pipe(duplex=False)
        proc = self._ctx.Process(
            target=_child_process_worker,
            args=(
                child_conn,
                tool_bytes,
                action,
                params,
                idempotency_key,
                operation_id,
                attempt_id,
            ),
        )

        start_time = time.perf_counter()
        child_pid: int | None = None

        try:
            proc.start()
            child_pid = proc.pid
            child_conn.close()

            # Asynchronous event loop poll loop without host worker threads
            data_ready = False
            poll_interval = 0.02
            while (time.perf_counter() - start_time) < effective_timeout:
                if parent_conn.poll(0):
                    data_ready = True
                    break
                await asyncio.sleep(poll_interval)

            if data_ready:
                try:
                    msg = parent_conn.recv()
                except EOFError:
                    msg = None

                proc.join(timeout=5.0)
                exit_code = proc.exitcode

                if msg is not None:
                    return self._handle_child_message(
                        msg=msg,
                        tool=tool,
                        child_pid=child_pid or 0,
                        start_time=start_time,
                        exit_code=exit_code,
                        raise_on_error=raise_on_error,
                    )

                if exit_code != 0:
                    err = f"Child process PID {child_pid} terminated abruptly with exit code {exit_code}"
                    if raise_on_error:
                        raise InfrastructureError(err)
                    return ProcessExecutionResult(
                        success=False,
                        child_pid=child_pid,
                        parent_pid=os.getpid(),
                        terminated=False,
                        exit_code=exit_code,
                        error_type="AbruptChildExitError",
                        error_message=err,
                        duration_ms=(time.perf_counter() - start_time) * 1000,
                    )

            # Deadline exceeded: hard OS termination
            confirmed_dead = self._terminate_child(proc, timeout=1.0)
            duration_ms = (time.perf_counter() - start_time) * 1000
            exit_code = proc.exitcode

            if not confirmed_dead:
                raise ProcessContainmentError(
                    f"Child process PID {child_pid} exceeded deadline ({effective_timeout}s) "
                    "and could NOT be confirmed terminated by the OS.",
                    child_pid=child_pid,
                )

            tool_name = getattr(tool, "name", type(tool).__name__)
            err_msg = (
                f"Tool '{tool_name}.{action}' child process PID {child_pid} "
                f"exceeded deadline ({effective_timeout}s) and was terminated by parent."
            )

            if raise_on_error:
                raise ProcessTerminatedError(err_msg, child_pid=child_pid)

            return ProcessExecutionResult(
                success=False,
                tool_result=None,
                child_pid=child_pid,
                parent_pid=os.getpid(),
                terminated=True,
                exit_code=exit_code,
                error_type="ProcessTerminatedError",
                error_message=err_msg,
                duration_ms=duration_ms,
            )
        finally:
            try:
                parent_conn.close()
            except Exception:
                pass
            try:
                proc.close()
            except Exception:
                pass
