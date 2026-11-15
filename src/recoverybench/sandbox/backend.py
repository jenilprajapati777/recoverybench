"""Modular execution backends for running agents under test."""

import importlib.util
import inspect
import os
import re
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from recoverybench.adapters.callable import wrap_agent
from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolClient,
)
from recoverybench.errors import ConfigurationError, InfrastructureError, ToolError
from recoverybench.sandbox.config import (
    MANDATORY_CONTAINER_ENV,
    SandboxConfig,
    is_reserved_or_blocked_env,
)
from recoverybench.sandbox.docker_client import DockerClient
from recoverybench.sandbox.protocol import (
    decode_message,
    encode_message,
)
from recoverybench.sandbox.spec import AgentSpec
from recoverybench.sandbox.staging import WorkspaceStaging


class IsolationMode(StrEnum):
    """Execution isolation mode."""

    ISOLATED = "isolated"
    TRUSTED = "trusted"


class ExecutionBackend(ABC):
    """Abstract base class for agent execution environments."""

    @property
    @abstractmethod
    def mode(self) -> IsolationMode:
        """The isolation mode of this backend."""

    @property
    @abstractmethod
    def is_isolated(self) -> bool:
        """Whether this backend provides operating-system-level isolation."""

    @abstractmethod
    def run(
        self,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        task_context: TaskContext,
        tool_client: ToolClient,
        timeout_seconds: float = 15.0,
    ) -> AgentResult:
        """Execute the agent in this environment and return the standardized result."""


class InProcessExecutionBackend(ExecutionBackend):
    """Executes the agent in the host Python process.

    WARNING: This execution mode is TRUSTED and NOT ISOLATED.
    It does not restrict external network calls or filesystem access.
    """

    @property
    def mode(self) -> IsolationMode:
        return IsolationMode.TRUSTED

    @property
    def is_isolated(self) -> bool:
        return False

    def run(
        self,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        task_context: TaskContext,
        tool_client: ToolClient,
        timeout_seconds: float = 15.0,
    ) -> AgentResult:
        # Emit explicit, un-ignorable security advisory
        sys.stderr.write(
            "[SECURITY ADVISORY] Running agent in TRUSTED / NOT ISOLATED mode.\n"
            "This execution backend does not block outbound network traffic or restrict host filesystem access.\n"
        )
        sys.stderr.flush()

        resolved_adapter: AgentAdapter
        if isinstance(agent, AgentSpec):
            # Dynamically load the agent spec in-process
            resolved_adapter = self._load_agent_from_spec(agent)
        elif isinstance(agent, AgentAdapter):
            resolved_adapter = agent
        else:
            resolved_adapter = wrap_agent(agent)

        res = resolved_adapter.run(task_context, tool_client)
        return res

    def _load_agent_from_spec(self, spec: AgentSpec) -> AgentAdapter:
        """Dynamically load an AgentSpec into an AgentAdapter in-process."""
        workspace = Path(spec.workspace_dir).resolve()
        if str(workspace) not in sys.path:
            sys.path.insert(0, str(workspace))

        try:
            mod = importlib.import_module(spec.module_path)
            entrypoint = getattr(mod, spec.entrypoint)
        except Exception as exc:
            raise ConfigurationError(
                f"Failed to load AgentSpec entrypoint '{spec.module_path}:{spec.entrypoint}': {exc}"
            ) from exc

        if isinstance(entrypoint, AgentAdapter):
            return entrypoint
        if callable(entrypoint):
            return wrap_agent(entrypoint, agent_id=spec.name)
        raise ConfigurationError(f"Loaded entrypoint '{spec.entrypoint}' is neither an AgentAdapter nor a callable")


class DockerExecutionBackend(ExecutionBackend):
    """Executes the agent inside an isolated Docker Linux container with --network=none."""

    def __init__(self, sandbox_config: SandboxConfig | None = None) -> None:
        self.config = sandbox_config or SandboxConfig()
        self.docker_client = DockerClient(self.config)
        self.last_env_diagnostics: list[str] = []

    @property
    def mode(self) -> IsolationMode:
        return IsolationMode.ISOLATED

    @property
    def is_isolated(self) -> bool:
        return True

    def run(
        self,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        task_context: TaskContext,
        tool_client: ToolClient,
        timeout_seconds: float | None = None,
    ) -> AgentResult:
        # 1. Fail Closed Check: Docker must be available
        is_avail, reason = self.docker_client.is_docker_available()
        if not is_avail:
            raise InfrastructureError(
                f"Docker sandbox execution requested, but Docker is unavailable: {reason}. "
                "Ensure Docker Engine is installed, running, and accessible, "
                "or explicitly specify trusted mode (--isolation trusted)."
            )

        # 2. Resolve AgentSpec
        spec: AgentSpec
        if isinstance(agent, AgentSpec):
            spec = agent
        elif hasattr(agent, "spec") and isinstance(agent.spec, AgentSpec):
            spec = agent.spec
        elif isinstance(agent, AgentAdapter):
            # Attempt to resolve from adapter
            try:
                spec = AgentSpec.from_callable(agent.__class__)
            except ValueError as ve:
                raise ConfigurationError(
                    f"Cannot run AgentAdapter '{agent.agent_id}' in Docker sandbox without an AgentSpec: {ve}. "
                    "Use AgentSpec.from_file(...) to declare the agent source location, "
                    "or execute with --isolation trusted."
                ) from ve
        elif callable(agent):
            try:
                spec = AgentSpec.from_callable(agent)
            except ValueError as ve:
                raise ConfigurationError(
                    f"Cannot run in-memory callable in Docker sandbox without an AgentSpec: {ve}. "
                    "In-memory closures and lambdas cannot be safely transferred across container boundaries without pickle. "
                    "Define your agent in an on-disk module and use AgentSpec.from_file(...)."
                ) from ve
        else:
            raise ConfigurationError(f"Unsupported agent type for Docker sandbox: {type(agent).__name__}")

        # 3. Fail Closed Check: Container image must be pinned with digest and available
        effective_image = spec.runtime_image or self.config.runtime_image
        if not self.config.allow_mutable_image_tags and not re.search(r"@sha256:[a-fA-F0-9]{64}$", effective_image):
            raise ConfigurationError(
                f"Security violation: Mutable container image tag '{effective_image}' rejected. "
                "Isolated execution requires an immutable SHA-256 image digest (e.g. 'python:3.11-slim@sha256:...'). "
                "To allow mutable tags for local development, set allow_mutable_image_tags=True in SandboxConfig."
            )
        if not self.docker_client.check_image_exists(effective_image):
            raise ConfigurationError(
                f"Docker runtime image '{effective_image}' is not available locally. "
                f"Pull or build the required runtime image using 'docker pull {effective_image}', "
                "or configure an existing local image in Settings or AgentSpec."
            )

        # 4. Stage Workspace
        from recoverybench.sandbox import worker as worker_module

        worker_source = inspect.getsource(worker_module)
        staged_dir = WorkspaceStaging.stage_workspace(
            spec.workspace_dir,
            worker_script_content=worker_source,
            manifest_files=spec.files,
        )

        container_name = f"rb_sandbox_{uuid.uuid4().hex[:12]}"
        max_timeout = timeout_seconds or self.config.timeout_seconds

        # 5. Build Environment Variables (strictly filtered allowlist)
        env_vars = self._build_safe_env(spec)

        # 6. Build Docker Command
        effective_config = self.config.model_copy(update={"runtime_image": effective_image})
        cmd = self.docker_client.build_run_command(
            container_name=container_name,
            staged_dir=staged_dir,
            env_vars=env_vars,
            custom_config=effective_config,
        )

        # 7. Execute Process with IPC Loop
        proc: subprocess.Popen[str] | None = None
        start_time = time.perf_counter()

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,  # Line buffered
            )

            result = self._drive_ipc_loop(
                proc=proc,
                spec=spec,
                task_context=task_context,
                tool_client=tool_client,
                timeout_seconds=max_timeout,
                start_time=start_time,
            )
            return result

        except Exception as exc:
            # Capture any diagnostic output from stderr
            stderr_out = ""
            if proc and proc.stderr:
                try:
                    stderr_out = proc.stderr.read()
                except Exception:
                    pass

            if isinstance(exc, (ConfigurationError, InfrastructureError, ToolError)):
                raise
            err_msg = f"{type(exc).__name__}: {exc}"
            if stderr_out:
                err_msg += f"\nWorker stderr: {stderr_out.strip()}"
            raise InfrastructureError(f"Isolated container execution failed: {err_msg}") from exc

        finally:
            if proc:
                try:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.terminate()
                    proc.wait(timeout=2.0)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            # Guaranteed container removal & workspace cleanup
            self.docker_client.kill_and_remove(container_name)
            WorkspaceStaging.cleanup_workspace(staged_dir)

    def _build_safe_env(self, spec: AgentSpec) -> dict[str, str]:
        """Construct a strictly filtered environment dictionary without host secrets or reserved variables."""
        safe_env: dict[str, str] = dict(MANDATORY_CONTAINER_ENV)

        # Include allowlisted host variables (strictly excluding reserved/blocked variables)
        for var_name in self.config.env_allowlist:
            if is_reserved_or_blocked_env(var_name):
                continue
            if var_name in os.environ:
                val = os.environ[var_name]
                if os.name == "nt" and (":" in val or "\\" in val) and var_name.upper() == "PYTHONPATH":
                    continue
                safe_env[var_name] = val

        # Filter or reject explicit agent-declared variables against reserved container policy
        rejected_vars: list[str] = []
        for k, v in spec.env.items():
            if is_reserved_or_blocked_env(k):
                rejected_vars.append(k)
                continue
            safe_env[k] = v

        if rejected_vars:
            diag = (
                f"AgentSpec.env contains reserved/blocked environment variable(s): "
                f"{', '.join(sorted(rejected_vars))}. "
                "Overriding container runtime, dynamic loader, or system execution variables "
                "is prohibited in isolated sandbox mode."
            )
            self.last_env_diagnostics.append(diag)
            if self.config.reject_reserved_env:
                raise ConfigurationError(f"Security violation: {diag}")
            sys.stderr.write(
                f"[SECURITY ADVISORY] Filtered {len(rejected_vars)} reserved variable(s) from AgentSpec.env: {diag}\n"
            )
            sys.stderr.flush()

        return safe_env

    def _drive_ipc_loop(
        self,
        proc: subprocess.Popen[str],
        spec: AgentSpec,
        task_context: TaskContext,
        tool_client: ToolClient,
        timeout_seconds: float,
        start_time: float,
    ) -> AgentResult:
        """Drive the JSON Lines protocol exchange with the isolated worker."""
        if proc.stdin is None or proc.stdout is None:
            raise InfrastructureError("Worker process stdin or stdout pipe is not available")

        # Helper to read next frame with deadline
        def read_next_frame() -> dict[str, Any]:
            elapsed = time.perf_counter() - start_time
            if elapsed > timeout_seconds:
                raise InfrastructureError(f"Container execution timed out after {timeout_seconds:.1f} seconds")

            assert proc.stdout is not None
            line = proc.stdout.readline()
            if not line:
                # Process exited or died
                ret = proc.poll()
                stderr_text = proc.stderr.read() if proc.stderr else ""
                raise InfrastructureError(
                    f"Isolated container worker exited unexpectedly (exit code: {ret}). "
                    f"Worker stderr: {stderr_text.strip() or 'None'}"
                )
            return decode_message(line, max_bytes=self.config.max_message_bytes)

        # 1. Send Init
        init_msg = {
            "type": "init",
            "version": "1.0",
            "spec": spec.model_dump(mode="json"),
            "scenario_id": task_context.scenario_id,
            "task_params": task_context.task_params,
        }
        proc.stdin.write(encode_message(init_msg))
        proc.stdin.flush()

        # 2. Await Ready
        ready_frame = read_next_frame()
        if ready_frame.get("type") == "error":
            raise ConfigurationError(
                f"Worker failed to initialize agent: {ready_frame.get('message')}"
            )
        if ready_frame.get("type") != "ready":
            raise InfrastructureError(
                f"Expected 'ready' frame from worker, got '{ready_frame.get('type')}'"
            )

        # 3. Send Execute
        exec_msg = {
            "type": "execute",
            "version": "1.0",
            "scenario_id": task_context.scenario_id,
            "task_params": task_context.task_params,
            "operation_id": task_context.operation_id,
        }
        proc.stdin.write(encode_message(exec_msg))
        proc.stdin.flush()

        # 4. Service Tool Calls & Await Completion
        while True:
            elapsed = time.perf_counter() - start_time
            if elapsed > timeout_seconds:
                raise InfrastructureError(f"Container execution timed out after {timeout_seconds:.1f} seconds")

            frame = read_next_frame()
            msg_type = frame.get("type")

            if msg_type == "tool_call":
                req_id = frame.get("request_id", "")
                tool_name = frame.get("tool_name", "")
                action = frame.get("action", "")
                params = frame.get("params", {})
                idempotency_key = frame.get("idempotency_key")
                operation_id = frame.get("operation_id")

                try:
                    tool_data = tool_client.call(
                        tool_name=tool_name,
                        action=action,
                        params=params,
                        idempotency_key=idempotency_key,
                        operation_id=operation_id,
                    )
                    resp_msg = {
                        "type": "tool_response",
                        "version": "1.0",
                        "request_id": req_id,
                        "success": True,
                        "data": tool_data,
                        "error": None,
                    }
                except Exception as exc:
                    resp_msg = {
                        "type": "tool_response",
                        "version": "1.0",
                        "request_id": req_id,
                        "success": False,
                        "data": None,
                        "error": str(exc),
                    }

                proc.stdin.write(encode_message(resp_msg))
                proc.stdin.flush()

            elif msg_type == "completion":
                # Graceful shutdown
                try:
                    proc.stdin.write(encode_message({"type": "shutdown", "version": "1.0"}))
                    proc.stdin.flush()
                except Exception:
                    pass

                status_str = frame.get("status", "UNKNOWN").upper()
                st = CompletionStatus.UNKNOWN
                if status_str in ("SUCCEEDED", "SUCCESS"):
                    st = CompletionStatus.SUCCEEDED
                elif status_str in ("FAILED", "FAILURE", "ERROR"):
                    st = CompletionStatus.FAILED

                return AgentResult(
                    status=st,
                    success=bool(frame.get("success")),
                    data=frame.get("data", {}),
                    error=frame.get("error"),
                    attempts=frame.get("attempts", 1),
                    status_message=frame.get("status_message", ""),
                )

            elif msg_type == "error":
                err_msg = frame.get("message", "Unknown worker error")
                raise InfrastructureError(f"Isolated worker error: {err_msg}")

            else:
                raise InfrastructureError(f"Unexpected IPC message type from worker: {msg_type}")
