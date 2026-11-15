"""IPC protocol message schemas and frame encoding for host-worker communication."""

import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ProtocolMessageType(StrEnum):
    """Enumeration of valid IPC protocol message types."""

    INIT = "init"
    READY = "ready"
    EXECUTE = "execute"
    TOOL_CALL = "tool_call"
    TOOL_RESPONSE = "tool_response"
    COMPLETION = "completion"
    ERROR = "error"
    SHUTDOWN = "shutdown"


class ProtocolMessage(BaseModel):
    """Base model for all framed IPC protocol messages."""

    model_config = ConfigDict(extra="forbid")

    type: ProtocolMessageType
    version: str = Field(default="1.0")


class InitRequest(ProtocolMessage):
    """Host request to initialize the worker with an AgentSpec."""

    type: ProtocolMessageType = ProtocolMessageType.INIT
    spec: dict[str, Any]
    scenario_id: str
    task_params: dict[str, Any] = Field(default_factory=dict)


class ReadyResponse(ProtocolMessage):
    """Worker acknowledgement that initialization is complete."""

    type: ProtocolMessageType = ProtocolMessageType.READY
    agent_id: str


class ExecuteRequest(ProtocolMessage):
    """Host instruction to begin executing the agent."""

    type: ProtocolMessageType = ProtocolMessageType.EXECUTE
    scenario_id: str
    task_params: dict[str, Any] = Field(default_factory=dict)
    operation_id: str | None = None


class ToolCallRequest(ProtocolMessage):
    """Worker request to dispatch a tool mutation or query to the host."""

    type: ProtocolMessageType = ProtocolMessageType.TOOL_CALL
    request_id: str
    tool_name: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None


class ToolCallResponse(ProtocolMessage):
    """Host response to a tool call dispatched through simulated services."""

    type: ProtocolMessageType = ProtocolMessageType.TOOL_RESPONSE
    request_id: str
    success: bool
    data: dict[str, Any] | None = None
    error: str | None = None


class CompletionResponse(ProtocolMessage):
    """Worker notification of agent completion."""

    type: ProtocolMessageType = ProtocolMessageType.COMPLETION
    status: str
    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = Field(default=1, ge=1)
    status_message: str = Field(default="")


class ErrorResponse(ProtocolMessage):
    """Structured error message for protocol or runtime failures."""

    type: ProtocolMessageType = ProtocolMessageType.ERROR
    error_type: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ShutdownRequest(ProtocolMessage):
    """Host instruction to gracefully shut down the worker."""

    type: ProtocolMessageType = ProtocolMessageType.SHUTDOWN


def encode_message(msg: BaseModel | dict[str, Any]) -> str:
    """Encode a message into a single-line JSON string ending with newline."""
    data = msg.model_dump() if isinstance(msg, BaseModel) else msg
    line = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
    return line + "\n"


def decode_message(line: str, max_bytes: int = 1_048_576) -> dict[str, Any]:
    """Decode and validate a single JSON Lines protocol frame."""
    raw_bytes = line.encode("utf-8")
    if len(raw_bytes) > max_bytes:
        raise ValueError(f"IPC protocol message exceeds size limit ({len(raw_bytes)} > {max_bytes} bytes)")

    clean = line.strip()
    if not clean:
        raise ValueError("Empty IPC message frame received")

    try:
        data = json.loads(clean)
    except json.JSONDecodeError as jde:
        raise ValueError(f"Malformed JSON frame received over IPC: {jde}") from jde

    if not isinstance(data, dict):
        raise ValueError(f"IPC message frame must be a JSON object, got {type(data).__name__}")

    if "type" not in data:
        raise ValueError("IPC message missing required 'type' field")

    return data
