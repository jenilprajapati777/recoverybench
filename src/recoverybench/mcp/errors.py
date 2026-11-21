"""Standard JSON-RPC 2.0 and MCP error codes and exception classes."""

from typing import Any


class MCPErrorCode:
    """Standard JSON-RPC 2.0 and MCP protocol error codes."""

    # JSON-RPC 2.0 Predefined Error Codes
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # MCP-specific Server Error Codes (-32000 to -32099)
    SERVER_NOT_INITIALIZED = -32002
    RESOURCE_NOT_FOUND = -32001
    TOOL_EXECUTION_ERROR = -32000
    PROCESS_TERMINATION_ERROR = -32003


class MCPError(Exception):
    """Base exception for MCP server bridge errors."""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def to_dict(self) -> dict[str, Any]:
        """Serialize error to JSON-RPC 2.0 error object."""
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            result["data"] = self.data
        return result
