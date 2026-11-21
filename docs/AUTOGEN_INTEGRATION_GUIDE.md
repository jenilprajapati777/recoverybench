# RecoveryBench — AutoGen Integration Guide

**Version:** 0.2.0
**Status:** Official Guide
**Applies to:** RecoveryBench Phase 4.4+

---

## 1. Overview

Microsoft AutoGen (and AG2) coordinates multi-agent conversational workflows centered around `ConversableAgent`, `UserProxyAgent`, and `AssistantAgent`.

The RecoveryBench AutoGen Adapter (`AutoGenAdapter` in `recoverybench.adapters.autogen`) enables:
- Registering RecoveryBench mock tools as callable Python functions on AutoGen agents.
- Intercepting agent tool calls with deterministic network drops, timeouts, and HTTP 500 errors.
- Evaluating whether agents safely retry with stable idempotency keys or crash.
- Capturing conversation histories and termination signals into RecoveryBench test reports.

---

## 2. Installation & Optional Dependencies

AutoGen is an optional integration dependency:

```powershell
pip install "recoverybench[autogen]"
```
Or directly:
```powershell
pip install autogen-agentchat pyautogen
```

---

## 3. How It Works

RecoveryBench converts its mock tool services into typed Python functions via `create_autogen_tool()` or `create_autogen_payment_tool()`:

```
┌──────────────────────────────────────────────────────────────┐
│                       AutoGen System                         │
│  ┌────────────────────────┐      Messages      ┌──────────┐  │
│  │ UserProxyAgent         │◄──────────────────►│Assistant │  │
│  └───────────┬────────────┘                    └──────────┘  │
│              │ executes registered function                  │
│  ┌───────────▼────────────────────────────────────────────┐  │
│  │ Bridged Function: payment_gateway_charge()             │  │
│  └───────────────────────────┬────────────────────────────┘  │
└──────────────────────────────┼───────────────────────────────┘
                               │ tool_call()
┌──────────────────────────────▼───────────────────────────────┐
│           RecoveryBench Execution Engine & Ledger            │
│  - Injected Faults (Network Drops, Timeouts, 500s)           │
│  - Append-Only Event Ledger                                  │
│  - Conservative Side-Effect Accounting                       │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. Usage Example

### Defining an AutoGen Agent Pair and Evaluating Resilience

```python
from autogen import AssistantAgent, UserProxyAgent
import recoverybench as rb
from recoverybench.adapters.autogen import AutoGenAdapter, create_autogen_payment_tool

def build_autogen_agents(tools: list) -> tuple[UserProxyAgent, AssistantAgent]:
    """Factory creating user_proxy and assistant with registered RecoveryBench tools."""
    user_proxy = UserProxyAgent(
        name="user_proxy",
        human_input_mode="NEVER",
        max_consecutive_auto_reply=3,
        is_termination_msg=lambda x: "terminate" in x.get("content", "").lower(),
    )
    assistant = AssistantAgent(
        name="billing_assistant",
        system_message="Execute the payment charge using payment_gateway_charge. Repeat with the same idempotency key if a timeout occurs.",
    )

    # Register bridged tools for execution and LLM invocation
    for tool_fn in tools:
        user_proxy.register_for_execution(name=tool_fn.__name__)(tool_fn)
        assistant.register_for_llm(name=tool_fn.__name__, description=tool_fn.__doc__)(tool_fn)

    return user_proxy, assistant

# Create RecoveryBench Adapter
adapter = AutoGenAdapter(build_autogen_agents)

# Test under deterministic timeout fault
result = rb.test_agent("payment_timeout_after_commit", adapter)
result.assert_passed()
```

---

## 5. Result Extraction & Status Normalization

The adapter analyzes the resulting `ChatResult`:
- Captures conversation history and cost metrics into `AgentResult.data`.
- Inspects termination messages (`"TERMINATE"`, `"confirmed"`, `"success"`) to determine `CompletionStatus.SUCCEEDED`.
- Detects error keywords or unhandled exceptions and flags `CompletionStatus.FAILED`.
- Tool calls triggered during conversation are recorded in the causal ledger with accurate attempt and effect identities.
