"""RecoveryBench Developer SDK.

Public API for authoring, wrapping, and evaluating AI agents:
- test_agent: One-line test function for CI/CD and pytest suites.
- RecoveryBenchTester: Reusable testing harness and context manager.
- wrap_agent: Wrap arbitrary functions into AgentAdapter.
- @agent: Decorator for creating RecoveryBench agent adapters.
- LangGraphAdapter: First-class adapter for LangGraph StateGraph agents.
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
    bridge_tools,
    build_idempotent_langgraph_agent,
    build_naive_langgraph_agent,
    create_langchain_payment_tool,
)
from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    TaskContext,
    ToolCaller,
)
from recoverybench.engine.context import RunResult
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
    resolve_scenario,
    test_agent,
)

__all__ = [
    "test_agent",
    "RecoveryBenchTester",
    "resolve_scenario",
    "agent",
    "wrap_agent",
    "CallableAgentAdapter",
    "LangGraphAdapter",
    "NaiveLangGraphAgent",
    "IdempotentLangGraphAgent",
    "bridge_tools",
    "build_naive_langgraph_agent",
    "build_idempotent_langgraph_agent",
    "create_langchain_payment_tool",
    "AgentAdapter",
    "AgentResult",
    "TaskContext",
    "ToolCaller",
    "RunResult",
    "AgentSpec",
    "ExecutionBackend",
    "InProcessExecutionBackend",
    "DockerExecutionBackend",
    "IsolationMode",
    "SandboxConfig",
]
