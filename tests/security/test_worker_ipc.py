"""Tests for worker IPC protocol framing, stdio communication, tool dispatch, and stderr secret masking."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from recoverybench.sandbox.protocol import (
    CompletionResponse,
    InitRequest,
    ReadyResponse,
    ToolCallRequest,
    ToolCallResponse,
    decode_message,
    encode_message,
)
from recoverybench.sandbox.worker import mask_secrets


def test_protocol_message_encoding_and_decoding() -> None:
    """Verify JSON Lines framing roundtrip for all protocol message types."""
    # 1. Init
    init_msg = InitRequest(
        scenario_id="scenario_abc",
        task_params={"amount": 100},
        spec={
            "module_path": "agent",
            "entrypoint": "run",
            "name": "test_agent",
        },
    )
    encoded = encode_message(init_msg)
    assert isinstance(encoded, str)
    assert encoded.endswith("\n")
    decoded = decode_message(encoded)
    assert decoded["type"] == "init"
    assert decoded["scenario_id"] == "scenario_abc"
    assert decoded["task_params"]["amount"] == 100

    # 2. Ready
    ready_msg = ReadyResponse(agent_id="test_agent")
    decoded_ready = decode_message(encode_message(ready_msg))
    assert decoded_ready["type"] == "ready"
    assert decoded_ready["agent_id"] == "test_agent"

    # 3. ToolCall & ToolResponse
    tool_req = ToolCallRequest(
        request_id="req_123",
        tool_name="payment_service",
        action="charge",
        params={"amount": 50},
        idempotency_key="key_xyz",
        operation_id="op_789",
    )
    decoded_req = decode_message(encode_message(tool_req))
    assert decoded_req["type"] == "tool_call"
    assert decoded_req["idempotency_key"] == "key_xyz"
    assert decoded_req["operation_id"] == "op_789"

    tool_resp = ToolCallResponse(
        request_id="req_123",
        success=True,
        data={"transaction_id": "tx_456"},
    )
    decoded_resp = decode_message(encode_message(tool_resp))
    assert decoded_resp["type"] == "tool_response"
    assert decoded_resp["data"]["transaction_id"] == "tx_456"

    # 4. Completion
    comp_msg = CompletionResponse(
        status="SUCCEEDED",
        success=True,
        data={"charge_completed": True},
    )
    decoded_comp = decode_message(encode_message(comp_msg))
    assert decoded_comp["type"] == "completion"
    assert decoded_comp["status"] == "SUCCEEDED"
    assert decoded_comp["success"] is True


def test_protocol_decoding_size_limit_rejection() -> None:
    """Verify that protocol frame exceeding max_bytes limit is rejected."""
    huge_data = "A" * (2 * 1024 * 1024)  # 2MB
    huge_line = json.dumps({"type": "ready", "agent_id": huge_data}) + "\n"

    with pytest.raises(ValueError) as exc_info:
        decode_message(huge_line, max_bytes=1024 * 1024)  # 1MB limit
    assert "exceeds size limit" in str(exc_info.value)


def test_protocol_decoding_malformed_json_rejection() -> None:
    """Verify invalid JSON is rejected."""
    with pytest.raises(ValueError):
        decode_message("NOT_JSON_DATA\n")

    with pytest.raises(ValueError):
        decode_message('{"missing_type": true}\n')


def test_worker_secret_masking() -> None:
    """Verify that credentials and tokens are redacted by worker mask_secrets."""
    test_key = "sk_" + "live_" + "abcdef123456789012345678"
    msg = f"Error connecting with {test_key} and token=tok_stripe_live_secret123"
    masked = mask_secrets(msg)
    assert test_key not in masked
    assert "[REDACTED_SECRET]" in masked

    bearer = "Authorization: Bearer secret_bearer_token_xyz12345"
    masked_bearer = mask_secrets(bearer)
    assert "secret_bearer_token_xyz12345" not in masked_bearer
    assert "[REDACTED_SECRET]" in masked_bearer


def test_worker_process_ipc_roundtrip(tmp_path: Path) -> None:
    """Launch worker.py directly as a child process and verify full IPC dialogue."""
    # 1. Create a dummy agent script in tmp_path
    agent_code = """
def run_agent(task, tool):
    print("Agent debug print to stdout - should go to stderr!")
    resp = tool("payment_service", "charge", {"amount": 250}, idempotency_key="idemp_key_1", operation_id=task.operation_id)
    return {"status": "SUCCEEDED", "success": True, "data": resp}
"""
    agent_file = tmp_path / "mock_agent.py"
    agent_file.write_text(agent_code, encoding="utf-8")

    worker_file = Path(__file__).resolve().parent.parent.parent / "src" / "recoverybench" / "sandbox" / "worker.py"
    assert worker_file.exists()

    # Launch worker script
    env = dict(PYTHONPATH=str(tmp_path))
    proc = subprocess.Popen(
        [sys.executable, "-u", str(worker_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
        env=env,
        text=True,
    )

    try:
        # Step A: Send InitRequest
        init_frame = {
            "type": "init",
            "version": "1.0",
            "spec": {
                "module_path": "mock_agent",
                "entrypoint": "run_agent",
                "name": "mock_agent",
            },
            "scenario_id": "test_scenario",
            "task_params": {"amount": 250},
        }
        proc.stdin.write(json.dumps(init_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        # Step B: Read ReadyResponse
        ready_line = proc.stdout.readline()  # type: ignore[union-attr]
        ready_data = json.loads(ready_line)
        assert ready_data["type"] == "ready"
        assert ready_data["agent_id"] == "mock_agent"

        # Step C: Send ExecuteRequest
        exec_frame = {
            "type": "execute",
            "version": "1.0",
            "scenario_id": "test_scenario",
            "task_params": {"amount": 250},
            "operation_id": "op_test_100",
        }
        proc.stdin.write(json.dumps(exec_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        # Step D: Read ToolCallRequest from worker
        tool_call_line = proc.stdout.readline()  # type: ignore[union-attr]
        tool_call_data = json.loads(tool_call_line)
        assert tool_call_data["type"] == "tool_call"
        assert tool_call_data["tool_name"] == "payment_service"
        assert tool_call_data["action"] == "charge"
        assert tool_call_data["params"]["amount"] == 250
        assert tool_call_data["idempotency_key"] == "idemp_key_1"
        assert tool_call_data["operation_id"] == "op_test_100"

        # Step E: Host replies with ToolCallResponse
        req_id = tool_call_data["request_id"]
        tool_resp_frame = {
            "type": "tool_response",
            "version": "1.0",
            "request_id": req_id,
            "success": True,
            "data": {"status": "committed", "tx_id": "tx_abc123"},
        }
        proc.stdin.write(json.dumps(tool_resp_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        # Step F: Read CompletionResponse
        comp_line = proc.stdout.readline()  # type: ignore[union-attr]
        comp_data = json.loads(comp_line)
        assert comp_data["type"] == "completion"
        assert comp_data["status"] == "SUCCEEDED"
        assert comp_data["success"] is True
        assert comp_data["data"]["status"] == "SUCCEEDED"
        assert comp_data["data"]["data"]["status"] == "committed"

        # Step G: Send ShutdownRequest
        shutdown_frame = {"type": "shutdown", "version": "1.0"}
        proc.stdin.write(json.dumps(shutdown_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        proc.wait(timeout=5.0)
        assert proc.returncode == 0

        # Verify agent's print statement was redirected to stderr, not corrupting stdout
        stderr_output = proc.stderr.read()  # type: ignore[union-attr]
        assert "Agent debug print to stdout" in stderr_output

    finally:
        if proc.poll() is None:
            proc.kill()


def test_worker_process_handles_agent_crash(tmp_path: Path) -> None:
    """Launch worker.py with an agent that raises an exception; verify completion with FAILED status."""
    crash_key = "sk_" + "live_" + "abcdef1234567890"
    agent_code = f"""
def crashing_agent(task, tool):
    raise RuntimeError("Critical agent crash with secret {crash_key}!")
"""
    agent_file = tmp_path / "crash_agent.py"
    agent_file.write_text(agent_code, encoding="utf-8")

    worker_file = Path(__file__).resolve().parent.parent.parent / "src" / "recoverybench" / "sandbox" / "worker.py"

    proc = subprocess.Popen(
        [sys.executable, "-u", str(worker_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
        text=True,
    )

    try:
        init_frame = {
            "type": "init",
            "version": "1.0",
            "spec": {"module_path": "crash_agent", "entrypoint": "crashing_agent"},
            "scenario_id": "crash_scenario",
            "task_params": {},
        }
        proc.stdin.write(json.dumps(init_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        ready_line = proc.stdout.readline()  # type: ignore[union-attr]
        assert json.loads(ready_line)["type"] == "ready"

        exec_frame = {"type": "execute", "version": "1.0", "scenario_id": "crash_scenario", "task_params": {}}
        proc.stdin.write(json.dumps(exec_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        comp_line = proc.stdout.readline()  # type: ignore[union-attr]
        comp_data = json.loads(comp_line)
        assert comp_data["type"] == "completion"
        assert comp_data["status"] == "FAILED"
        assert comp_data["success"] is False
        # Secret should have been redacted
        assert crash_key not in comp_data["error"]
        assert "[REDACTED_SECRET]" in comp_data["error"]

        shutdown_frame = {"type": "shutdown", "version": "1.0"}
        proc.stdin.write(json.dumps(shutdown_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        proc.wait(timeout=5.0)
    finally:
        if proc.poll() is None:
            proc.kill()


def test_worker_reports_missing_declared_dependencies(tmp_path: Path) -> None:
    """Verify worker returns MissingDependencyError when declared dependencies are missing."""
    agent_code = "def run(t, c): return True\n"
    (tmp_path / "valid_agent.py").write_text(agent_code, encoding="utf-8")
    worker_file = Path(__file__).resolve().parent.parent.parent / "src" / "recoverybench" / "sandbox" / "worker.py"

    proc = subprocess.Popen(
        [sys.executable, "-u", str(worker_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
        text=True,
    )

    try:
        init_frame = {
            "type": "init",
            "version": "1.0",
            "spec": {
                "module_path": "valid_agent",
                "entrypoint": "run",
                "dependencies": ["non_existent_package_xyz123>=1.0"],
            },
            "scenario_id": "test_dep_scen",
            "task_params": {},
        }
        proc.stdin.write(json.dumps(init_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        resp_line = proc.stdout.readline()  # type: ignore[union-attr]
        resp_data = json.loads(resp_line)

        assert resp_data["type"] == "error"
        assert resp_data["error_type"] == "MissingDependencyError"
        assert "non_existent_package_xyz123" in resp_data["message"]
        assert "--network=none" in resp_data["message"]
    finally:
        if proc.poll() is None:
            proc.kill()


def test_worker_reports_missing_import_dependencies(tmp_path: Path) -> None:
    """Verify worker returns MissingDependencyError when an uninstalled module is imported."""
    agent_code = "import nonexistent_custom_lib_456\ndef run(t, c): return True\n"
    (tmp_path / "broken_import_agent.py").write_text(agent_code, encoding="utf-8")
    worker_file = Path(__file__).resolve().parent.parent.parent / "src" / "recoverybench" / "sandbox" / "worker.py"

    proc = subprocess.Popen(
        [sys.executable, "-u", str(worker_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(tmp_path),
        text=True,
    )

    try:
        init_frame = {
            "type": "init",
            "version": "1.0",
            "spec": {
                "module_path": "broken_import_agent",
                "entrypoint": "run",
            },
            "scenario_id": "test_import_scen",
            "task_params": {},
        }
        proc.stdin.write(json.dumps(init_frame) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

        resp_line = proc.stdout.readline()  # type: ignore[union-attr]
        resp_data = json.loads(resp_line)

        assert resp_data["type"] == "error"
        assert resp_data["error_type"] == "MissingDependencyError"
        assert "nonexistent_custom_lib_456" in resp_data["message"]
        assert "--network=none" in resp_data["message"]
    finally:
        if proc.poll() is None:
            proc.kill()
