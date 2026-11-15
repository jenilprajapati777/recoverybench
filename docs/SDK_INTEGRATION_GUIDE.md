# RecoveryBench - Agent Integration & SDK Guide

> **"Test the failure before production does."**

This guide covers integrating autonomous AI agents with RecoveryBench using the **Reusable Agent Integration Interface**, **Generic Callable Adapters**, **LangGraph Framework Adapter**, and the high-level **Developer SDK**.

---

## 1. Architecture Overview

RecoveryBench connects the Agent Under Test (AUT) to simulated tool interfaces rather than real-world payment gateways or production infrastructure. It runs the agent inside a controlled virtual-time harness equipped with deterministic fault injection and an append-only Effect Ledger. When running with container isolation (`--isolation isolated`), the agent executes inside a hardened Linux container configured with `--network=none` (Linux containers share the host operating system kernel; see [SECURITY_MODEL.md](SECURITY_MODEL.md)).

```
┌─────────────────────────────────────────────────────────────────┐
│                    Developer Agent Code                         │
│  [Python Function / @agent]   OR   [LangGraph StateGraph Node]  │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                   ┌─────────────▼────────────┐
                   │  Agent Integration Layer │
                   │  (Callable / LangGraph)  │
                   └─────────────┬────────────┘
                                 │ ToolCall Dispatch
┌────────────────────────────────▼────────────────────────────────┐
│                   RecoveryBench Execution Engine                │
│                                                                 │
│   ┌─────────────────────┐            ┌──────────────────────┐   │
│   │ Fault Injector      │            │ Simulated Mock Tools │   │
│   │ (6 Fault Modes)     │            │ (Payment Gateway)    │   │
│   └──────────┬──────────┘            └──────────┬───────────┘   │
│              │                                  │               │
│              └────────────────┬─────────────────┘               │
│                               │ Append Event                    │
│                    ┌──────────▼───────────┐                     │
│                    │  Append-Only Ledger  │                     │
│                    │  (SQLite / Memory)   │                     │
│                    └──────────┬───────────┘                     │
└───────────────────────────────┼─────────────────────────────────┘
                                │ Query Events
                     ┌──────────▼──────────┐
                     │  Recovery Evaluator │
                     │  (Rule Assertions)  │
                     └─────────────────────┘
```

---

## 2. Generic Python Agent Callables

You do not need to subclass framework classes or import heavy libraries to test an agent. RecoveryBench supports arbitrary Python functions and methods using the `@agent` decorator or `wrap_agent()`.

### 2.1 Using the `@agent` Decorator

```python
import recoverybench as rb
from recoverybench.agent.base import AgentResult, TaskContext, ToolCaller


@rb.agent(id="resilient_payment_agent")
def my_payment_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
    # 1. Derive stable idempotency key for this transaction
    stable_key = f"tx_key_{task.task_params.get('customer_id')}"

    # 2. Resilient retry loop
    for attempt in range(1, 4):
        try:
            response = tool_call(
                "payment_gateway",
                "charge",
                task.task_params,
                idempotency_key=stable_key,  # Stable key preserved on retry
            )
            return AgentResult(
                success=True,
                data=response,
                attempts=attempt,
                status_message="Charge confirmed",
            )
        except Exception:
            # Network drop or timeout occurred; safe to retry with identical key
            continue

    return AgentResult(success=False, error="Exhausted 3 retry attempts")
```

### 2.2 Return Value Normalization, Strict Boolean Validation & Explicit Completion States

To eliminate silent failures and false successes (AUDIT-02 and P0-2 Final Hardening), RecoveryBench requires explicit, unambiguous confirmation of task completion. The system defines three semantic completion states (`CompletionStatus`):
- `CompletionStatus.SUCCEEDED`: Agent explicitly confirmed task completion (`success=True`).
- `CompletionStatus.FAILED`: Agent explicitly confirmed task failure or caught a terminal error (`success=False`).
- `CompletionStatus.UNKNOWN`: Ambiguous completion state where the agent exited prematurely, returned `None`, passed conflicting signals, or provided unverified data (`success=False`).

#### Strict Boolean Validation (No Coercion)

RecoveryBench enforces **strict type validation** and never uses Python's `bool(value)` coercion:
- **No String Coercion**: Strings such as `"true"`, `"True"`, `"false"`, `"False"`, `"1"`, or `"0"` are strictly rejected for `success`.
- **No Integer Coercion**: Integers such as `1` or `0` are strictly rejected for `success` (even though Python's `bool` is a subclass of `int`, `type(val) is bool` is required).
- **No Null Coercion**: A `None` value in `success` or an empty return is never coerced to `True`.
- **Conflicting Indicators**: When an agent dictionary contains conflicting signals (e.g., `{"status": "failed", "success": True}` or `{"status": "succeeded", "success": False}`), RecoveryBench refuses to silently prioritize one field over the other. It immediately marks `status=CompletionStatus.UNKNOWN`, `success=False`, and records a descriptive conflict diagnostic.

`CallableAgentAdapter` normalizes return types according to the following strict matrix:

| Function Return Type | Normalized Status | Normalized `success` | Evaluator Interpretation |
| :--- | :--- | :--- | :--- |
| `AgentResult(status=SUCCEEDED, ...)` | `SUCCEEDED` | `True` | Explicit success. Verified against committed effects. |
| `AgentResult(status=FAILED, ...)` | `FAILED` | `False` | Explicit failure. Verified against expected failure invariants. |
| `AgentResult(status=UNKNOWN, ...)` | `UNKNOWN` | `False` | Ambiguous completion. Evaluates to `INSUFFICIENT_EVIDENCE`. |
| `bool True` (`type is bool`) | `SUCCEEDED` | `True` | Explicit success signal. |
| `bool False` (`type is bool`) | `FAILED` | `False` | Explicit failure signal. |
| `dict` with `"status": "succeeded"` and `success is not False` | `SUCCEEDED` | `True` | Explicit success signal. |
| `dict` with `"status": "failed"` and `success is not True` | `FAILED` | `False` | Explicit failure signal. |
| `dict` with conflicting `"status"` and `"success"` | `UNKNOWN` | `False` | Conflicting indicators. Evaluates to `INSUFFICIENT_EVIDENCE`. |
| `dict` with string/int `success` (e.g. `"true"`, `1`) | `UNKNOWN` | `False` | Type validation failure. Evaluates to `INSUFFICIENT_EVIDENCE`. |
| `dict` without `"success"` or `"status"` | `UNKNOWN` | `False` | Ambiguous completion (`INSUFFICIENT_EVIDENCE`). |
| `None` | `UNKNOWN` | `False` | Ambiguous completion (`INSUFFICIENT_EVIDENCE`). Never inferred as success! |
| Unhandled User Exception | `FAILED` | `False` | Agent crash / failure with exception payload. |
| Harness Exception (`InfrastructureError`) | *Propagated* | *Propagated* | Evaluated as `INFRASTRUCTURE_ERROR`, never swallowed. |
| Unsupported type (`int`, `str`, `list`, etc.) | `UNKNOWN` | `False` | Ambiguous completion (`INSUFFICIENT_EVIDENCE`). |

> [!IMPORTANT]
> **Deliberate Breaking Change on Coerced Types**:
> Previous drafts claimed 100% backward compatibility for all loose Python return values. In P0-2 Final Hardening, **behavioral compatibility for ambiguous types (`None`, `"true"`, `"false"`, `1`, `0`, and conflicting dictionaries) was intentionally broken**. These values are strictly rejected to eliminate false-success risks in safety-critical agent evaluations.

#### Read-Only and Zero-Side-Effect Scenarios (`expected_side_effects: 0`)

In addition to mutating workflows, agents often perform read-only queries or validation checks. You can declare zero-side-effect expectations in scenario YAML:

```yaml
scenario_id: balance_inquiry_read_only
description: Agent performs read-only balance inquiry without modifying ledger state
target_tool: payment_gateway
assertions:
  expected_side_effects: 0        # Explicitly expect zero mutations
  expect_agent_success: true       # Agent should succeed
```

Evaluator behavior for side-effect assertions:
1. **Zero Expected (`expected_side_effects: 0`)**: If the agent explicitly succeeds and 0 commits occur, the evaluator awards **`PASS`**. If any tool commits a side-effect, the evaluator flags a prohibited mutation and awards **`GENUINE_FAILURE`**. If the agent returns `UNKNOWN`, it evaluates to **`INSUFFICIENT_EVIDENCE`** (never `PASS`).
2. **Positive Expected (`expected_side_effects: N > 0`)**: If the agent explicitly succeeds but 0 effects commit, the evaluator flags a phantom success and awards **`GENUINE_FAILURE`**.
3. **Unspecified (`expected_side_effects: null`)**: An empty ledger without verifiable contracts yields **`INSUFFICIENT_EVIDENCE`**.

---

### 2.3 Multi-Operation Identity & Explicit Operation Scopes

In real-world applications, agents often perform multiple tool invocations within a single workflow. RecoveryBench uses **decoupled transaction identifiers**:

| Identifier | Level | Purpose |
| :--- | :--- | :--- |
| **`operation_id`** | Business Intent | Identifies one logical business operation. Stable across retries; distinct for separate operations. |
| **`attempt_id`** | Network Transport | Unique ID for each invocation attempt generated by the engine (e.g. `att_run123_1`). |
| **`effect_id`** | Ground-Truth State | Mutation ID committed in the mock service (e.g. `eff_ch_sim_0001`). Reused on idempotent replays. |
| **`request_fingerprint`** | Diagnostic Metadata | Canonical SHA-256 hash of payload arguments. Used for debugging, not duplicate detection. |

#### Scenario Scope Contract: Single-Operation vs. Multi-Operation

1. **Single-Operation Scenarios (`single_operation: true` or default with `expected_side_effects: 1`)**:
   - The engine automatically establishes a scenario-level default `operation_id` (`op_{scenario_id}` or `task["operation_id"]`).
   - All invocations and retries within that scenario share that default identity without requiring explicit developer markup.
2. **Multi-Operation Scenarios (`single_operation: false` or `expected_side_effects > 1`)**:
   - The engine **prohibits** a scenario-level default `operation_id` to prevent independent business actions from accidentally sharing one identity.
   - The agent **must** explicitly establish a distinct `operation_id` for every logical business operation using either:
     - The context manager: `with tool.operation("op_order_101"):`
     - The explicit argument: `tool.call(..., operation_id="op_order_101")`
   - If an agent in a multi-operation scenario omits the `operation_id`, the evaluator refuses to guess from request fingerprints and immediately emits **`INSUFFICIENT_EVIDENCE`**.

#### Comprehensive Developer Examples

##### Example 1: Single-Operation Agent (Default Identity)
```python
import recoverybench as rb
from recoverybench.agent.base import AgentResult, TaskContext, ToolCaller

@rb.agent(id="single_charge_agent")
def single_charge_agent(task: TaskContext, tool: ToolCaller) -> AgentResult:
    # Single-operation scenarios automatically supply task.operation_id
    stable_key = f"key_{task.task_params.get('customer_id')}"
    res = tool("payment_gateway", "charge", task.task_params, idempotency_key=stable_key)
    return AgentResult(success=True, data=res)
```

##### Example 2: Multi-Operation Agent with `tool.operation(...)` Context Scope
```python
@rb.agent(id="batch_checkout_agent")
def batch_checkout_agent(task: TaskContext, tool: ToolCaller) -> AgentResult:
    orders = [{"id": "ord_101", "amount": 2500}, {"id": "ord_102", "amount": 2500}]
    results = []

    for order in orders:
        # Establish distinct operation boundary for each order
        with tool.operation(f"op_{order['id']}"):
            res = tool(
                "payment_gateway", "charge",
                {"amount": order["amount"], "currency": "USD"},
                idempotency_key=f"idem_{order['id']}",
            )
            results.append(res)

    # Identical amounts produce identical request fingerprints, but distinct operation_ids
    # result in: 2 proven effects, 0 duplicates -> PASS!
    return AgentResult(success=True, data={"orders": results})
```

##### Example 3: Safe Retry Behavior (Preserving Operation Scope on Network Failure)
```python
@rb.agent(id="safe_resilient_agent")
def safe_resilient_agent(task: TaskContext, tool: ToolCaller) -> AgentResult:
    with tool.operation("op_invoice_999"):
        stable_key = "key_invoice_999"
        for attempt in range(1, 4):
            try:
                # Retries inside the same operation scope inherit 'op_invoice_999'
                res = tool("payment_gateway", "charge", {"amount": 5000}, idempotency_key=stable_key)
                return AgentResult(success=True, data=res, attempts=attempt)
            except Exception:
                # Network timeout occurred; retry safely with identical idempotency key
                continue
    return AgentResult(success=False, error="Retry limit exhausted")
```

##### Example 4: Missing Identity Behavior (Omitting `operation_id` in Multi-Op Scenario)
```python
@rb.agent(id="careless_multi_agent")
def careless_multi_agent(task: TaskContext, tool: ToolCaller) -> AgentResult:
    # In a multi-operation scenario, omitting operation_id leaves it as None:
    res1 = tool("payment_gateway", "charge", {"amount": 1000}, idempotency_key="k1")
    # Evaluator flags: actual="insufficient_evidence_missing_operation_id"
    # Verdict: INSUFFICIENT_EVIDENCE (Never silently guesses identity from payload!)
    return AgentResult(success=True, data=res1)
```

##### Example 5: Unverified Commits (Committed Events Missing `effect_id`)
If a mock service reports a committed state mutation without an `effect_id` (or in legacy SQLite databases):
- The mutation is **not** counted towards verified `actual_commits`.
- The evaluator classifies the assertion as **`INSUFFICIENT_EVIDENCE`** citing the unverified event IDs.
- However, if independent evidence proves a genuine failure (e.g. retry budget exceeded, unstable idempotency key, or proven duplicate effects), the **`GENUINE_FAILURE`** verdict is strictly preserved.
| Unhandled Exception (`raise TimeoutError`) | `AgentResult(success=False, error="TimeoutError: ...")` |

---

## 3. LangGraph Framework Adapter

[LangGraph](https://github.com/langchain-ai/langgraph) is the industry-standard multi-agent state graph library by LangChain. In LangGraph, agents are modeled as state graphs where LLMs emit `AIMessage(tool_calls=[...])` and the prebuilt `ToolNode` executes them.

RecoveryBench provides **first-class integration with LangGraph**:
- Bridges RecoveryBench mock services into native LangChain `@tool` definitions (`BaseTool`).
- Intercepts tool calls issued by `ToolNode(tools, handle_tool_errors=True)`.
- Evaluates state persistence and idempotency key preservation across retry cycles.
- Runs 100% offline without requiring paid OpenAI or Anthropic API keys.

### 3.1 Prebuilt LangGraph Adapters

RecoveryBench includes two reference LangGraph implementations:

```python
import recoverybench as rb
from recoverybench.adapters.langgraph import (
    IdempotentLangGraphAgent,
    NaiveLangGraphAgent,
)

# 1. Test Resilient LangGraph Agent (Passes)
safe_agent = IdempotentLangGraphAgent()
safe_result = rb.test_agent("payment_timeout_after_commit", safe_agent)
assert safe_result.passed is True
assert safe_result.evaluation.actual_side_effects == 1

# 2. Test Naive LangGraph Agent (Fails with double-charge)
naive_agent = NaiveLangGraphAgent()
naive_result = rb.test_agent("payment_timeout_after_commit", naive_agent)
assert naive_result.passed is False
assert naive_result.evaluation.duplicate_effect_count == 1
```

### 3.2 Custom LangGraph Graph Factory

To test your own custom LangGraph workflow, pass a graph factory to `LangGraphAdapter`:

```python
from typing import Annotated, Any, TypedDict
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
import recoverybench as rb
from recoverybench.adapters.langgraph import LangGraphAdapter


class PaymentState(TypedDict):
    messages: Annotated[list[Any], add_messages]
    idempotency_key: str
    attempts: int
    confirmed: bool


def create_resilient_payment_graph(tools: list[Any]) -> Any:
    def agent_node(state: PaymentState) -> dict[str, Any]:
        messages = state.get("messages", [])
        attempts = state.get("attempts", 0)
        key = state.get("idempotency_key", "tx_stable_key_001")
        last_msg = messages[-1] if messages else None

        # Handle tool errors (retries with identical key)
        if last_msg and isinstance(last_msg, ToolMessage) and getattr(last_msg, "status", "") == "error":
            attempts += 1
            retry_call = {
                "name": "payment_gateway_charge",
                "args": {"amount": 100.0, "currency": "USD", "idempotency_key": key},
                "id": f"retry_{attempts}",
                "type": "tool_call",
            }
            return {"messages": [AIMessage(content="", tool_calls=[retry_call])], "attempts": attempts}

        # Handle tool success
        if last_msg and isinstance(last_msg, ToolMessage) and getattr(last_msg, "status", "") != "error":
            return {"messages": [AIMessage(content="Payment confirmed!")], "confirmed": True}

        # Initial tool call
        init_call = {
            "name": "payment_gateway_charge",
            "args": {"amount": 100.0, "currency": "USD", "idempotency_key": key},
            "id": "init_1",
            "type": "tool_call",
        }
        return {"messages": [AIMessage(content="", tool_calls=[init_call])], "idempotency_key": key, "attempts": 1}

    builder = StateGraph(PaymentState)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", ToolNode(tools, handle_tool_errors=True))
    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", tools_condition, {"tools": "tools", "__end__": END})
    builder.add_edge("tools", "agent")
    return builder.compile()


# Mount into RecoveryBench
adapter = LangGraphAdapter(graph_factory=create_resilient_payment_graph)
result = rb.test_agent("payment_timeout_after_commit", adapter)
assert result.passed
```

---

## 4. Developer SDK Reference

### 4.1 `test_agent()`

```python
def test_agent(
    scenario: str | Path | ScenarioModel,
    agent: AgentAdapter | Callable[..., Any],
    *,
    run_id: str | None = None,
    seed: int | None = None,
    max_attempts: int | None = None,
    ledger_type: str = "memory",  # 'memory' or 'sqlite'
    db_path: str | Path | None = None,
    custom_tools: ToolRegistry | None = None,
) -> RunResult:
```

### 4.2 `RunResult` Properties

- `result.passed`: `True` if all deterministic recovery assertions passed.
- `result.verdict`: `"PASS"`, `"GENUINE_FAILURE"`, `"INFRASTRUCTURE_ERROR"`, or `"INSUFFICIENT_EVIDENCE"`.
- `result.evaluation.actual_side_effects`: Actual commits persisted in the Effect Ledger.
- `result.evaluation.duplicate_effect_count`: Count of duplicate commits matching identical business parameters.
- `result.events`: List of immutable `LedgerEvent` objects recorded during the run.
- `result.assert_passed()`: Helper method that raises `AssertionError` with detailed failure diagnostics if not passed.

### 4.3 Using `RecoveryBenchTester` in Pytest

```python
import pytest
from recoverybench.sdk import RecoveryBenchTester
from my_agents import ProductionBillingAgent


@pytest.fixture
def tester():
    with RecoveryBenchTester() as t:
        yield t


def test_payment_timeout_recovery(tester):
    agent = ProductionBillingAgent()
    result = tester.test("payment_timeout_after_commit", agent)

    # Clean assertion
    result.assert_passed()
    assert result.evaluation.actual_side_effects == 1
    assert result.evaluation.duplicate_effect_count == 0


def test_rate_limit_backoff(tester):
    agent = ProductionBillingAgent()
    result = tester.test("payment_rate_limit_429", agent)
    result.assert_passed()
```

---

## 5. Best Practices for Recovery Resilience in Agents

1. **Establish Idempotency Keys Upfront:**
   Derive keys deterministically from the business transaction parameters (e.g. `hash(user_id + order_id + action)`) rather than generating random UUIDs inside retry blocks.
2. **Preserve Key in State / Memory Checkpoints:**
   In stateful frameworks like LangGraph, ensure the idempotency key is a dedicated key in the graph state dictionary (`AgentState`) so that retry nodes access the original token.
3. **Distinguish Transient Errors from Ambiguous Commits:**
   - On HTTP 429 / 500: State was not committed; standard backoff and retry is safe.
   - On Socket Timeout: Server state may have committed (`timeout_after_commit`). Always re-present the original idempotency key so the server can safely return cached receipts without double execution.

---

## 6. Execution Isolation & Container Sandbox Testing

RecoveryBench supports running agents inside hardened Linux containers with `--network=none` to prevent rogue agents from bypassing mock tools and making direct outbound network connections.

### 6.1 Declarative Agent Entrypoint via `AgentSpec`
Because Python closures and lambdas cannot be safely transferred across process or container boundaries without unsafe `pickle` deserialization, isolated execution uses `AgentSpec`:

```python
from recoverybench.sandbox import AgentSpec

# Declare agent from an on-disk source file
spec = AgentSpec.from_file(
    file_path="src/my_project/billing_agent.py",
    entrypoint="run_agent",
    name="production_billing_agent",
)

# Or infer spec from an inspectable function/class
from my_project.billing_agent import run_agent
spec = AgentSpec.from_callable(run_agent)
```

### 6.2 Running Isolated Tests in Python / Pytest
Specify `isolation="isolated"` (the default and recommended mode) in `test_agent` or `RecoveryBenchTester`:

```python
from recoverybench.sdk import test_agent, RecoveryBenchTester
from recoverybench.sandbox import AgentSpec

spec = AgentSpec.from_file("src/agents/payment.py", entrypoint="process_payment")

# One-liner execution with real container network isolation
result = test_agent("payment_timeout_after_commit", spec, isolation="isolated")
result.assert_passed()

# Using RecoveryBenchTester in pytest
def test_isolated_payment_recovery():
    with RecoveryBenchTester(isolation="isolated") as tester:
        res = tester.test("payment_timeout_after_commit", spec)
        res.assert_passed()
```

### 6.3 Local Development with `isolation="trusted"`
If Docker is not installed on the developer workstation (e.g. Windows without Docker Desktop), explicitly pass `isolation="trusted"` to run in-process:

```python
result = test_agent("payment_timeout_after_commit", agent, isolation="trusted")
```
When running in trusted mode, the harness prints a visible warning banner explaining that network access is unconstrained.

---

## 7. Container-Isolated Agent Execution: SDK Imports and Environment Policies

When agents run inside isolated Docker containers (`--network=none`), they execute in a minimal, read-only runtime environment. Understanding supported SDK imports, third-party dependency pre-installation, and environment policies is critical for smooth agent authoring.

### 7.1 Supported SDK Imports in Isolated Containers
The isolated container worker provides pure standard-library shims for core RecoveryBench agent primitives. Isolated agents can safely import:

```python
# 1. Base Agent Adapters and Return Structures
from recoverybench.agent.base import AgentAdapter, AgentResult, TaskContext, ToolCaller

# 2. Standard Tool Exceptions
from recoverybench.errors import ToolError, RateLimitError, MalformedResponseError
```

#### What `TaskContext` and `ToolClient` Provide Inside Containers
- `task.scenario_id`: Current evaluation scenario name (e.g. `"payment_timeout_after_commit"`).
- `task.task_params` / `task.params`: Input payload dictionary for the task.
- `task.operation_id`: Stable transaction operation identifier (P0-1 transaction boundary).
- `task.clock.sleep(seconds)`: Non-blocking virtual time advancement.
- `tool_call(tool_name, action, params, idempotency_key=..., operation_id=...)`: Line-framed IPC tool dispatch to host mock services.

### 7.2 Third-Party Dependencies Must Be in the Runtime Image
Because the container is launched with strict network isolation (`--network=none`):
- **No runtime package downloads are possible** (`pip install` cannot reach PyPI).
- Any third-party libraries used by your agent (e.g. `langchain`, `langgraph`, `pydantic`, `requests`, `openai`, `numpy`) **must already be pre-installed in the container image**.
- Declare your dependencies in `AgentSpec(dependencies=["langgraph>=0.2.0", "pydantic>=2.0"])`.
- RecoveryBench performs a pre-flight dependency check before invoking your agent. If a required dependency is missing, it fails fast with a clear `MissingDependencyError`.
- For custom agent dependencies, specify a pre-built custom image in `AgentSpec(runtime_image="my-org/agent-runtime@sha256:...")` or `SandboxConfig(runtime_image=...)`.

### 7.3 Configurable Application Variables (`AgentSpec.env`)
Agents may declare non-sensitive configuration parameters via `AgentSpec(env={...})`. Examples of permitted configurable variables:
- `OPENAI_API_BASE` / `MOCK_LLM_URL`: Base URLs for local or mock services.
- `MODEL_NAME`: Name or checkpoint of the target model.
- `AGENT_TIMEOUT_SECONDS`: Inner agent timeouts.
- `RETRY_BUDGET`: Configurable loop iteration caps.
- `LOG_LEVEL` / `APP_ENV`: Application logging and environment flags.

### 7.4 Reserved Variables Agents Cannot Override
To preserve container isolation and prevent shared library or loader hijacking, RecoveryBench enforces a strict **Centralized Environment Security Policy**. Agents **cannot** set:
- **System Path:** `PATH` (the container base image's Linux PATH is strictly preserved; host PATH is never inherited).
- **Dynamic Linker & Loader Hooks:** `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `LD_DEBUG`, `LD_PROFILE`, `LD_ORIGIN_PATH`, `GCONV_PATH`, `GLIBC_TUNABLES`, and all `LD_*` / `DYLD_*` prefixes.
- **Python Runtime Redirection:** `PYTHONHOME`, `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONUSERBASE`, `PYTHONBREAKPOINT`.
- **Shell Hooks:** `BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `SHELLOPTS`, `BASHOPTS`, `SHELL`, `IFS`.
- **Process Identity:** `USER`, `USERNAME`, `LOGNAME`, `HOME` (fixed to `nobody:nogroup`).
- **Internal Harness Indicators:** `RECOVERYBENCH_ISOLATED` (fixed to `1`).

If an `AgentSpec` attempts to configure reserved variables, RecoveryBench filters them with an explicit `[SECURITY ADVISORY]` diagnostic and can be configured to fail closed with `ConfigurationError` when `reject_reserved_env=True` is enabled in `SandboxConfig`.
