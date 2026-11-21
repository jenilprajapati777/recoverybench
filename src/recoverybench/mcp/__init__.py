"""RecoveryBench Model Context Protocol (MCP) Server Bridge.

Exposes RecoveryBench tool execution, fault injection, and evaluation
capabilities to external AI agents via the standard Model Context Protocol.
"""

from recoverybench.mcp.errors import MCPError, MCPErrorCode
from recoverybench.mcp.protocol import (
    JSONRPCErrorObject,
    JSONRPCRequest,
    JSONRPCResponse,
    MCPCallToolResult,
    MCPResource,
    MCPResourceContent,
    MCPTextContent,
    MCPTool,
)
from recoverybench.mcp.server import (
    MCPServerConfig,
    RecoveryBenchMCPServer,
    serve_mcp,
)

__all__ = [
    "JSONRPCErrorObject",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "MCPCallToolResult",
    "MCPError",
    "MCPErrorCode",
    "MCPResource",
    "MCPResourceContent",
    "MCPServerConfig",
    "MCPTextContent",
    "MCPTool",
    "RecoveryBenchMCPServer",
    "serve_mcp",
]
