"""Mock tool services and interfaces."""

from recoverybench.tools.base import (
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    ToolService,
    demonstrate_runtime_capacity_enforcement,
)
from recoverybench.tools.declarative import (
    DeclarativeToolService,
    mock_tool,
)
from recoverybench.tools.payment import MockPaymentService
from recoverybench.tools.process_runner import (
    ProcessExecutionResult,
    ToolProcessRunner,
)
from recoverybench.tools.testing import (
    UncooperativeErrorTool,
    UncooperativeHangingTool,
    UncooperativePidTool,
    UncooperativeSuccessTool,
)

__all__ = [
    "ToolInvocation",
    "ToolResult",
    "ToolService",
    "ToolRegistry",
    "MockPaymentService",
    "DeclarativeToolService",
    "mock_tool",
    "demonstrate_runtime_capacity_enforcement",
    "ToolProcessRunner",
    "ProcessExecutionResult",
    "UncooperativePidTool",
    "UncooperativeHangingTool",
    "UncooperativeErrorTool",
    "UncooperativeSuccessTool",
]
