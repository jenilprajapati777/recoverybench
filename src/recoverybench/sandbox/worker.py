"""Standalone worker script executed inside isolated execution containers.

Uses ONLY Python standard library modules to ensure compatibility with
minimal container images without pre-installed host packages.
Communicates strictly over stdin/stdout pipes using framed JSON Lines.
"""

import contextlib
import importlib
import inspect
import json
import os
import re
import sys
import types
import uuid
from collections.abc import Generator
from typing import Any

# Regex patterns for sanitizing secrets in error messages
SECRET_PATTERNS = [
    re.compile(r"sk_live_[0-9a-zA-Z]{16,64}"),
    re.compile(r"ak_[0-9a-zA-Z]{16,64}"),
    re.compile(r"Bearer\s+([A-Za-z0-9\-_\.\~]{10,})", re.IGNORECASE),
    re.compile(r'(password|token|secret|key|authorization)["\']?\s*[:=]\s*["\']?([^"\'\s,;]+)', re.IGNORECASE),
]


def mask_secrets(text: str) -> str:
    """Redact API keys, tokens, and credentials from worker messages."""
    result = text
    for pattern in SECRET_PATTERNS:
        result = pattern.sub("[REDACTED_SECRET]", result)
    return result


class WorkerProtocolError(Exception):
    """Raised on invalid protocol frame format or unexpected message type."""


class ToolError(Exception):
    """Raised when host tool dispatch returns an error."""


class IPCToolClient:
    """ToolClient implementation communicating with the host over stdin/stdout."""

    def __init__(self, ipc_handler: "WorkerIPCHandler", default_operation_id: str | None = None) -> None:
        self._ipc = ipc_handler
        self._default_op_id = default_operation_id
        self._current_op_id = default_operation_id

    @property
    def current_operation_id(self) -> str | None:
        return self._current_op_id

    @contextlib.contextmanager
    def operation(self, operation_id: str) -> Generator["IPCToolClient", None, None]:
        """Context manager establishing an explicit operation identity boundary."""
        prev = self._current_op_id
        self._current_op_id = operation_id.strip() if operation_id else None
        try:
            yield self
        finally:
            self._current_op_id = prev

    def call(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch a tool request through the host over IPC."""
        effective_op_id = (
            operation_id.strip()
            if (operation_id and operation_id.strip())
            else self._current_op_id
        )
        return self._ipc.dispatch_tool(
            tool_name=tool_name,
            action=action,
            params=params or {},
            idempotency_key=idempotency_key,
            operation_id=effective_op_id,
        )

    def __call__(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        return self.call(tool_name, action, params, idempotency_key, operation_id)


class WorkerClock:
    """Minimal virtual clock for isolated container execution."""

    def __init__(self) -> None:
        self.elapsed: float = 0.0

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds


class WorkerTaskContext:
    """Minimal TaskContext passed into the agent entrypoint."""

    def __init__(self, scenario_id: str, task_params: dict[str, Any], operation_id: str | None = None) -> None:
        self.scenario_id = scenario_id
        self.task_params = task_params
        self.params = task_params
        self.operation_id = operation_id
        self.clock = WorkerClock()


# In-container shims for recoverybench SDK when running without host packages
if "recoverybench" not in sys.modules:
    rb_mod = types.ModuleType("recoverybench")
    rb_agent_mod = types.ModuleType("recoverybench.agent")
    rb_base_mod = types.ModuleType("recoverybench.agent.base")
    rb_errors_mod = types.ModuleType("recoverybench.errors")

    class AgentAdapter:
        agent_id: str = "agent"

    class AgentResult:
        def __init__(
            self,
            success: bool = False,
            status: str = "UNKNOWN",
            data: dict[str, Any] | None = None,
            error: str | None = None,
            attempts: int = 1,
            status_message: str = "",
        ) -> None:
            self.success = success
            self.status = "SUCCEEDED" if success else ("FAILED" if status != "UNKNOWN" else "UNKNOWN")
            self.data = data or {}
            self.error = error
            self.attempts = attempts
            self.status_message = status_message

    class RateLimitError(ToolError):
        def __init__(self, message: str, retry_after_s: float = 1.0) -> None:
            super().__init__(message)
            self.retry_after_s = retry_after_s

    class MalformedResponseError(ToolError):
        pass

    rb_base_mod.AgentAdapter = AgentAdapter  # type: ignore[attr-defined]
    rb_base_mod.AgentResult = AgentResult  # type: ignore[attr-defined]
    rb_base_mod.TaskContext = WorkerTaskContext  # type: ignore[attr-defined]
    rb_base_mod.ToolCaller = Any  # type: ignore[attr-defined]

    rb_errors_mod.ToolError = ToolError  # type: ignore[attr-defined]
    rb_errors_mod.RateLimitError = RateLimitError  # type: ignore[attr-defined]
    rb_errors_mod.MalformedResponseError = MalformedResponseError  # type: ignore[attr-defined]

    rb_mod.agent = rb_agent_mod  # type: ignore[attr-defined]
    rb_agent_mod.base = rb_base_mod  # type: ignore[attr-defined]
    rb_mod.errors = rb_errors_mod  # type: ignore[attr-defined]

    sys.modules["recoverybench"] = rb_mod
    sys.modules["recoverybench.agent"] = rb_agent_mod
    sys.modules["recoverybench.agent.base"] = rb_base_mod
    sys.modules["recoverybench.errors"] = rb_errors_mod


class WorkerIPCHandler:
    """Manages protocol framing, message dispatch, and agent execution."""

    def __init__(self, in_pipe: Any, out_pipe: Any, err_pipe: Any) -> None:
        self.in_pipe = in_pipe
        self.out_pipe = out_pipe
        self.err_pipe = err_pipe
        self.agent_spec: dict[str, Any] = {}
        self.scenario_id: str = ""
        self.task_params: dict[str, Any] = {}
        self.agent_callable: Any = None

    def log(self, message: str) -> None:
        """Write diagnostics exclusively to stderr."""
        self.err_pipe.write(f"[worker] {message}\n")
        self.err_pipe.flush()

    def send_frame(self, data: dict[str, Any]) -> None:
        """Send a single JSON line to host on stdout."""
        line = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        self.out_pipe.write(line + "\n")
        self.out_pipe.flush()

    def read_frame(self) -> dict[str, Any]:
        """Read and decode a single JSON line from host on stdin."""
        line = self.in_pipe.readline()
        if not line:
            raise EOFError("Host closed stdin pipe")
        clean = line.strip()
        if not clean:
            raise WorkerProtocolError("Empty frame received from host")
        try:
            data = json.loads(clean)
            if not isinstance(data, dict):
                raise WorkerProtocolError(f"Expected JSON object from host, got {type(data).__name__}")
            return data
        except json.JSONDecodeError as jde:
            raise WorkerProtocolError(f"Malformed JSON from host: {jde}") from jde

    def dispatch_tool(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Send tool_call request to host and await tool_response."""
        req_id = f"req_{uuid.uuid4().hex[:8]}"
        req_msg = {
            "type": "tool_call",
            "version": "1.0",
            "request_id": req_id,
            "tool_name": tool_name,
            "action": action,
            "params": params,
            "idempotency_key": idempotency_key,
            "operation_id": operation_id,
        }
        self.send_frame(req_msg)

        # Await matching response
        while True:
            resp = self.read_frame()
            resp_type = resp.get("type")
            if resp_type == "tool_response":
                if resp.get("request_id") != req_id:
                    raise WorkerProtocolError(
                        f"Request ID mismatch: expected {req_id}, got {resp.get('request_id')}"
                    )
                if not resp.get("success"):
                    error_msg = resp.get("error") or "Tool call failed"
                    raise ToolError(error_msg)
                return resp.get("data") or {}
            elif resp_type == "shutdown":
                raise WorkerProtocolError("Received shutdown while awaiting tool response")
            else:
                self.log(f"Ignoring unexpected message while waiting for tool response: {resp_type}")

    def normalize_result(self, raw_result: Any) -> dict[str, Any]:
        """Normalize agent return value according to strict P0-2 rules."""
        # 1. Native booleans
        if type(raw_result) is bool:
            st = "SUCCEEDED" if raw_result else "FAILED"
            return {"status": st, "success": raw_result, "data": {}, "error": None}

        # 2. None
        if raw_result is None:
            return {
                "status": "UNKNOWN",
                "success": False,
                "data": {},
                "error": "Agent returned None (ambiguous completion)",
            }

        # 3. Object with .status and .success (e.g. AgentResult)
        if hasattr(raw_result, "status") and hasattr(raw_result, "success"):
            st_val = getattr(raw_result.status, "value", str(raw_result.status)).upper()
            succ_val = raw_result.success
            err_val = getattr(raw_result, "error", None)
            data_val = getattr(raw_result, "data", {})
            return {
                "status": st_val,
                "success": succ_val,
                "data": data_val if isinstance(data_val, dict) else {},
                "error": err_val,
            }

        # 4. Dictionary
        if isinstance(raw_result, dict):
            has_succ = "success" in raw_result
            raw_succ = raw_result.get("success")
            has_st = "status" in raw_result and raw_result["status"] is not None
            raw_st = raw_result.get("status")

            # Check strict boolean for success
            if has_succ and raw_succ is not None and type(raw_succ) is not bool:
                return {
                    "status": "UNKNOWN",
                    "success": False,
                    "data": raw_result,
                    "error": f"Invalid success field: expected boolean, got {type(raw_succ).__name__}",
                }

            # Check conflict
            if has_succ and has_st:
                upper_st = str(raw_st).strip().upper()
                if upper_st in ("SUCCEEDED", "SUCCESS") and not raw_succ:
                    return {
                        "status": "UNKNOWN",
                        "success": False,
                        "data": raw_result,
                        "error": "Conflicting completion indicators: status='SUCCEEDED' conflicts with success=False",
                    }
                if upper_st in ("FAILED", "FAILURE", "ERROR") and raw_succ:
                    return {
                        "status": "UNKNOWN",
                        "success": False,
                        "data": raw_result,
                        "error": "Conflicting completion indicators: status='FAILED' conflicts with success=True",
                    }
                if upper_st == "UNKNOWN" and raw_succ:
                    return {
                        "status": "UNKNOWN",
                        "success": False,
                        "data": raw_result,
                        "error": "Conflicting completion indicators: status='UNKNOWN' conflicts with success=True",
                    }

            if has_succ and type(raw_succ) is bool:
                st = "SUCCEEDED" if raw_succ else "FAILED"
                return {"status": st, "success": raw_succ, "data": raw_result, "error": raw_result.get("error")}

            if has_st:
                upper_st = str(raw_st).strip().upper()
                succ = upper_st in ("SUCCEEDED", "SUCCESS")
                return {"status": upper_st, "success": succ, "data": raw_result, "error": raw_result.get("error")}

            return {
                "status": "UNKNOWN",
                "success": False,
                "data": raw_result,
                "error": "Dictionary missing explicit status or success field",
            }

        # 5. Unsupported scalar
        return {
            "status": "UNKNOWN",
            "success": False,
            "data": {},
            "error": f"Unsupported agent return type: {type(raw_result).__name__}",
        }

    def run_loop(self) -> None:
        """Main protocol execution loop."""
        while True:
            try:
                frame = self.read_frame()
            except EOFError:
                break

            msg_type = frame.get("type")

            if msg_type == "init":
                self.agent_spec = frame.get("spec", {})
                self.scenario_id = frame.get("scenario_id", "")
                self.task_params = frame.get("task_params", {})

                # Setup sys.path from workspace
                workspace = "/workspace" if os.path.exists("/workspace") else os.getcwd()
                if workspace not in sys.path:
                    sys.path.insert(0, workspace)

                # Load module and entrypoint
                module_name = self.agent_spec.get("module_path", "")
                entrypoint_name = self.agent_spec.get("entrypoint", "run_agent")
                dependencies = self.agent_spec.get("dependencies", [])

                # 1. Pre-flight dependency verification
                missing_deps = []
                for dep in dependencies:
                    pkg_candidate = re.split(r"[><=~!;]", str(dep))[0].strip()
                    if not pkg_candidate:
                        continue
                    import_name = pkg_candidate.replace("-", "_")
                    try:
                        importlib.import_module(import_name)
                    except ImportError:
                        try:
                            importlib.import_module(pkg_candidate)
                        except ImportError:
                            missing_deps.append(str(dep))

                if missing_deps:
                    missing_str = ", ".join(missing_deps)
                    self.send_frame({
                        "type": "error",
                        "version": "1.0",
                        "error_type": "MissingDependencyError",
                        "message": (
                            f"Missing required runtime dependencies in container: {missing_str}. "
                            "Because the sandbox runs with network isolation (--network=none), packages cannot be downloaded at runtime. "
                            "Use a prebuilt container image containing these dependencies, or specify a custom image in SandboxConfig / AgentSpec."
                        ),
                        "details": {"missing_dependencies": missing_deps},
                    })
                    continue

                # 2. Module import & entrypoint retrieval
                try:
                    mod = importlib.import_module(module_name)
                    self.agent_callable = getattr(mod, entrypoint_name)
                    agent_id = getattr(self.agent_callable, "agent_id", self.agent_spec.get("name", "agent"))
                    self.send_frame({"type": "ready", "version": "1.0", "agent_id": str(agent_id)})
                except ModuleNotFoundError as mnfe:
                    clean_err = mask_secrets(str(mnfe))
                    self.send_frame({
                        "type": "error",
                        "version": "1.0",
                        "error_type": "MissingDependencyError",
                        "message": (
                            f"Agent entrypoint module '{module_name}' failed to load due to missing dependency: {clean_err}. "
                            "Because the sandbox runs with network isolation (--network=none), packages cannot be downloaded at runtime. "
                            "Ensure all required dependencies are pre-installed in your base container image."
                        ),
                        "details": {"module": module_name, "error": clean_err},
                    })
                except Exception as exc:
                    clean_err = mask_secrets(str(exc))
                    self.send_frame({
                        "type": "error",
                        "version": "1.0",
                        "error_type": "ImportError",
                        "message": f"Failed to load agent entrypoint '{module_name}:{entrypoint_name}': {clean_err}",
                        "details": {},
                    })

            elif msg_type == "execute":
                op_id = frame.get("operation_id")
                scenario_id = frame.get("scenario_id", self.scenario_id)
                task_params = frame.get("task_params", self.task_params)

                tool_client = IPCToolClient(self, default_operation_id=op_id)
                task_context = WorkerTaskContext(scenario_id, task_params, operation_id=op_id)

                try:
                    # Invoke agent callable
                    agent_obj = self.agent_callable
                    if inspect.isclass(agent_obj):
                        try:
                            agent_obj = agent_obj()
                        except Exception as exc:
                            raise RuntimeError(
                                f"Failed to instantiate agent class {self.agent_callable.__name__}: {exc}"
                            ) from exc

                    if hasattr(agent_obj, "run") and callable(agent_obj.run):
                        raw_result = agent_obj.run(task_context, tool_client)
                    elif callable(agent_obj):
                        raw_result = agent_obj(task_context, tool_client)
                    else:
                        raise TypeError(f"Loaded entrypoint {self.agent_callable!r} is not callable")

                    normalized = self.normalize_result(raw_result)
                    self.send_frame({
                        "type": "completion",
                        "version": "1.0",
                        "status": normalized["status"],
                        "success": normalized["success"],
                        "data": normalized.get("data", {}),
                        "error": normalized.get("error"),
                        "attempts": 1,
                        "status_message": f"Completed with status {normalized['status']}",
                    })
                except Exception as exc:
                    clean_err = mask_secrets(str(exc))
                    self.log(f"Agent raised unhandled exception: {clean_err}")
                    self.send_frame({
                        "type": "completion",
                        "version": "1.0",
                        "status": "FAILED",
                        "success": False,
                        "data": {},
                        "error": clean_err,
                        "attempts": 1,
                        "status_message": f"Agent crashed: {clean_err}",
                    })

            elif msg_type == "shutdown":
                self.log("Worker received shutdown signal. Exiting.")
                break
            else:
                self.log(f"Unknown message type received: {msg_type}")


def main() -> None:
    """Worker entrypoint."""
    real_stdin = sys.stdin
    real_stdout = sys.stdout
    real_stderr = sys.stderr

    # Redirect user stdout to stderr so agent print() does not corrupt protocol stream
    sys.stdout = sys.stderr

    handler = WorkerIPCHandler(real_stdin, real_stdout, real_stderr)
    try:
        handler.run_loop()
    except Exception as exc:
        real_stderr.write(f"[worker fatal] {exc}\n")
        real_stderr.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
