"""Model Context Protocol (MCP) Server for RecoveryBench.

Provides a standard JSON-RPC 2.0 stdio server exposing RecoveryBench tool
services, fault injection, and evaluation capabilities to external MCP clients
(e.g., Claude Desktop, Cursor, AutoGen, custom agents).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from recoverybench.agent.base import AgentResult, CompletionStatus
from recoverybench.clock import VirtualClock
from recoverybench.engine.context import RunResult
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.errors import (
    ProcessTerminatedError,
)
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.faults.injector import FaultInjector
from recoverybench.ledger.base import Ledger
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.mcp.errors import MCPError, MCPErrorCode
from recoverybench.mcp.protocol import (
    ImplementationInfo,
    InitializeResult,
    MCPCallToolResult,
    MCPResource,
    MCPResourceContent,
    MCPTextContent,
    MCPTool,
    ServerCapabilities,
)
from recoverybench.models import (
    CommitStatus,
    ExecutionStatus,
    LedgerEvent,
    ToolExecutionModel,
    compute_request_fingerprint,
    hash_token,
)
from recoverybench.sandbox.backend import (
    ExecutionBackend,
    InProcessExecutionBackend,
    IsolationMode,
    ProcessIsolatedExecutionBackend,
)
from recoverybench.scenarios.schema import ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService
from recoverybench.tools.process_runner import ToolProcessRunner

logger = logging.getLogger("recoverybench.mcp")


@dataclass
class MCPServerConfig:
    """Configuration options for RecoveryBench MCP Server."""

    server_name: str = "recoverybench-mcp"
    server_version: str = "0.2.0"
    scenario: ScenarioModel | None = None
    isolation: IsolationMode = IsolationMode.TRUSTED
    db_path: Path | None = None
    default_tool_timeout: float = 10.0


class RecoveryBenchMCPServer:
    """MCP Server Bridge exposing RecoveryBench tools and execution pipeline."""

    def __init__(
        self,
        config: MCPServerConfig | None = None,
        tool_registry: ToolRegistry | None = None,
        ledger: Ledger | None = None,
        engine: ExecutionEngine | None = None,
    ) -> None:
        self.config = config or MCPServerConfig()
        self.ledger: Ledger = ledger or InMemoryLedger()
        self.tool_registry: ToolRegistry = tool_registry or ToolRegistry()
        self.clock = VirtualClock()
        self.evaluator = RecoveryEvaluator()
        self.process_runner = ToolProcessRunner()

        # Ensure default mock tools are registered if registry is empty
        if not self.tool_registry.list_tools():
            self.tool_registry.register(MockPaymentService())

        # Configure Sandbox backend
        self.backend: ExecutionBackend
        if self.config.isolation == IsolationMode.ISOLATED:
            self.backend = ProcessIsolatedExecutionBackend()
        else:
            self.backend = InProcessExecutionBackend()

        self.engine: ExecutionEngine = engine or ExecutionEngine(
            ledger=self.ledger,
            clock=self.clock,
            tool_registry=self.tool_registry,
            backend=self.backend,
        )

        # Active Session State
        self.session_id: str = f"mcp_session_{uuid.uuid4().hex[:8]}"
        self.active_run_id: str = f"run_{self.session_id}"
        self.active_scenario: ScenarioModel = self.config.scenario or ScenarioModel(
            schema_version=1,
            scenario_id="mcp_interactive",
            name="MCP Interactive Testing Session",
            description="Interactive tool testing session via Model Context Protocol",
            target_tool="payment_gateway",
            task={"operation_id": f"op_{self.session_id}"},
            faults=[],
        )
        self.fault_injector = FaultInjector(
            faults=self.active_scenario.faults,
            seed=self.active_scenario.seed,
        )

        self._initialized: bool = False
        self._client_ready: bool = False
        self._total_invocations: int = 0
        self._latest_run_result: RunResult | None = None

        # Bind tools to session run
        self.tool_registry.reset_all()
        self.tool_registry.bind_to_run(self.active_run_id)

    @property
    def is_initialized(self) -> bool:
        """Return whether client has completed handshake."""
        return self._initialized

    # --------------------------------------------------------------------------
    # Protocol Dispatch
    # --------------------------------------------------------------------------

    def handle_request(self, request_dict: dict[str, Any]) -> dict[str, Any] | None:
        """Process a single JSON-RPC request and return response dict (or None for notifications)."""
        req_id = request_dict.get("id")
        method = request_dict.get("method")
        params = request_dict.get("params") or {}
        is_notification = ("id" not in request_dict or req_id is None)

        if not isinstance(method, str):
            if is_notification:
                return None
            err = MCPError(MCPErrorCode.INVALID_REQUEST, "Method must be a string")
            return self._format_error(req_id, err)

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method in ("notifications/initialized", "initialized"):
                self._client_ready = True
                return None
            elif method in ("notifications/cancelled", "cancelled"):
                logger.info("Received client cancellation notification")
                return None
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = self._handle_tools_list()
            elif method == "tools/call":
                result = self._handle_tools_call(params)
            elif method == "resources/list":
                result = self._handle_resources_list()
            elif method == "resources/read":
                result = self._handle_resources_read(params)
            else:
                if is_notification:
                    # JSON-RPC 2.0: Never reply to notifications even if method is unrecognized
                    return None
                raise MCPError(MCPErrorCode.METHOD_NOT_FOUND, f"Method '{method}' not found")

            if is_notification:
                return None

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }
        except MCPError as exc:
            if is_notification:
                return None
            return self._format_error(req_id, exc)
        except Exception as exc:
            if is_notification:
                return None
            logger.exception("Internal error in MCP server dispatch")
            return self._format_error(
                req_id,
                MCPError(MCPErrorCode.INTERNAL_ERROR, str(exc)),
            )

    def handle_message(self, raw_json: str) -> str | None:
        """Process incoming raw JSON string and return response JSON string (or None)."""
        raw_json = raw_json.strip()
        if not raw_json:
            return None

        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": MCPErrorCode.PARSE_ERROR,
                    "message": f"Parse error: {exc.msg}",
                },
            }
            return json.dumps(err_resp)

        if isinstance(data, list):
            # Batch request
            responses: list[dict[str, Any]] = []
            for item in data:
                if isinstance(item, dict):
                    resp = self.handle_request(item)
                    if resp is not None:
                        responses.append(resp)
            return json.dumps(responses) if responses else None

        if isinstance(data, dict):
            resp = self.handle_request(data)
            return json.dumps(resp) if resp is not None else None

        err_resp = {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": MCPErrorCode.INVALID_REQUEST,
                "message": "Invalid Request: expected object or array",
            },
        }
        return json.dumps(err_resp)

    # --------------------------------------------------------------------------
    # Handlers
    # --------------------------------------------------------------------------

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """Handle initialize handshake."""
        self._initialized = True
        client_version = params.get("protocolVersion") if isinstance(params, dict) else None
        negotiated_version = str(client_version) if client_version in ("2024-11-05", "latest") else "2024-11-05"
        init_res = InitializeResult(
            protocolVersion=negotiated_version,
            capabilities=ServerCapabilities(),
            serverInfo=ImplementationInfo(
                name=self.config.server_name,
                version=self.config.server_version,
            ),
        )
        return init_res.model_dump()

    def _handle_tools_list(self) -> dict[str, Any]:
        """Return list of available MCP tools."""
        tools: list[MCPTool] = []

        # 1. Registered Tool Services
        for name in self.tool_registry.list_tools():
            service = self.tool_registry.get(name)
            desc = getattr(service, "__doc__", None) or f"RecoveryBench mock service: {name}"
            # Expose service tool
            tool = MCPTool(
                name=name,
                description=desc.strip(),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "description": "Action to invoke on tool service"},
                        "params": {"type": "object", "description": "Payload parameters for tool action"},
                        "idempotency_key": {"type": "string", "description": "Client idempotency key"},
                        "operation_id": {"type": "string", "description": "Logical business operation identifier"},
                    },
                    "required": ["action"],
                },
            )
            tools.append(tool)

            # If service has explicit default action or payment charge
            if name == "payment_gateway":
                tools.append(
                    MCPTool(
                        name="payment_gateway_charge",
                        description="Execute a credit card charge via RecoveryBench payment gateway.",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "amount": {"type": "number", "description": "Charge amount"},
                                "currency": {"type": "string", "default": "USD"},
                                "customer_id": {"type": "string"},
                                "idempotency_key": {"type": "string", "description": "Unique transaction key"},
                                "operation_id": {"type": "string"},
                            },
                            "required": ["amount", "customer_id"],
                        },
                    )
                )

        # 2. RecoveryBench Platform Evaluation Tools
        tools.append(
            MCPTool(
                name="recoverybench_evaluate",
                description="Evaluate all recorded tool calls in current session against scenario assertions.",
                inputSchema={
                    "type": "object",
                    "properties": {},
                },
            )
        )
        tools.append(
            MCPTool(
                name="recoverybench_reset_session",
                description="Reset ledger and tools for a new test run.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "scenario_id": {"type": "string", "description": "Optional scenario identifier to load"},
                    },
                },
            )
        )

        return {"tools": [t.model_dump() for t in tools]}

    def _handle_tools_call(self, params: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool call using the RecoveryBench execution pipeline."""
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}

        if not isinstance(tool_name, str):
            raise MCPError(MCPErrorCode.INVALID_PARAMS, "Tool name must be a string")
        if not isinstance(arguments, dict):
            raise MCPError(MCPErrorCode.INVALID_PARAMS, "Tool arguments must be a dictionary")

        # Built-in Platform Tools
        if tool_name == "recoverybench_evaluate":
            return self._evaluate_current_session()
        if tool_name == "recoverybench_reset_session":
            return self._reset_session(arguments.get("scenario_id"))

        # Target Tool & Action Resolution
        resolved_tool_name = tool_name
        raw_action = arguments.get("action")
        resolved_action: str | None = str(raw_action) if raw_action is not None else None

        if tool_name == "payment_gateway_charge":
            resolved_tool_name = "payment_gateway"
            resolved_action = "charge"
        elif "_" in tool_name and not resolved_action:
            parts = tool_name.split("_", 1)
            if parts[0] in self.tool_registry.list_tools():
                resolved_tool_name = parts[0]
                resolved_action = parts[1]

        tool_service = self.tool_registry.get(resolved_tool_name)
        if tool_service is None:
            raise MCPError(MCPErrorCode.INVALID_PARAMS, f"Tool '{resolved_tool_name}' not found.")

        if not resolved_action:
            def_action = getattr(tool_service, "default_action", "default")
            resolved_action = str(def_action) if def_action else "default"

        # Extract execution parameters
        tool_params = arguments.get("params")
        if tool_params is None:
            # Flatten top-level keys excluding protocol fields
            tool_params = {
                k: v
                for k, v in arguments.items()
                if k not in ("action", "idempotency_key", "operation_id")
            }

        idempotency_key = arguments.get("idempotency_key")
        operation_id = arguments.get("operation_id") or self.active_scenario.task.get("operation_id")

        self._total_invocations += 1
        attempt_number = self._total_invocations

        # Check execution model and isolation constraints
        exec_model = getattr(tool_service, "execution_model", ToolExecutionModel.BOUNDED_SYNC)
        is_isolated = getattr(self.backend, "is_isolated", False)

        if exec_model in (ToolExecutionModel.UNCOOPERATIVE, ToolExecutionModel.UNCOOPERATIVE_SYNC):
            if not is_isolated:
                raise MCPError(
                    MCPErrorCode.TOOL_EXECUTION_ERROR,
                    f"Tool '{resolved_tool_name}' declares uncooperative execution which requires process isolation.",
                )

        # Tool Execution with Fault Injection and Ledger Recording
        attempt_id = f"mcp_att_{uuid.uuid4().hex[:12]}"
        sim_ts = self.clock.now_iso()
        request_fp = compute_request_fingerprint(resolved_tool_name, resolved_action, tool_params)
        key_hash = hash_token(idempotency_key) if idempotency_key else None

        # Injected Pre-Commit Fault Check
        attempt = self.fault_injector.register_attempt(resolved_tool_name, resolved_action)
        pre_commit_fault = self.fault_injector.evaluate_pre_commit(resolved_tool_name, resolved_action, attempt)
        if pre_commit_fault:
            err_event = LedgerEvent(
                run_id=self.active_run_id,
                scenario_id=self.active_scenario.scenario_id,
                tool_name=resolved_tool_name,
                action_type=resolved_action,
                operation_id=operation_id,
                attempt_id=attempt_id,
                effect_id=None,
                idempotency_key_hash=key_hash,
                request_fingerprint=request_fp,
                attempt_number=attempt,
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.ABORTED,
                simulated_timestamp=sim_ts,
                error_type=type(pre_commit_fault).__name__,
                details={"fault_injected": True, "error": str(pre_commit_fault)},
            )
            self.ledger.append(err_event)
            return MCPCallToolResult(
                content=[MCPTextContent(text=f"Injected Fault: {type(pre_commit_fault).__name__}: {pre_commit_fault}")],
                isError=True,
                metadata={"attempt_id": attempt_id, "commit_status": "ABORTED"},
            ).model_dump()

        # Execute Tool (OS Process or In-Process)
        try:
            if exec_model in (ToolExecutionModel.UNCOOPERATIVE, ToolExecutionModel.UNCOOPERATIVE_SYNC):
                proc_res = self.process_runner.execute(
                    tool=tool_service,
                    action=resolved_action,
                    params=tool_params,
                    timeout_seconds=getattr(tool_service, "timeout_seconds", self.config.default_tool_timeout),
                    raise_on_error=True,
                )
                tool_result_obj = proc_res.tool_result
                tool_result_data = getattr(tool_result_obj, "data", tool_result_obj)
                committed = getattr(tool_result_obj, "committed", getattr(tool_service, "is_mutating", True))
                effect_id = getattr(tool_result_obj, "effect_id", None) or (f"eff_{uuid.uuid4().hex[:8]}" if committed else None)
            else:
                raw_res = tool_service.execute(
                    resolved_action,
                    tool_params,
                    idempotency_key=idempotency_key,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                )
                if hasattr(raw_res, "data"):
                    tool_result_data = raw_res.data
                    committed = getattr(raw_res, "committed", True)
                    effect_id = getattr(raw_res, "effect_id", None)
                elif isinstance(raw_res, dict):
                    tool_result_data = raw_res
                    committed = True
                    effect_id = raw_res.get("effect_id") or f"eff_{uuid.uuid4().hex[:8]}"
                else:
                    tool_result_data = {"result": raw_res}
                    committed = False
                    effect_id = None

            # Injected Post-Commit Fault Check (e.g. timeout after commit)
            post_commit_fault = self.fault_injector.evaluate_post_commit(resolved_tool_name, resolved_action, attempt)
            if post_commit_fault:
                event = LedgerEvent(
                    run_id=self.active_run_id,
                    scenario_id=self.active_scenario.scenario_id,
                    tool_name=resolved_tool_name,
                    action_type=resolved_action,
                    operation_id=operation_id,
                    attempt_id=attempt_id,
                    effect_id=effect_id,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=request_fp,
                    attempt_number=attempt,
                    execution_status=ExecutionStatus.ERROR,
                    commit_status=CommitStatus.UNCERTAIN,
                    simulated_timestamp=sim_ts,
                    error_type=type(post_commit_fault).__name__,
                    details={"fault_injected": True, "error": str(post_commit_fault)},
                )
                self.ledger.append(event)
                return MCPCallToolResult(
                    content=[MCPTextContent(text=f"Injected Fault: {type(post_commit_fault).__name__}: {post_commit_fault}")],
                    isError=True,
                    metadata={"attempt_id": attempt_id, "commit_status": "UNCERTAIN"},
                ).model_dump()

            # Record Success Event
            commit_status = CommitStatus.COMMITTED if committed else CommitStatus.NOT_APPLICABLE
            event = LedgerEvent(
                run_id=self.active_run_id,
                scenario_id=self.active_scenario.scenario_id,
                tool_name=resolved_tool_name,
                action_type=resolved_action,
                operation_id=operation_id,
                attempt_id=attempt_id,
                effect_id=effect_id,
                idempotency_key_hash=key_hash,
                request_fingerprint=request_fp,
                attempt_number=attempt_number,
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=commit_status,
                simulated_timestamp=sim_ts,
                details={"result": tool_result_data},
            )
            self.ledger.append(event)

            return MCPCallToolResult(
                content=[MCPTextContent(text=json.dumps(tool_result_data))],
                isError=False,
                metadata={
                    "attempt_id": attempt_id,
                    "effect_id": effect_id,
                    "commit_status": commit_status.value,
                },
            ).model_dump()

        except ProcessTerminatedError as term_exc:
            term_event = LedgerEvent(
                run_id=self.active_run_id,
                scenario_id=self.active_scenario.scenario_id,
                tool_name=resolved_tool_name,
                action_type=resolved_action,
                operation_id=operation_id,
                attempt_id=attempt_id,
                effect_id=None,
                idempotency_key_hash=key_hash,
                request_fingerprint=request_fp,
                attempt_number=attempt_number,
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp=sim_ts,
                error_type="ProcessTerminatedError",
                details={"process_terminated": True, "error": str(term_exc)},
            )
            self.ledger.append(term_event)
            return MCPCallToolResult(
                content=[MCPTextContent(text=f"ProcessTerminatedError: {term_exc}")],
                isError=True,
                metadata={"attempt_id": attempt_id, "commit_status": "UNCERTAIN"},
            ).model_dump()

        except Exception as exc:
            err_event = LedgerEvent(
                run_id=self.active_run_id,
                scenario_id=self.active_scenario.scenario_id,
                tool_name=resolved_tool_name,
                action_type=resolved_action,
                operation_id=operation_id,
                attempt_id=attempt_id,
                effect_id=None,
                idempotency_key_hash=key_hash,
                request_fingerprint=request_fp,
                attempt_number=attempt_number,
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.ABORTED,
                simulated_timestamp=sim_ts,
                error_type=type(exc).__name__,
                details={"error": str(exc)},
            )
            self.ledger.append(err_event)
            return MCPCallToolResult(
                content=[MCPTextContent(text=f"{type(exc).__name__}: {exc}")],
                isError=True,
                metadata={"attempt_id": attempt_id, "commit_status": "ABORTED"},
            ).model_dump()

    def _evaluate_current_session(self) -> dict[str, Any]:
        """Evaluate session ledger against active scenario assertions."""
        events = self.ledger.get_events(self.active_run_id)
        agent_res = AgentResult(success=True, status=CompletionStatus.SUCCEEDED)
        eval_result = self.evaluator.evaluate(
            events=events,
            assertions=self.active_scenario.assertions,
            agent_result=agent_res,
            run_id=self.active_run_id,
        )
        report_data = {
            "run_id": self.active_run_id,
            "scenario_id": self.active_scenario.scenario_id,
            "verdict": eval_result.verdict.value,
            "passed": eval_result.verdict.value == "PASS",
            "side_effect_bounds": eval_result.bounds,
            "event_count": len(events),
            "assertion_results": [
                {"name": a.name, "passed": a.passed, "message": a.message}
                for a in eval_result.assertions
            ],
        }
        return MCPCallToolResult(
            content=[MCPTextContent(text=json.dumps(report_data, indent=2))],
            isError=not (eval_result.verdict.value == "PASS"),
            metadata={"verdict": eval_result.verdict.value},
        ).model_dump()

    def _reset_session(self, scenario_id: str | None = None) -> dict[str, Any]:
        """Reset ledger and session for a new test run."""
        self.session_id = f"mcp_session_{uuid.uuid4().hex[:8]}"
        self.active_run_id = f"run_{self.session_id}"
        self._total_invocations = 0
        self.tool_registry.reset_all()
        self.tool_registry.bind_to_run(self.active_run_id)

        msg = f"Reset session: {self.session_id} (run_id: {self.active_run_id})"
        return MCPCallToolResult(
            content=[MCPTextContent(text=msg)],
            isError=False,
            metadata={"session_id": self.session_id, "run_id": self.active_run_id},
        ).model_dump()

    # --------------------------------------------------------------------------
    # Resources
    # --------------------------------------------------------------------------

    def _handle_resources_list(self) -> dict[str, Any]:
        """Return list of available MCP resources."""
        resources = [
            MCPResource(
                uri="recoverybench://ledger/events",
                name="Current Session Ledger Events",
                description="Append-only record of all tool invocation attempts and committed side effects.",
            ),
            MCPResource(
                uri="recoverybench://scenario/active",
                name="Active Scenario Configuration",
                description="Scenario definition and active fault injection parameters.",
            ),
            MCPResource(
                uri="recoverybench://evaluation/latest",
                name="Latest Evaluation Report",
                description="Assertion evaluation results and side-effect intervals.",
            ),
        ]
        return {"resources": [r.model_dump() for r in resources]}

    def _handle_resources_read(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read content of an MCP resource by URI."""
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise MCPError(MCPErrorCode.INVALID_PARAMS, "URI must be a string")

        if uri == "recoverybench://ledger/events":
            events = self.ledger.get_events(self.active_run_id)
            serialized = [e.model_dump(mode="json") for e in events]
            text = json.dumps(serialized, indent=2)
        elif uri == "recoverybench://scenario/active":
            text = self.active_scenario.model_dump_json(indent=2)
        elif uri == "recoverybench://evaluation/latest":
            events = self.ledger.get_events(self.active_run_id)
            agent_res = AgentResult(success=True, status=CompletionStatus.SUCCEEDED)
            eval_res = self.evaluator.evaluate(
                events=events,
                assertions=self.active_scenario.assertions,
                agent_result=agent_res,
                run_id=self.active_run_id,
            )
            text = json.dumps(
                {
                    "verdict": eval_res.verdict.value,
                    "bounds": eval_res.bounds,
                    "assertions": [a.model_dump(mode="json") for a in eval_res.assertions],
                },
                indent=2,
            )
        else:
            raise MCPError(MCPErrorCode.RESOURCE_NOT_FOUND, f"Resource URI '{uri}' not found")

        content = MCPResourceContent(uri=uri, text=text)
        return {"contents": [content.model_dump()]}

    # --------------------------------------------------------------------------
    # Helpers
    # --------------------------------------------------------------------------

    def _format_error(self, req_id: str | int | None, exc: MCPError) -> dict[str, Any]:
        """Format an MCPError into a standard JSON-RPC 2.0 error response."""
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": exc.to_dict(),
        }

    # --------------------------------------------------------------------------
    # Stdio Transport Runner
    # --------------------------------------------------------------------------

    def run_stdio_sync(self) -> None:
        """Run blocking stdio transport loop for CLI execution."""
        # Ensure utf-8 stdio handling on Windows
        if sys.platform == "win32":
            if hasattr(sys.stdin, "reconfigure"):
                sys.stdin.reconfigure(encoding="utf-8")
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            response_json = self.handle_message(line)
            if response_json is not None:
                sys.stdout.write(response_json + "\n")
                sys.stdout.flush()

    async def run_stdio_async(self) -> None:
        """Run asynchronous stdio transport loop."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8").strip()
            if not line:
                continue
            response_json = self.handle_message(line)
            if response_json is not None:
                sys.stdout.write(response_json + "\n")
                sys.stdout.flush()


def serve_mcp(
    scenario: ScenarioModel | None = None,
    isolation: IsolationMode = IsolationMode.TRUSTED,
    tool_registry: ToolRegistry | None = None,
    ledger: Ledger | None = None,
) -> None:
    """Convenience entry point for starting the RecoveryBench MCP stdio server."""
    config = MCPServerConfig(scenario=scenario, isolation=isolation)
    server = RecoveryBenchMCPServer(
        config=config,
        tool_registry=tool_registry,
        ledger=ledger,
    )
    server.run_stdio_sync()
