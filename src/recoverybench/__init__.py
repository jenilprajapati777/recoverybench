"""RecoveryBench: Local-first AI Agent Recovery Testing Platform.

"Test the failure before production does."
"""

from recoverybench.adapters.callable import (
    CallableAgentAdapter,
    agent,
    wrap_agent,
)
from recoverybench.adapters.langgraph import (
    IdempotentLangGraphAgent,
    LangGraphAdapter,
    NaiveLangGraphAgent,
)
from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolCaller,
)
from recoverybench.engine.context import RunResult
from recoverybench.errors import ReportValidationError
from recoverybench.reporting import (
    CURRENT_REPORT_SCHEMA_VERSION,
    SUPPORTED_REPORT_SCHEMA_VERSIONS,
    JSONReporter,
    ReportModel,
    ReportValidator,
    SideEffectSafety,
)
from recoverybench.sandbox import (
    AgentSpec,
    DockerExecutionBackend,
    ExecutionBackend,
    InProcessExecutionBackend,
    IsolationMode,
    SandboxConfig,
)
from recoverybench.sdk.tester import (
    RecoveryBenchTester,
    resolve_backend,
    resolve_ledger,
    test_agent,
)

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("recoverybench")
except PackageNotFoundError:
    __version__ = "0.1.0"
except Exception:
    __version__ = "0.1.0"

__all__ = [
    "__version__",
    "test_agent",
    "RecoveryBenchTester",
    "resolve_backend",
    "resolve_ledger",
    "agent",
    "wrap_agent",
    "AgentAdapter",
    "AgentResult",
    "CompletionStatus",
    "TaskContext",
    "ToolCaller",
    "RunResult",
    "ReportModel",
    "ReportValidator",
    "ReportValidationError",
    "SideEffectSafety",
    "JSONReporter",
    "CURRENT_REPORT_SCHEMA_VERSION",
    "SUPPORTED_REPORT_SCHEMA_VERSIONS",
    "CallableAgentAdapter",
    "LangGraphAdapter",
    "NaiveLangGraphAgent",
    "IdempotentLangGraphAgent",
    "AgentSpec",
    "ExecutionBackend",
    "InProcessExecutionBackend",
    "DockerExecutionBackend",
    "IsolationMode",
    "SandboxConfig",
]
