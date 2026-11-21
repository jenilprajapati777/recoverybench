"""RecoveryBench: Local-first AI Agent Recovery Testing Platform.

"Test the failure before production does."
"""

import importlib
from typing import Any

from recoverybench.adapters.callable import (
    CallableAgentAdapter,
    agent,
    wrap_agent,
)
from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    AwaitableDict,
    CompletionStatus,
    TaskContext,
    ToolCaller,
    ToolClient,
)
from recoverybench.engine.context import RunResult
from recoverybench.engine.saga import (
    SagaOrchestrator,
    SagaWorkflowAgent,
    SagaWorkflowResult,
    StepExecutionRecord,
)
from recoverybench.errors import (
    CapacityBreachIntegrityError,
    ConcurrencyViolationError,
    ConflictingEventIdentityError,
    EvaluationInputValidationError,
    IdempotencyCorrelationError,
    IncompatibleSchemaVersionError,
    ProcessContainmentError,
    ProcessTaintedError,
    ProcessTerminatedError,
    ReportValidationError,
)
from recoverybench.evaluator.accounting import (
    AccountingResult,
    TwoLayerEffectAccountingEngine,
    derive_external_status,
)
from recoverybench.evaluator.rules import aggregate_outcome
from recoverybench.mcp import (
    MCPServerConfig,
    RecoveryBenchMCPServer,
    serve_mcp,
)
from recoverybench.models import (
    AbsentStatus,
    AttemptStateVector,
    AuthoritativeFinalityProof,
    CapacityProvenanceTier,
    CommitStatus,
    HarnessRuntimeAttestation,
    IdempotencyContract,
    IdempotencyGuarantee,
    IdempotencyScope,
    InventoryStatus,
    LateOutcomeRecord,
    PrimaryInfrastructureStatus,
    ReconciliationAuditEvidence,
    ReconciliationInfrastructureStatus,
    ReplayBehavior,
    SideEffectBounds,
    ToolExecutionModel,
)
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
    ProcessIsolatedExecutionBackend,
    SandboxConfig,
)
from recoverybench.scenarios.schema import (
    ScenarioAssertions,
    ScenarioModel,
    StepExecutionStatus,
    WorkflowStatus,
    WorkflowStep,
)
from recoverybench.sdk.tester import (
    RecoveryBenchTester,
    resolve_backend,
    resolve_ledger,
    resolve_tool_registry,
    test_agent,
    test_agent_async,
)
from recoverybench.tools import (
    DeclarativeToolService,
    MockPaymentService,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    ToolService,
    mock_tool,
)

__version__ = "0.2.0"

_LAZY_TOP_EXPORTS: dict[str, str] = {
    "AutoGenAdapter": "recoverybench.adapters.autogen",
    "CrewAIAdapter": "recoverybench.adapters.crewai",
    "LangGraphAdapter": "recoverybench.adapters.langgraph",
    "IdempotentLangGraphAgent": "recoverybench.adapters.langgraph",
    "NaiveLangGraphAgent": "recoverybench.adapters.langgraph",
    "create_langchain_payment_tool": "recoverybench.adapters.langgraph",
    "create_langchain_tool": "recoverybench.adapters.langgraph",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_TOP_EXPORTS:
        mod = importlib.import_module(_LAZY_TOP_EXPORTS[name])
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = [
    "__version__",
    "test_agent",
    "test_agent_async",
    "RecoveryBenchTester",
    "resolve_backend",
    "resolve_ledger",
    "resolve_tool_registry",
    "agent",
    "wrap_agent",
    "mock_tool",
    "DeclarativeToolService",
    "ToolRegistry",
    "ToolService",
    "ToolInvocation",
    "ToolResult",
    "MockPaymentService",
    "AgentAdapter",
    "AgentResult",
    "CompletionStatus",
    "TaskContext",
    "ToolCaller",
    "ToolClient",
    "AwaitableDict",
    "RunResult",
    "ReportModel",
    "ReportValidator",
    "ReportValidationError",
    "EvaluationInputValidationError",
    "CapacityBreachIntegrityError",
    "IdempotencyCorrelationError",
    "ConflictingEventIdentityError",
    "ConcurrencyViolationError",
    "IncompatibleSchemaVersionError",
    "ProcessTaintedError",
    "ProcessTerminatedError",
    "ProcessContainmentError",
    "PrimaryInfrastructureStatus",
    "ReconciliationInfrastructureStatus",
    "HarnessRuntimeAttestation",
    "ReconciliationAuditEvidence",
    "AuthoritativeFinalityProof",
    "CommitStatus",
    "CapacityProvenanceTier",
    "InventoryStatus",
    "AbsentStatus",
    "IdempotencyGuarantee",
    "IdempotencyScope",
    "ReplayBehavior",
    "IdempotencyContract",
    "AttemptStateVector",
    "SideEffectBounds",
    "ToolExecutionModel",
    "LateOutcomeRecord",
    "TwoLayerEffectAccountingEngine",
    "AccountingResult",
    "derive_external_status",
    "aggregate_outcome",
    "SideEffectSafety",
    "JSONReporter",
    "CURRENT_REPORT_SCHEMA_VERSION",
    "SUPPORTED_REPORT_SCHEMA_VERSIONS",
    "CallableAgentAdapter",
    "LangGraphAdapter",
    "CrewAIAdapter",
    "AutoGenAdapter",
    "NaiveLangGraphAgent",
    "IdempotentLangGraphAgent",
    "create_langchain_payment_tool",
    "create_langchain_tool",
    "AgentSpec",
    "ExecutionBackend",
    "InProcessExecutionBackend",
    "ProcessIsolatedExecutionBackend",
    "DockerExecutionBackend",
    "IsolationMode",
    "SandboxConfig",
    "RecoveryBenchMCPServer",
    "MCPServerConfig",
    "serve_mcp",
    "ScenarioModel",
    "ScenarioAssertions",
    "WorkflowStep",
    "WorkflowStatus",
    "StepExecutionStatus",
    "SagaOrchestrator",
    "SagaWorkflowAgent",
    "SagaWorkflowResult",
    "StepExecutionRecord",
]
