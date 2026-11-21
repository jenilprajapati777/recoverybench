"""Model Context Protocol (MCP) data structures and JSON-RPC 2.0 schemas."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class JSONRPCMessage(BaseModel):
    """Base JSON-RPC 2.0 message."""

    model_config = ConfigDict(extra="ignore")
    jsonrpc: Literal["2.0"] = "2.0"


class JSONRPCRequest(JSONRPCMessage):
    """JSON-RPC 2.0 Request or Notification."""

    id: str | int | None = None
    method: str
    params: dict[str, Any] | list[Any] | None = None

    @property
    def is_notification(self) -> bool:
        """Check if message is a notification (no ID)."""
        return self.id is None


class JSONRPCErrorObject(BaseModel):
    """JSON-RPC 2.0 Error Object."""

    model_config = ConfigDict(extra="allow")
    code: int
    message: str
    data: Any | None = None


class JSONRPCResponse(JSONRPCMessage):
    """JSON-RPC 2.0 Response."""

    id: str | int | None = None
    result: Any | None = None
    error: JSONRPCErrorObject | None = None


class MCPTextContent(BaseModel):
    """Standard MCP text content item."""

    type: Literal["text"] = "text"
    text: str


class MCPTool(BaseModel):
    """MCP tool definition exposed to clients."""

    name: str
    description: str | None = None
    inputSchema: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class MCPCallToolResult(BaseModel):
    """Result of an MCP tool invocation."""

    content: list[MCPTextContent] = Field(default_factory=list)
    isError: bool = False
    metadata: dict[str, Any] | None = None


class MCPResource(BaseModel):
    """MCP resource metadata."""

    uri: str
    name: str
    description: str | None = None
    mimeType: str | None = "application/json"


class MCPResourceContent(BaseModel):
    """Content of an MCP resource."""

    uri: str
    mimeType: str | None = "application/json"
    text: str


class ServerCapabilities(BaseModel):
    """Capabilities announced by RecoveryBench MCP Server."""

    tools: dict[str, bool] = Field(default_factory=lambda: {"listChanged": False})
    resources: dict[str, bool] = Field(default_factory=lambda: {"subscribe": False, "listChanged": False})


class ImplementationInfo(BaseModel):
    """Server or client implementation identification."""

    name: str
    version: str


class InitializeResult(BaseModel):
    """Response payload for MCP initialize handshake."""

    protocolVersion: str = "2024-11-05"
    capabilities: ServerCapabilities = Field(default_factory=ServerCapabilities)
    serverInfo: ImplementationInfo
