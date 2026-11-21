"""RecoveryBench Agent Integration Adapters.

Provides adapters for:
- Arbitrary Python callables (functions, lambdas, classes).
- LangGraph framework workflows and StateGraph agents.
- CrewAI autonomous crews and tools.
- AutoGen / AG2 multi-agent conversational workflows.
"""

import importlib
from typing import Any

from recoverybench.adapters.callable import (
    CallableAgentAdapter,
    agent,
    wrap_agent,
)

_LAZY_ADAPTER_EXPORTS: dict[str, str] = {
    "AutoGenAdapter": "recoverybench.adapters.autogen",
    "create_autogen_payment_tool": "recoverybench.adapters.autogen",
    "create_autogen_tool": "recoverybench.adapters.autogen",
    "parse_chat_result": "recoverybench.adapters.autogen",
    "CrewAIAdapter": "recoverybench.adapters.crewai",
    "BridgedCrewAITool": "recoverybench.adapters.crewai",
    "create_crewai_payment_tool": "recoverybench.adapters.crewai",
    "create_crewai_tool": "recoverybench.adapters.crewai",
    "parse_crew_output": "recoverybench.adapters.crewai",
    "LangGraphAdapter": "recoverybench.adapters.langgraph",
    "IdempotentLangGraphAgent": "recoverybench.adapters.langgraph",
    "NaiveLangGraphAgent": "recoverybench.adapters.langgraph",
    "bridge_tools": "recoverybench.adapters.langgraph",
    "build_naive_langgraph_agent": "recoverybench.adapters.langgraph",
    "build_idempotent_langgraph_agent": "recoverybench.adapters.langgraph",
    "create_langchain_payment_tool": "recoverybench.adapters.langgraph",
    "create_langchain_tool": "recoverybench.adapters.langgraph",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_ADAPTER_EXPORTS:
        mod = importlib.import_module(_LAZY_ADAPTER_EXPORTS[name])
        val = getattr(mod, name)
        globals()[name] = val
        return val
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


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
    "create_langchain_tool",
    "CrewAIAdapter",
    "BridgedCrewAITool",
    "create_crewai_tool",
    "create_crewai_payment_tool",
    "parse_crew_output",
    "AutoGenAdapter",
    "create_autogen_tool",
    "create_autogen_payment_tool",
    "parse_chat_result",
]
