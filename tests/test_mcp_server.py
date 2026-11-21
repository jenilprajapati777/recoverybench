"""Tests for RecoveryBench Model Context Protocol (MCP) Server Bridge (Phase 4.3)."""

import json
from typing import Any

import pytest

from recoverybench.faults.types import FaultConfig, FaultType
from recoverybench.mcp.errors import MCPErrorCode
from recoverybench.mcp.server import MCPServerConfig, RecoveryBenchMCPServer
from recoverybench.models import CommitStatus, ExecutionStatus
from recoverybench.sandbox.backend import IsolationMode
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.testing import UncooperativeHangingTool, UncooperativePidTool


class TestMCPServer:
    """Comprehensive test suite for Model Context Protocol (MCP) Server Bridge."""

    @staticmethod
    def _call(server: RecoveryBenchMCPServer, payload: Any) -> Any:
        msg = payload if isinstance(payload, str) else json.dumps(payload)
        res_str = server.handle_message(msg)
        assert res_str is not None
        return json.loads(res_str)

    @pytest.fixture
    def server(self) -> RecoveryBenchMCPServer:
        """Create a default MCP server instance with in-memory ledger."""
        config = MCPServerConfig(
            server_name="test-recoverybench-mcp",
            server_version="0.2.0",
        )
        return RecoveryBenchMCPServer(config=config)

    def test_initialize_handshake(self, server: RecoveryBenchMCPServer) -> None:
        """Verify initialize request returns protocol version and server capabilities."""
        req = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0.0"},
            },
        }
        res_str = server.handle_message(json.dumps(req))
        assert res_str is not None
        res = json.loads(res_str)

        assert res["jsonrpc"] == "2.0"
        assert res["id"] == 1
        assert "result" in res
        result = res["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "test-recoverybench-mcp"
        assert result["serverInfo"]["version"] == "0.2.0"
        assert "tools" in result["capabilities"]
        assert server.is_initialized is True

    def test_notification_initialized(self, server: RecoveryBenchMCPServer) -> None:
        """Verify notifications/initialized produces no response (notification rule)."""
        req = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        }
        res_str = server.handle_message(json.dumps(req))
        assert res_str is None

    def test_ping(self, server: RecoveryBenchMCPServer) -> None:
        """Verify ping request returns empty success result."""
        req = {"jsonrpc": "2.0", "id": "ping-42", "method": "ping"}
        res_str = server.handle_message(json.dumps(req))
        assert res_str is not None
        res = json.loads(res_str)
        assert res["id"] == "ping-42"
        assert res["result"] == {}

    def test_tools_list_discovery(self, server: RecoveryBenchMCPServer) -> None:
        """Verify tools/list exposes registered tool services and schemas."""
        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        res_str = server.handle_message(json.dumps(req))
        assert res_str is not None
        res = json.loads(res_str)

        assert "result" in res
        tools = res["result"]["tools"]
        tool_names = [t["name"] for t in tools]
        assert "payment_gateway" in tool_names
        assert "payment_gateway_charge" in tool_names
        assert "recoverybench_evaluate" in tool_names
        assert "recoverybench_reset_session" in tool_names

        # Inspect charge tool schema
        charge_tool = next(t for t in tools if t["name"] == "payment_gateway_charge")
        assert "inputSchema" in charge_tool
        assert "amount" in charge_tool["inputSchema"]["properties"]

    def test_tools_call_success(self, server: RecoveryBenchMCPServer) -> None:
        """Verify tools/call executes tool, appends event to ledger, and returns text content."""
        req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "payment_gateway_charge",
                "arguments": {
                    "amount": 49.99,
                    "customer_id": "cust_123",
                    "idempotency_key": "idemp_test_1",
                    "operation_id": "op_test_1",
                },
            },
        }
        res_str = server.handle_message(json.dumps(req))
        assert res_str is not None
        res = json.loads(res_str)

        assert res["id"] == 3
        result = res["result"]
        assert result["isError"] is False
        assert len(result["content"]) == 1
        content_text = json.loads(result["content"][0]["text"])
        assert content_text["status"] == "succeeded"
        assert content_text["amount"] == 49.99
        assert result["metadata"]["commit_status"] == "COMMITTED"

        # Verify ledger event was appended
        events = server.ledger.get_events(server.active_run_id)
        assert len(events) == 1
        event = events[0]
        assert event.tool_name == "payment_gateway"
        assert event.action_type == "charge"
        assert event.execution_status == ExecutionStatus.SUCCESS
        assert event.commit_status == CommitStatus.COMMITTED

    def test_tools_call_idempotency_replay(self, server: RecoveryBenchMCPServer) -> None:
        """Verify identical idempotency key returns cached result with same effect_id."""
        req = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "payment_gateway_charge",
                "arguments": {
                    "amount": 75.0,
                    "customer_id": "cust_replay",
                    "idempotency_key": "idemp_replay_42",
                    "operation_id": "op_replay_42",
                },
            },
        }
        res1 = self._call(server, req)["result"]

        # Call again with same idempotency key
        res2 = self._call(server, req)["result"]

        assert res1["metadata"]["effect_id"] == res2["metadata"]["effect_id"]
        events = server.ledger.get_events(server.active_run_id)
        assert len(events) == 2
        assert events[0].effect_id == events[1].effect_id

    def test_tools_call_fault_injection(self) -> None:
        """Verify tools/call injects configured fault and records failure with UNCERTAIN status."""
        scenario = ScenarioModel(
            schema_version=1,
            scenario_id="mcp_fault_test",
            name="MCP Fault Test",
            description="MCP Fault Test Description",
            target_tool="payment_gateway",
            task={"operation_id": "op_fault_test"},
            faults=[
                FaultConfig(
                    type=FaultType.TIMEOUT_AFTER_COMMIT,
                    target_tool="payment_gateway",
                    target_action="charge",
                    failure_count=1,
                    error_message="Injected Timeout After Commit",
                )
            ],
            assertions=ScenarioAssertions(),
        )
        config = MCPServerConfig(scenario=scenario)
        server = RecoveryBenchMCPServer(config=config)

        req = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "payment_gateway_charge",
                "arguments": {
                    "amount": 100.0,
                    "customer_id": "cust_err",
                },
            },
        }
        res = self._call(server, req)["result"]

        assert res["isError"] is True
        assert "Injected Fault" in res["content"][0]["text"]
        assert res["metadata"]["commit_status"] == "UNCERTAIN"

        events = server.ledger.get_events(server.active_run_id)
        assert len(events) == 1
        assert events[0].commit_status == CommitStatus.UNCERTAIN

    def test_tools_call_uncooperative_in_isolated_mode(self) -> None:
        """Verify uncooperative sync tool executes in isolated OS child process via MCP."""
        registry = ToolRegistry()
        registry.register(UncooperativePidTool(timeout_seconds=15.0))
        config = MCPServerConfig(isolation=IsolationMode.ISOLATED)
        server = RecoveryBenchMCPServer(config=config, tool_registry=registry)

        req = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "pid_tool",
                "arguments": {"action": "get_pid", "params": {}},
            },
        }
        res = self._call(server, req)["result"]

        assert res["isError"] is False
        pid_data = json.loads(res["content"][0]["text"])
        import os
        assert pid_data["pid"] != os.getpid()

    def test_tools_call_uncooperative_in_trusted_mode_fails_closed(self) -> None:
        """Verify uncooperative sync tool is rejected fail-closed in trusted mode."""
        registry = ToolRegistry()
        registry.register(UncooperativeHangingTool())
        config = MCPServerConfig(isolation=IsolationMode.TRUSTED)
        server = RecoveryBenchMCPServer(config=config, tool_registry=registry)

        req = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "hanging_tool",
                "arguments": {"action": "hang", "params": {}},
            },
        }
        res = self._call(server, req)
        assert "error" in res
        assert res["error"]["code"] == MCPErrorCode.TOOL_EXECUTION_ERROR
        assert "requires process isolation" in res["error"]["message"]

    def test_resources_list_and_read(self, server: RecoveryBenchMCPServer) -> None:
        """Verify resources/list and resources/read return ledger events and scenario config."""
        # Make a tool call first
        server.handle_message(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 10,
                "method": "tools/call",
                "params": {
                    "name": "payment_gateway_charge",
                    "arguments": {"amount": 20.0, "customer_id": "cust_res"},
                },
            })
        )

        # List resources
        list_req = {"jsonrpc": "2.0", "id": 11, "method": "resources/list"}
        list_res = self._call(server, list_req)["result"]
        uris = [r["uri"] for r in list_res["resources"]]
        assert "recoverybench://ledger/events" in uris
        assert "recoverybench://scenario/active" in uris
        assert "recoverybench://evaluation/latest" in uris

        # Read ledger events resource
        read_req = {
            "jsonrpc": "2.0",
            "id": 12,
            "method": "resources/read",
            "params": {"uri": "recoverybench://ledger/events"},
        }
        read_res = self._call(server, read_req)["result"]
        assert len(read_res["contents"]) == 1
        events_json = json.loads(read_res["contents"][0]["text"])
        assert len(events_json) == 1
        assert events_json[0]["tool_name"] == "payment_gateway"

    def test_recoverybench_evaluate_tool(self, server: RecoveryBenchMCPServer) -> None:
        """Verify recoverybench_evaluate tool returns assertion and side-effect summary."""
        # 1. Execute a charge
        server.handle_message(
            json.dumps({
                "jsonrpc": "2.0",
                "id": 20,
                "method": "tools/call",
                "params": {
                    "name": "payment_gateway_charge",
                    "arguments": {"amount": 15.0, "customer_id": "cust_eval"},
                },
            })
        )

        # 2. Evaluate
        eval_req = {
            "jsonrpc": "2.0",
            "id": 21,
            "method": "tools/call",
            "params": {"name": "recoverybench_evaluate", "arguments": {}},
        }
        res = self._call(server, eval_req)["result"]
        eval_summary = json.loads(res["content"][0]["text"])
        assert eval_summary["run_id"] == server.active_run_id
        assert eval_summary["event_count"] == 1
        assert "side_effect_bounds" in eval_summary

    def test_invalid_json_parse_error(self, server: RecoveryBenchMCPServer) -> None:
        """Verify malformed JSON string returns JSON-RPC -32700 Parse error."""
        res_str = server.handle_message("NOT A VALID JSON STRING {")
        assert res_str is not None
        res = json.loads(res_str)
        assert res["error"]["code"] == MCPErrorCode.PARSE_ERROR
        assert "Parse error" in res["error"]["message"]

    def test_unknown_method_error(self, server: RecoveryBenchMCPServer) -> None:
        """Verify unhandled method returns JSON-RPC -32601 Method not found."""
        req = {"jsonrpc": "2.0", "id": 99, "method": "unknown_future_method"}
        res = self._call(server, req)
        assert res["error"]["code"] == MCPErrorCode.METHOD_NOT_FOUND

    def test_batch_jsonrpc_requests(self, server: RecoveryBenchMCPServer) -> None:
        """Verify batch JSON-RPC request returns array of responses."""
        batch = [
            {"jsonrpc": "2.0", "id": 101, "method": "ping"},
            {"jsonrpc": "2.0", "id": 102, "method": "tools/list"},
        ]
        res_str = server.handle_message(json.dumps(batch))
        assert res_str is not None
        res = json.loads(res_str)
        assert isinstance(res, list)
        assert len(res) == 2
        assert res[0]["id"] == 101
        assert res[1]["id"] == 102

    def test_notification_never_returns_response_even_if_method_unknown(self, server: RecoveryBenchMCPServer) -> None:
        """Verify JSON-RPC 2.0 rule: notifications never produce a response, even on unknown methods."""
        req = {"jsonrpc": "2.0", "method": "custom/random_unknown_notification", "params": {"foo": "bar"}}
        res_str = server.handle_message(json.dumps(req))
        assert res_str is None

    def test_client_cancellation_notification(self, server: RecoveryBenchMCPServer) -> None:
        """Verify client cancellation notification is processed cleanly without emitting a response."""
        req = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "tool-call-123"}}
        res_str = server.handle_message(json.dumps(req))
        assert res_str is None

    def test_protocol_version_negotiation(self, server: RecoveryBenchMCPServer) -> None:
        """Verify protocol version negotiation in initialize handshake."""
        req = {
            "jsonrpc": "2.0",
            "id": "init-99",
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        }
        res = self._call(server, req)["result"]
        assert res["protocolVersion"] == "2024-11-05"

    def test_recoverybench_reset_session_tool(self, server: RecoveryBenchMCPServer) -> None:
        """Verify recoverybench_reset_session generates a fresh run_id and clears state."""
        old_run_id = server.active_run_id
        req = {
            "jsonrpc": "2.0",
            "id": 88,
            "method": "tools/call",
            "params": {"name": "recoverybench_reset_session", "arguments": {}},
        }
        res = self._call(server, req)["result"]
        assert "Reset session:" in res["content"][0]["text"]
        assert server.active_run_id != old_run_id
