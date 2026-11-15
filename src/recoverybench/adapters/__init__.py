"""RecoveryBench Agent Integration Adapters.

Provides adapters for:
- Arbitrary Python callables (functions, lambdas, classes).
- LangGraph framework workflows and StateGraph agents.
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

__all__ = [
    "CallableAgentAdapter",
    "agent",
    "wrap_agent",
    "LangGraphAdapter",
    "NaiveLangGraphAgent",
    "IdempotentLangGraphAgent",
    "bridge_tools",
    "build_naive_langgraph_agent",
    "build_idempotent_langgraph_agent",
    "create_langchain_payment_tool",
]
