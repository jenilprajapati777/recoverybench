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
schema_version: 1
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
    agent: AgentAdapter | AgentSpec | Callable[..., Any],
    *,
    run_id: str | None = None,
    seed: int | None = None,
    max_attempts: int | None = None,
    ledger_type: str = "memory",  # 'memory' (default) or 'sqlite'
    db_path: str | Path | None = None,
    custom_tools: ToolRegistry | None = None,
    backend: ExecutionBackend | None = None,
    isolation: str = "trusted",  # 'trusted' (default, in-process) or 'isolated' (Docker container)
) -> RunResult:
```

#### Execution Mode & Backend Precedence:
- **Default Execution Mode:** `test_agent()` defaults to `isolation="trusted"`. In trusted mode, the agent runs in-process on the host without network or filesystem isolation, emitting a security advisory. This allows fast, zero-dependency pytest execution.
- **Isolated Execution:** Passing `isolation="isolated"` (or aliases `"docker"`, `"sandbox"`) runs the agent inside a hardened Docker container with `--network=none`. If Docker is not running or the runtime image is unavailable, execution fails closed with `InfrastructureError` or `ConfigurationError`.
- **Precedence Rule:** If an explicit `backend: ExecutionBackend` instance is supplied, it takes precedence and is used as the active backend. If `isolation` is also specified, it is validated against supported modes, but `backend` determines execution.
- **Strict Isolation Validation:** Supported values are normalized for case and whitespace. Any unsupported isolation string immediately raises `ConfigurationError` — RecoveryBench **never** silently falls back from an invalid isolation mode to trusted execution.
- **Ledger Type Validation:** `ledger_type` must be `"memory"` (ephemeral in-memory ledger) or `"sqlite"` (persistent ledger). Any other value raises `ConfigurationError`.

### 4.2 `RunResult` Properties

- `result.passed`: `True` if all deterministic recovery assertions passed.
- `result.verdict`: `"PASS"`, `"GENUINE_FAILURE"`, `"INFRASTRUCTURE_ERROR"`, or `"INSUFFICIENT_EVIDENCE"`.
- `result.evaluation.actual_side_effects`: Actual commits persisted in the Effect Ledger.
- `result.evaluation.duplicate_effect_count`: Count of duplicate commits matching identical business parameters.
- `result.events`: List of immutable `LedgerEvent` objects recorded during the run.
- `result.assert_passed()`: Helper method that raises `AssertionError` with detailed failure diagnostics if not passed.

### 4.3 Using `RecoveryBenchTester` in Pytest

`RecoveryBenchTester` provides a reusable testing harness and context manager for evaluating multiple scenarios within a shared or isolated environment.

#### Class Signature:
```python
class RecoveryBenchTester:
    def __init__(
        self,
        *,
        isolation: str = "trusted",
        backend: ExecutionBackend | None = None,
        ledger_type: str = "memory",
        db_path: str | Path | None = None,
        custom_tools: ToolRegistry | None = None,
        default_seed: int = 42,
    ) -> None: ...

    def test(
        self,
        scenario: str | Path | ScenarioModel,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        *,
        run_id: str | None = None,
        seed: int | None = None,
    ) -> RunResult: ...

    def get_ledger_events(self, run_id: str) -> list[LedgerEvent]: ...
    def reset(self) -> None: ...
```

#### Pytest Fixture Example:

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

Both `test_agent()` and `RecoveryBenchTester()` default to `isolation="trusted"` for rapid, dependency-free local testing without requiring Docker Engine.

To enforce containerized network isolation (`--network=none`), explicitly specify `isolation="isolated"` (or aliases `"docker"`, `"sandbox"`):

```python
from recoverybench.sdk import test_agent, RecoveryBenchTester
from recoverybench.sandbox import AgentSpec

spec = AgentSpec.from_file("src/agents/payment.py", entrypoint="process_payment")

# 1. One-liner test_agent execution with real container network isolation
result = test_agent("payment_timeout_after_commit", spec, isolation="isolated")
result.assert_passed()

# 2. Using RecoveryBenchTester in pytest with container isolation
def test_isolated_payment_recovery():
    with RecoveryBenchTester(isolation="isolated") as tester:
        res = tester.test("payment_timeout_after_commit", spec)
        res.assert_passed()
```

If Docker is not running or the required immutable runtime image is missing, isolated execution **fails closed** immediately by raising `InfrastructureError` or `ConfigurationError`.

### 6.3 Local Development with `isolation="trusted"` (Default)

In trusted mode (`isolation="trusted"`, the default SDK setting), the agent runs directly in the host Python process. This is ideal for rapid development and debugging when Docker is not installed (e.g. Windows without Docker Desktop):

```python
# Both invocations run in trusted (in-process) mode:
result = test_agent("payment_timeout_after_commit", agent)
result_explicit = test_agent("payment_timeout_after_commit", agent, isolation="trusted")

# Similarly with RecoveryBenchTester:
with RecoveryBenchTester() as tester:
    res = tester.test("payment_timeout_after_commit", agent)
    res.assert_passed()
```

> [!WARNING]
> Running in trusted mode emits an explicit `[SECURITY ADVISORY]` banner to stderr, reminding developers that external network traffic is unconstrained. For security-critical evaluations or testing third-party agents, always run with container isolation (`isolation="isolated"`).

### 6.4 Isolation Modes, Aliases, and Precedence Contract

RecoveryBench adheres to a strict, security-sensitive validation contract for isolation modes:

| Category | Accepted Aliases (Case-Insensitive) | Backend Instantiated | Security Boundary |
| :--- | :--- | :--- | :--- |
| **Isolated** | `"isolated"`, `"docker"`, `"sandbox"` | `DockerExecutionBackend` | Operating-system container (`--network=none`, `--read-only`, `nobody:nogroup`, resource capped). |
| **Trusted** | `"trusted"`, `"in-process"`, `"inprocess"`, `"local"` | `InProcessExecutionBackend` | Host Python process without container isolation. |

- **Strict Validation:** Leading/trailing whitespace and case are normalized (`isolation.strip().lower()`). Any unrecognized value (e.g. `"container"`, `"remote"`, `"unknown"`) immediately raises a `ConfigurationError`. RecoveryBench **never** silently falls back to trusted execution on an invalid isolation string.
- **Backend Precedence Rule:** When an explicit `backend: ExecutionBackend` instance is passed (e.g., `backend=InProcessExecutionBackend()` or a custom test double), it determines the active execution engine backend. If `isolation` is also specified, it is still validated against recognized aliases to catch configuration typos, but `backend` takes precedence.
- **AgentSpec Support:** `RecoveryBenchTester.test()` fully supports `AgentSpec` alongside `AgentAdapter` and generic Python callables, passing the spec directly to the active backend without wrapping.

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

---

## 8. Scenario Schema Versioning & Specification Contract

Starting in Phase 3B.2, RecoveryBench enforces explicit, deterministic, and independently versioned scenario schemas. Every scenario definition—whether loaded from YAML, loaded from JSON, or constructed directly as a `ScenarioModel` in Python—must declare an explicit `schema_version`.

### 8.1 Schema Version vs. Package Version

The **scenario schema version** is decoupled from the **RecoveryBench package version**:
- **RecoveryBench Package Version** (e.g. `0.1.0`): Governs the Python package release, SDK APIs, CLI commands, and runtime dependencies.
- **Scenario Schema Version** (e.g. `1`): Governs the declarative schema format for fault definitions, task parameters, and evaluator assertion rules.

A scenario authored with `schema_version: 1` is supported by all RecoveryBench releases that explicitly maintain support for schema version 1. RecoveryBench does not promise unconditional forward compatibility across arbitrary future major schema revisions; when a future schema version (e.g. version 2) is introduced, the platform's version policy will define the explicit set of supported versions.

### 8.2 Currently Supported Schema Versions

- Current Version: `1` (integer).
- Supported Versions: `[1]` (`frozenset({1})`).
- Constants exported by `recoverybench.scenarios`:
  - `CURRENT_SCENARIO_SCHEMA_VERSION = 1`
  - `SUPPORTED_SCENARIO_SCHEMA_VERSIONS = frozenset({1})`
  - `validate_scenario_schema_version(version: Any) -> int`

### 8.3 Supported File Formats & `load_scenario` Entry Point

Scenario files can be authored in either YAML or JSON:
- **YAML**: `.yaml`, `.yml` (parsed with PyYAML `safe_load`).
- **JSON**: `.json` (parsed with standard Python `json`).

Both formats share the identical schema definition and semantic validation pipeline. Equivalent YAML and JSON scenario files produce identical `ScenarioModel` instances.

The standard Python programmatic entry point, `load_scenario(path: str | Path)`, remains fully available and actively maintained:
```python
from recoverybench.scenarios import load_scenario

# Loads and validates YAML (.yaml, .yml)
scenario_yaml = load_scenario("scenarios/payment_timeout_after_commit.yaml")

# Loads and validates JSON (.json)
scenario_json = load_scenario("custom_scenarios/order_flow.json")
```

`load_scenario` dynamically dispatches by file extension, validating the schema version and all invariants regardless of whether YAML or JSON is used.

### 8.4 Migration Instructions for Existing Scenarios (Breaking Change for Unversioned Files)

> [!IMPORTANT]
> **Breaking Change for Unversioned Scenarios**: Existing unversioned scenario files are **not** backward-compatible with Phase 3B.2. Any scenario file that lacks `schema_version: 1` will fail validation with `ScenarioValidationError`.

RecoveryBench **does not silently auto-migrate** unversioned scenarios. Custom or legacy scenarios authored without a schema version must be updated by explicitly adding `schema_version: 1` as a top-level field.

#### Example: Migrating an Unversioned Scenario

**Before (Unversioned - Fails Validation):**
```yaml
scenario_id: custom_order_cancellation
name: "Order Cancellation Timeout"
description: "Tests recovery when cancel request times out"
target_tool: order_service
target_action: cancel
task:
  order_id: "ord_123"
assertions:
  expected_side_effects: 1
```

**After (Versioned - Passes Validation):**
```yaml
schema_version: 1
scenario_id: custom_order_cancellation
name: "Order Cancellation Timeout"
description: "Tests recovery when cancel request times out"
target_tool: order_service
target_action: cancel
task:
  order_id: "ord_123"
assertions:
  expected_side_effects: 1
```

### 8.5 Example: Versioned YAML and JSON Scenarios

#### Versioned YAML Scenario (`custom_scenario.yaml`):
```yaml
schema_version: 1
scenario_id: payment_timeout_after_commit
name: "Payment Timeout After Commit (Two Generals' Hazard)"
description: "Simulates server committing a charge, but client network connection dropping before receiving 200 OK."
target_tool: payment_gateway
target_action: charge

faults:
  - type: timeout_after_commit
    target_tool: payment_gateway
    target_action: charge
    failure_count: 1
    error_message: "Network dropped connection after server committed charge"

task:
  amount: 5000
  currency: USD
  customer_id: "cust_demo_alice_99"

assertions:
  expected_side_effects: 1
  max_duplicate_effects: 0
  require_stable_idempotency_key: true
  max_allowed_retries: 3
  expect_agent_success: true
```

#### Equivalent Versioned JSON Scenario (`custom_scenario.json`):
```json
{
  "schema_version": 1,
  "scenario_id": "payment_timeout_after_commit",
  "name": "Payment Timeout After Commit (Two Generals' Hazard)",
  "description": "Simulates server committing a charge, but client network connection dropping before receiving 200 OK.",
  "target_tool": "payment_gateway",
  "target_action": "charge",
  "faults": [
    {
      "type": "timeout_after_commit",
      "target_tool": "payment_gateway",
      "target_action": "charge",
      "failure_count": 1,
      "error_message": "Network dropped connection after server committed charge"
    }
  ],
  "task": {
    "amount": 5000,
    "currency": "USD",
    "customer_id": "cust_demo_alice_99"
  },
  "assertions": {
    "expected_side_effects": 1,
    "max_duplicate_effects": 0,
    "require_stable_idempotency_key": true,
    "max_allowed_retries": 3,
    "expect_agent_success": true
  }
}
```

### 8.6 Strict Failure Behavior for Unsupported Versions

RecoveryBench fails closed when encountering invalid versions:

| Input Condition | Validator Behavior | Error Message Sample |
| :--- | :--- | :--- |
| **Missing `schema_version`** | Fails with `ScenarioValidationError` | `[schema_version]: Field required. Missing required field 'schema_version'. Supported versions: [1].` |
| **Unsupported version (`2`, `0`, `-1`)** | Fails with `ScenarioValidationError` | `[schema_version]: Value error, Unsupported scenario schema_version: 2. Supported versions: [1].` |
| **Non-integer types (`"1"`, `True`, `1.0`)** | Fails with `ScenarioValidationError` | `[schema_version]: Value error, Invalid schema_version type 'str'. schema_version must be an integer, supported versions: [1].` |
| **Malformed YAML or JSON** | Fails with `ScenarioValidationError` | `Failed to parse YAML / Failed to parse JSON...` |
| **Non-object root (list, scalar)** | Fails with `ScenarioValidationError` | `expected dictionary/object, got list` |
| **Unsupported extension (`.toml`)** | Fails with `ScenarioValidationError` | `Unsupported scenario file format '.toml'. Supported extensions: .json, .yaml, .yml` |
| **Missing file** | Fails with `ConfigurationError` | `Scenario file not found: ...` |

---

## 9. Stable JSON Report Contract (Schema v1)

Starting in Phase 3B.3, RecoveryBench defines a predictable, validated, machine-readable JSON contract for execution reports. These reports serve as the authoritative record of execution evidence for local dashboards, CI pipelines, and external automated reliability analysis tools.

### 9.1 Schema Versioning & Compatibility Policy

The **report schema version** is tracked independently of both the scenario schema version and package release version:
- **`report_schema_version` (Integer, currently `1`)**: Governs the schema and structure of the serialized report.
- **`recoverybench_version` (String, e.g. `"0.1.0"`)**: Indicates the RecoveryBench package version that generated the report.
- **`version` (String, e.g. `"0.1.0"`)**: Retained for backward compatibility with existing report parsers.

#### Strict Version Validation
- The `report_schema_version` must be an integer (e.g. `1`).
- Floats (`1.0`), strings (`"1"`), booleans (`True`/`False`), and missing versions fail closed with `ReportValidationError`.
- Unsupported schema versions (e.g. `0`, `2`, `99`) fail validation citing supported versions: `Supported versions: [1]`.

#### Unknown Fields Policy
Schema Version 1 enforces strict validation across all schema models (`extra="forbid"`). Any unexpected or undeclared fields in a report JSON document fail closed with `ReportValidationError` to prevent silent contract drift.

### 9.2 Explicit Outcome Dimensions & Status Semantics

RecoveryBench strictly avoids collapsing test evaluation into a single ambiguous status flag. Every report's `outcome` object clearly distinguishes distinct dimensions:

```json
"outcome": {
  "verdict": "PASS",
  "execution_success": true,
  "recovery_success": true,
  "side_effect_safety": "VERIFIED_SAFE",
  "side_effect_safe": true,
  "validation_failure": false,
  "runtime_failure": false
}
```

| Field | Type | Semantic Definition |
| :--- | :--- | :--- |
| `verdict` | `str` | High-level classification: `"PASS"`, `"GENUINE_FAILURE"`, `"INFRASTRUCTURE_ERROR"`, or `"INSUFFICIENT_EVIDENCE"`. |
| `execution_success` | `bool` | `true` if the agent ran to completion and the harness evaluated the run without an unhandled infrastructure/runtime crash. |
| `recovery_success` | `bool` | `true` if and only if all deterministic recovery assertions passed (`evaluation.passed == true` and `verdict == "PASS"`). |
| `side_effect_safety` | `str` | Explicit three-state safety classification: `"VERIFIED_SAFE"`, `"UNSAFE"`, or `"UNVERIFIED"`. |
| `side_effect_safe` | `bool` | `true` strictly when `side_effect_safety == "VERIFIED_SAFE"`. Never `true` for unverified runs. |
| `validation_failure` | `bool` | `true` if execution halted due to scenario schema, parameter, or configuration validation errors (`configuration_integrity` failure). |
| `runtime_failure` | `bool` | `true` if an unexpected system or infrastructure fault occurred (e.g. missing Docker daemon, host I/O failure). |

#### Three-State Side-Effect Safety Classification
Zero duplicate side effects alone does not prove safety if the expected side effect count is missing or if the run aborted before effects were recorded. RecoveryBench classifies side effects into three deterministic states:
- **`VERIFIED_SAFE`**: Concrete evidence confirms actual committed side effects equal the scenario's authoritative expected side effect count, with zero duplicate side effects.
- **`UNSAFE`**: Concrete evidence confirms duplicate side effects (`duplicate_count > 0`) or an unexpected number of side effects occurred.
- **`UNVERIFIED`**: Insufficient evidence exists to establish safety (e.g. expected side effect count was not defined, or execution crashed before recording events).

### 9.3 Detailed Report Sections

Every report contains the following core sections:

1. **`scenario`**: Metadata and identity of the evaluated scenario:
   - `id`, `name`, `description`, `schema_version` (scenario schema version), `target_tool`, `target_action`, `is_single_operation`.
2. **`outcome`**: Categorical status breakdown (see Section 9.2).
3. **`side_effects`**: Concrete mutation audit from the ledger:
   - `expected_count`, `actual_count`, `duplicate_count`, `idempotency_key_stable`, `safety_status`, `safe`, `committed_effect_ids`.
4. **`fault_injection`**: Detailed fault injection audit:
   - `configured_faults`: List of fault configs declared in the scenario.
   - `injected_fault_count`: Total faults actively injected during execution.
   - `injected_faults`: List of events where `execution_status == "FAULT_INJECTED"`, recording attempt number, tool, action, error class, and timestamp.
5. **`evaluation`**: Comprehensive assertion outcomes with ledger evidence:
   - `verdict`, `passed`, `isolation_mode`, `duration_ms`, `failure_reasons`, `evidence_summary`, `assertions`.
6. **`events`**: Full append-only sequence of immutable ledger events, with credentials securely redacted across all fields.
7. **`errors`**: Unified list of failure reasons, agent errors, and runtime exceptions.
8. **`metadata`**: Wall-clock execution duration, timestamps, PRNG seed, and total event count.

### 9.4 Python SDK Usage

Access and serialize reports directly from `RunResult`:

```python
import recoverybench as rb
from recoverybench.reporting import ReportModel, ReportValidator, JSONReporter

# 1. Execute recovery test
result = rb.test_agent("payment_timeout_after_commit", my_agent)

# 2. Convert to validated ReportModel instance
report: ReportModel = result.to_report()
assert report.report_schema_version == 1
assert report.outcome.recovery_success is True
assert report.outcome.side_effect_safe is True

# 3. Serialize to deterministic JSON string
json_str: str = result.to_json(indent=2)

# 4. Convert to serializable dictionary
report_dict: dict = result.to_report_dict()

# 5. Validate existing report JSON file
validated_report = ReportValidator.validate_file("reports/run_123_report.json")
assert validated_report.outcome.verdict == "PASS"
```

### 9.5 CLI Usage

```bash
# 1. Run scenario and emit 100% pure JSON report to stdout
recoverybench run payment_timeout_after_commit --agent idempotent --isolation trusted --json

# 2. Run scenario and save report artifacts to directory
recoverybench run payment_timeout_after_commit --agent idempotent --output-dir reports/

# 3. Stream historical run report directly to stdout as JSON (touches no disk files when --output-dir is omitted)
recoverybench report demo_safe_agent_run --json

# 4. Stream JSON to stdout and simultaneously save files to directory
recoverybench report demo_safe_agent_run --json --output-dir reports/

# 5. Validate an existing execution report against the schema contract
recoverybench validate-report reports/payment_timeout_after_commit_report.json
```

### 9.6 Example Serialized JSON Report (Schema v1)

```json
{
  "agent_id": "idempotent_payment_agent",
  "agent_result": {
    "attempts": 2,
    "data": {
      "amount": 5000,
      "charge_id": "ch_sim_0001",
      "currency": "USD",
      "idempotent_replay": true,
      "status": "succeeded"
    },
    "error": null,
    "status": "SUCCEEDED",
    "status_message": "Payment recovered and confirmed on attempt 2",
    "success": true
  },
  "errors": [],
  "evaluation": {
    "actual_side_effects": 1,
    "assertions": [
      {
        "actual": 1,
        "event_ids": [1],
        "evidence": [
          "Operation 'op_payment_timeout_after_commit': Exactly 1 unique effect 'eff_ch_sim_0001' committed across 1 event(s) (Attempts: [1], Events: [1]). Diagnostic FP: ['ad5a2be1c14d0a7c']"
        ],
        "expected": 1,
        "message": "Exactly 1 side-effect(s) committed as expected (0 duplicates).",
        "name": "side_effect_count",
        "passed": true
      },
      {
        "actual": "stable",
        "event_ids": [1, 2],
        "evidence": [
          "Operation 'op_payment_timeout_after_commit' Attempt 1 (Event #1): idempotency_hash='2500877b2bb62932'",
          "Operation 'op_payment_timeout_after_commit' Attempt 2 (Event #2): idempotency_hash='2500877b2bb62932'"
        ],
        "expected": "identical_key_hash_on_retry",
        "message": "Idempotency key remained stable across all retries.",
        "name": "idempotency_key_stability",
        "passed": true
      },
      {
        "actual": 1,
        "event_ids": [1, 2],
        "evidence": [
          "Observed 2 total invocation attempts across 1 operation(s) (Events: [1, 2])",
          "Computed retries: 1 across operations (Max retries for single operation: 1, Budget limit: 3)"
        ],
        "expected": "<=3",
        "message": "Observed 1 retries (2 attempts across 1 operation(s)), within allowed limit of 3.",
        "name": "retry_budget",
        "passed": true
      },
      {
        "actual": true,
        "event_ids": [1],
        "evidence": [
          "Agent completion status: SUCCEEDED (success=True, attempts=2)",
          "Ground-truth ledger commits verified: 1",
          "Agent status message: 'Payment recovered and confirmed on attempt 2'"
        ],
        "expected": true,
        "message": "Agent reported success and 1 verified side-effect(s) committed.",
        "name": "agent_completion_truth",
        "passed": true
      },
      {
        "actual": "safely_reconciled",
        "event_ids": [1, 2],
        "evidence": [
          "Ambiguous fault observed at Event #1: Tool committed state, but response was lost (ToolTimeoutError).",
          "Follow-up Event #2 (Attempt 2): NOT_APPLICABLE key_hash='2500877b2bb62932'",
          "Agent re-presented idempotency key; gateway safely returned cached confirmation."
        ],
        "expected": "safe_idempotent_reconciliation",
        "message": "Safe unknown outcome handling: Agent safely reconciled unknown transaction outcome using idempotency.",
        "name": "unknown_outcome_handling",
        "passed": true
      }
    ],
    "duplicate_effect_count": 0,
    "duration_ms": 0.35,
    "evidence_summary": {
      "agent_completion_truth": [
        "Agent completion status: SUCCEEDED (success=True, attempts=2)",
        "Ground-truth ledger commits verified: 1",
        "Agent status message: 'Payment recovered and confirmed on attempt 2'"
      ]
    },
    "expected_side_effects": 1,
    "failure_reasons": [],
    "idempotency_key_stable": true,
    "isolation_mode": "trusted",
    "max_retries_observed": 1,
    "passed": true,
    "verdict": "PASS"
  },
  "events": [
    {
      "action_type": "charge",
      "attempt_id": "att_run_demo_001_1",
      "attempt_number": 1,
      "commit_status": "COMMITTED",
      "details": {
        "params_keys": ["amount", "currency", "customer_id"]
      },
      "effect_id": "eff_ch_sim_0001",
      "error_type": "ToolTimeoutError",
      "event_id": 1,
      "evidence_reference": "ch_sim_0001",
      "execution_status": "FAULT_INJECTED",
      "idempotency_key_hash": "2500877b2bb62932",
      "operation_id": "op_payment_timeout_after_commit",
      "request_fingerprint": "ad5a2be1c14d0a7c",
      "simulated_timestamp": "2024-01-01T00:00:00+00:00",
      "tool_name": "payment_gateway"
    },
    {
      "action_type": "charge",
      "attempt_id": "att_run_demo_001_2",
      "attempt_number": 2,
      "commit_status": "NOT_APPLICABLE",
      "details": {
        "params_keys": ["amount", "currency", "customer_id"]
      },
      "effect_id": "eff_ch_sim_0001",
      "error_type": null,
      "event_id": 2,
      "evidence_reference": "ch_sim_0001",
      "execution_status": "SUCCESS",
      "idempotency_key_hash": "2500877b2bb62932",
      "operation_id": "op_payment_timeout_after_commit",
      "request_fingerprint": "ad5a2be1c14d0a7c",
      "simulated_timestamp": "2024-01-01T00:00:00.700000+00:00",
      "tool_name": "payment_gateway"
    }
  ],
  "fault_injection": {
    "configured_faults": [
      {
        "error_message": "Network dropped before acknowledgement returned",
        "failure_count": 1,
        "target_action": "charge",
        "target_tool": "payment_gateway",
        "type": "timeout_after_commit"
      }
    ],
    "injected_fault_count": 1,
    "injected_faults": [
      {
        "action_type": "charge",
        "attempt_number": 1,
        "commit_status": "COMMITTED",
        "error_type": "ToolTimeoutError",
        "event_id": 1,
        "timestamp": "2024-01-01T00:00:00+00:00",
        "tool_name": "payment_gateway"
      }
    ]
  },
  "isolation_mode": "trusted",
  "metadata": {
    "agent_id": "idempotent_payment_agent",
    "created_at": "2024-01-01T00:00:00.800000+00:00",
    "duration_ms": 0.35,
    "isolation_mode": "trusted",
    "max_retries_observed": 1,
    "seed": 42,
    "total_events": 2
  },
  "outcome": {
    "execution_success": true,
    "recovery_success": true,
    "runtime_failure": false,
    "side_effect_safe": true,
    "side_effect_safety": "VERIFIED_SAFE",
    "validation_failure": false,
    "verdict": "PASS"
  },
  "recoverybench_version": "0.1.0",
  "report_schema_version": 1,
  "run_id": "run_demo_001",
  "scenario": {
    "description": "Simulates a payment transaction committing on the server, but the network connection dropping before the client receives confirmation.",
    "id": "payment_timeout_after_commit",
    "is_single_operation": true,
    "name": "Payment Timeout After Commit (Two Generals' Hazard)",
    "schema_version": 1,
    "target_action": "charge",
    "target_tool": "payment_gateway"
  },
  "side_effects": {
    "actual_count": 1,
    "committed_effect_ids": [
      "eff_ch_sim_0001"
    ],
    "duplicate_count": 0,
    "expected_count": 1,
    "idempotency_key_stable": true,
    "safe": true,
    "safety_status": "VERIFIED_SAFE"
  },
  "version": "0.1.0"
}
```
