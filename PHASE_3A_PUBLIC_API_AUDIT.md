# RecoveryBench Phase 3A — Public API & Contract Audit Report

**Repository:** `https://github.com/jenilprajapati777/recoverybench`  
**Working Directory:** `D:\RecoveryBench`  
**Current Branch:** `main`  
**Baseline Git Commit:** `b4cda04a40e13dce0c67448ce0a18405fa75d750`  
**Current Public Release:** `v0.1.0` (published on PyPI)  
**Audit Date:** 2026-11-16  
**Auditor:** Senior Public API & Developer Contract Specialist  

---

## Executive Summary

RecoveryBench is an open-source, local-first AI Agent Recovery Testing platform ("Test the failure before production does"). Following the successful publication of `v0.1.0` to PyPI and the verified green CI matrix across Ubuntu 22.04/24.04 and Python 3.11/3.12/3.13, this Phase 3A audit provides an exhaustive, evidence-based review of all public developer-facing surfaces.

The mission of this audit is to establish a rigorous, frozen, and unambiguous **v1 Developer Contract** before building browser dashboards, adding distributed orchestration, or releasing v1.0.0. 

### Key Findings Summary
1. **Core Runtime & Verification Engines Are Exceptionally Sound:** The underlying append-only Effect Ledger, deterministic fault injector (6 fault modes), virtual clock, and transaction evaluator (with P0-1 identity scopes and P0-2 strict boolean validation) form a robust foundation.
2. **Critical SDK / CLI Default Mismatch Discovered:** The CLI (`recoverybench run` and `suite`) defaults to `isolation="isolated"` (hardened Docker container), whereas the high-level Python SDK (`test_agent()`) defaults to `isolation="trusted"` (in-process).
3. **`RecoveryBenchTester` Missing Isolation Parameter:** The official documentation (`docs/SDK_INTEGRATION_GUIDE.md`) instructs developers to write `with RecoveryBenchTester(isolation="isolated") as tester:`, but the actual `RecoveryBenchTester.__init__` does not accept an `isolation` or `backend` parameter, causing an immediate `TypeError`.
4. **Scenario Schema Versioning Absent:** `ScenarioModel` does not declare a `schema_version` field. User scenario YAML files lack version tracking, creating forward-compatibility risks when scenario features evolve.
5. **JSON Report Version Conflation:** The top-level version in `JSONReporter` is emitted as `"version": "0.1.0"`, hardcoded to the package release version rather than a dedicated reporting schema specification version (`schema_version: "1.0"`).
6. **Tool Bridging Hardcoding in LangGraph:** `LangGraphAdapter.bridge_tools()` is hardcoded to bridge only the payment gateway mock tool, preventing multi-tool LangGraph recovery testing.

---

## 1. Public Python SDK Audit

### 1.1 Package Exports and Namespaces (`src/recoverybench/__init__.py`)

The root `recoverybench` package exports the primary developer API in `src/recoverybench/__init__.py`:

```python
__all__ = [
    "__version__",
    "test_agent",
    "RecoveryBenchTester",
    "agent",
    "wrap_agent",
    "AgentAdapter",
    "AgentResult",
    "CompletionStatus",
    "TaskContext",
    "ToolCaller",
    "RunResult",
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
```

#### Evaluation:
- **Strengths:** A clean, flat root namespace allowing standard imports (`import recoverybench as rb; rb.test_agent(...)`).
- **Gaps:** Developers frequently import from internal submodules (`recoverybench.agent.base`, `recoverybench.sdk.tester`, `recoverybench.sandbox`). Submodules are not guarded by private underscores (`_base.py`, `_tester.py`), leading to ambiguous import paths across code and documentation.

---

### 1.2 Generic Callable Adapter & Decorator (`src/recoverybench/adapters/callable.py`)

#### Signatures:
```python
@overload
def agent(target_callable: F) -> CallableAgentAdapter: ...

@overload
def agent(*, id: str | None = None) -> Callable[[F], CallableAgentAdapter]: ...

def wrap_agent(
    target_callable: Callable[..., Any] | AgentAdapter,
    agent_id: str | None = None,
) -> AgentAdapter: ...
```

#### Heuristic Parameter Inspection:
`CallableAgentAdapter` inspects target functions using `inspect.signature()`:
- **1 argument:** Passed `(tool_call: ToolCaller)`.
- **2 arguments:** Checks whether the first parameter annotation is `dict` or parameter name is in `("params", "task_params", "input_data")`. If matched, passes `(task.task_params, tool_call)`; otherwise passes `(task: TaskContext, tool_call: ToolCaller)`.
- **Fallback / >2 arguments:** Passes `(task, tool_call)`.

#### Contract Risk:
Parameter name sniffing (`"params"`, `"task_params"`, `"input_data"`) is an undocumented heuristic. If an agent function is defined as `def my_agent(inputs, tool_call):`, RecoveryBench treats `inputs` as expecting `TaskContext`, which will fail at runtime if `inputs` accesses dictionary keys (`inputs['customer_id']`).

#### Return Value Normalization Matrix:
Implemented in `CallableAgentAdapter._normalize_result()`:
- `AgentResult`: Preserved as-is.
- `bool True` (`type is bool`): Normalized to `CompletionStatus.SUCCEEDED`, `success=True`.
- `bool False` (`type is bool`): Normalized to `CompletionStatus.FAILED`, `success=False`.
- `None`: Normalized to `CompletionStatus.UNKNOWN`, `success=False`.
- `dict`: Validated via `AgentResult(**data)`; invokes strict boolean checks and conflict detection.
- Scalar/Object (`int`, `str`, `list`): Normalized to `CompletionStatus.UNKNOWN`, `success=False`.

#### Exception Handling Contract:
- **Harness Infrastructure Exceptions:** `InfrastructureError`, `ConfigurationError`, `CircuitBreakerError` are explicitly re-raised without modification.
- **Tool Exceptions:** `ToolError` is intercepted and normalized to `AgentResult(success=False, status=CompletionStatus.FAILED, error="ToolError: ...")` with secrets masked.
- **Application Exceptions:** Generic `Exception` is intercepted and normalized to `AgentResult(success=False, status=CompletionStatus.FAILED, error="Exception: ...")` with secrets masked.

---

### 1.3 `AgentResult` & `CompletionStatus` (`src/recoverybench/agent/base.py`)

#### Schema:
```python
class CompletionStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"

class AgentResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    success: bool = False
    status: CompletionStatus = CompletionStatus.UNKNOWN
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    attempts: int = Field(default=1, ge=1)
    status_message: str = Field(default="")
```

#### Reconciler & Conflict Detection (`_reconcile_status_and_success`):
1. **Strict Type Enforcement:** `success` must strictly have `type(v) is bool`. Strings (`"true"`, `"false"`), integers (`0`, `1`), and `None` are rejected and converted to `UNKNOWN`.
2. **Conflict Resolution:** If `status="SUCCEEDED"` but `success=False`, or `status="FAILED"` but `success=True`, the result is forced to `status=UNKNOWN`, `success=False`, with an explicit diagnostic in `error`.
3. **Pydantic Model Config:** `ConfigDict(extra="allow")` allows extra properties to be attached to `AgentResult`. This creates an accidental API where developers attach arbitrary attributes rather than placing them in the structured `data` dictionary.

---

### 1.4 `TaskContext` & Identity Scopes (`src/recoverybench/agent/base.py`)

```python
class TaskContext(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    scenario_id: str
    task_params: dict[str, Any] = Field(default_factory=dict)
    clock: VirtualClock
    operation_id: str | None = None

    @property
    def is_single_operation(self) -> bool:
        return self.operation_id is not None
```

- **`clock`:** Provides non-blocking virtual time advancement (`task.clock.sleep(seconds)`).
- **`operation_id`:** Logical business identity scoping. In single-operation scenarios, `task.operation_id` is populated automatically. In multi-operation scenarios, it is `None`, requiring agents to establish an explicit scope.

---

### 1.5 `ToolCaller` Protocol & Context Manager (`src/recoverybench/agent/base.py`)

`ToolCaller` is defined as:
```python
ToolCaller = Callable[..., dict[str, Any]] | ToolClient
```

`ToolClient` declares two primary dispatching styles:
1. **Callable Protocol:** `tool_call(tool_name, action, params, idempotency_key=None, operation_id=None)`
2. **Explicit Method:** `tool_client.call(tool_name, action, params, idempotency_key=None, operation_id=None)`
3. **Context Manager:**
   ```python
   with tool.operation("op_batch_order_101"):
       tool("payment_gateway", "charge", {"amount": 2500}, idempotency_key="key_1")
   ```

#### Contract Observation:
In early versions, `tool_call` accepted 4 positional arguments: `(tool_name, action, params, idempotency_key)`. When `operation_id` was added in P0-1, callers using 5 positional arguments clashed if `idempotency_key` was omitted. In v1.0, keyword arguments should be encouraged or mandated for optional identifiers.

---

### 1.6 High-Level Test Runner (`test_agent`) (`src/recoverybench/sdk/tester.py`)

#### Signature:
```python
def test_agent(
    scenario: str | Path | ScenarioModel,
    agent: AgentAdapter | AgentSpec | Callable[..., Any],
    *,
    run_id: str | None = None,
    seed: int | None = None,
    max_attempts: int | None = None,
    ledger_type: str = "memory",
    db_path: str | Path | None = None,
    custom_tools: ToolRegistry | None = None,
    backend: ExecutionBackend | None = None,
    isolation: str = "trusted",
) -> RunResult:
```

#### CRITICAL CONTRADICTION — Isolation Defaults:
- **SDK `test_agent()`:** Defaults to `isolation="trusted"`.
- **CLI `recoverybench run`:** Defaults to `isolation="isolated"`.
- **CLI `recoverybench suite`:** Defaults to `isolation="isolated"`.
- **Documentation Claim (`docs/SDK_INTEGRATION_GUIDE.md` L438):** *"Specify `isolation="isolated"` (the default and recommended mode) in `test_agent` or `RecoveryBenchTester`"*.
- **Impact:** Code and documentation directly contradict each other. A developer running tests via `test_agent()` believes they are running in isolated Docker containers, but is actually running in trusted host processes unless explicitly passing `isolation="isolated"`.

---

### 1.7 `RecoveryBenchTester` Harness (`src/recoverybench/sdk/tester.py`)

#### Signature:
```python
class RecoveryBenchTester:
    def __init__(
        self,
        *,
        ledger_type: str = "memory",
        db_path: str | Path | None = None,
        custom_tools: ToolRegistry | None = None,
        default_seed: int = 42,
    ) -> None: ...

    def test(
        self,
        scenario: str | Path | ScenarioModel,
        agent: AgentAdapter | Callable[..., Any],
        *,
        run_id: str | None = None,
        seed: int | None = None,
    ) -> RunResult: ...

    def get_ledger_events(self, run_id: str) -> list[LedgerEvent]: ...
    def reset(self) -> None: ...
```

#### CRITICAL CODE DEFECT — Missing Parameter:
`docs/SDK_INTEGRATION_GUIDE.md` lines 450-455 states:
```python
# Using RecoveryBenchTester in pytest
def test_isolated_payment_recovery():
    with RecoveryBenchTester(isolation="isolated") as tester:
        res = tester.test("payment_timeout_after_commit", spec)
        res.assert_passed()
```
**Reality:** `RecoveryBenchTester.__init__` has NO `isolation` or `backend` parameter. Executing the documented example raises:
`TypeError: RecoveryBenchTester.__init__() got an unexpected keyword argument 'isolation'`.
Furthermore, `RecoveryBenchTester.test()` only accepts `AgentAdapter | Callable` and does NOT accept `AgentSpec`.

---

### 1.8 LangGraph Integration (`src/recoverybench/adapters/langgraph.py`)

#### Exports:
- `LangGraphAdapter(graph_factory, agent_id="langgraph_agent", input_factory=None)`
- `NaiveLangGraphAgent(agent_id="naive_langgraph_agent", max_retries=3)`
- `IdempotentLangGraphAgent(agent_id="idempotent_langgraph_agent", max_retries=3)`
- `create_langchain_payment_tool(tool_call)`
- `bridge_tools(tool_call, tool_names=None)`

#### Dependency Decoupling:
If `langgraph` or `langchain-core` is missing, `LANGGRAPH_AVAILABLE` is set to `False`. Calling `LangGraphAdapter` or `create_langchain_payment_tool` raises a clear `ConfigurationError`:
`"LangGraph is not installed. Install with 'pip install recoverybench[langgraph]'."`

#### Limitation:
`bridge_tools()` currently ignores `tool_names` and only instantiates `create_langchain_payment_tool`. Custom tools registered by users are not bridged into LangChain tools.

---

### 1.9 `AgentSpec` Contract (`src/recoverybench/sandbox/spec.py`)

Used for isolated container execution (`DockerExecutionBackend`):
```python
class AgentSpec(BaseModel):
    name: str = Field(default="agent")
    module_path: str
    entrypoint: str = Field(default="run_agent")
    workspace_dir: Path = Field(default_factory=lambda: Path("."))
    config: dict[str, Any] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    lockfile: Path | None = None
    runtime_image: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    files: list[str] | None = None
```

- **Security Validators:** Traversal attacks (`..`, `/`, `\`) in `module_path` and `files` are rejected. `entrypoint` must be a valid Python identifier.
- **Factories:** `AgentSpec.from_file(file_path, ...)` and `AgentSpec.from_callable(func, ...)`. If a callable is in-memory (lambda, closure) without an inspectable source file, `from_callable` fails fast with an informative error.

---

### 1.10 Run & Evaluation Models (`src/recoverybench/engine/context.py`, `src/recoverybench/models.py`)

- **`RunResult`**:
  - `run_id: str`
  - `scenario: ScenarioModel`
  - `agent_id: str`
  - `evaluation: EvaluationResult`
  - `events: list[LedgerEvent]`
  - `summary: RunSummary`
  - `agent_raw_result: dict[str, object] | None`
  - Properties: `passed` (bool), `verdict` (str), `isolation_mode` (str).
  - Helper: `assert_passed()`, raising `AssertionError` with detailed failure reasons.
- **`EvaluationResult`**:
  - `verdict`: `EvaluationVerdict` (`PASS`, `GENUINE_FAILURE`, `INFRASTRUCTURE_ERROR`, `INSUFFICIENT_EVIDENCE`).
  - `passed`: `bool`.
  - `assertions`: `list[EvaluationAssertion]`.
  - Metrics: `expected_side_effects`, `actual_side_effects`, `duplicate_effect_count`, `idempotency_key_stable`, `max_retries_observed`, `duration_ms`.
  - Diagnostics: `failure_reasons: list[str]`, `evidence_summary: dict[str, list[str]]`.

---

## 2. CLI Developer Interface Audit (`src/recoverybench/cli.py`)

### 2.1 Subcommands Matrix

| Command | Arguments | Options & Flags | Defaults | Exit Codes | Machine Output |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `doctor` | None | `--require-isolation` | `False` | 0 (OK), 3 (Infra Fail) | Console Table only (no `--json`) |
| `init` | None | `--dir`, `-d` (Path) | `Path(".")` | 0 (Success) | Rich Panel & text |
| `list-scenarios`| None | `--json` (bool) | `False` | 0 (Success) | Table or JSON array |
| `validate` | `scenario_path` (Path) | None | `Path("scenarios")` | 0 (Valid), 2 (Config/Schema Error) | Table or error text (no `--json`) |
| `demo` | None | `--isolation`, `-i` (str) | `"trusted"` | 0 (Complete), 2 (Config), 3 (Infra) | Comparison table + report files |
| `run` | `scenario_id` (str) | `--agent`, `-a`<br>`--isolation`, `-i`<br>`--json`<br>`--html`<br>`--junit`<br>`--output-dir`, `-o` | `agent="idempotent"`<br>`isolation="isolated"`<br>`json=False`<br>`html=None`<br>`junit=None`<br>`output_dir=None` | 0 (Pass)<br>1 (Assertion Fail)<br>2 (Config Error)<br>3 (Infra Error) | Console text or JSON (`--json`) |
| `suite` | `scenarios_dir` (Path) | `--agent`, `-a`<br>`--isolation`, `-i`<br>`--output-dir`, `-o`<br>`--junit / --no-junit`<br>`--json` | `agent="idempotent"`<br>`isolation="isolated"`<br>`output_dir="reports"`<br>`junit=True`<br>`json=False` | 0 (All Pass)<br>1 (Any Fail)<br>2 (Config Error)<br>3 (Infra Error) | Suite table or JSON (`--json`) |
| `replay` | `run_id` (str) | `--json` (bool) | `False` | 0 (Found), 2 (Not Found) | Timeline table or JSON trace |
| `report` | `run_id` (str) | `--format`, `-f`<br>`--output-dir`, `-o` | `format="all"`<br>`output_dir=None` | 0 (Exported), 2 (Not Found) | File export status text |

---

### 2.2 CLI Exit Code Contract

RecoveryBench adheres to a deterministic, semantic exit code contract across all commands:

- **`0` — PASS / SUCCESS:** All scenarios passed assertions, or diagnostic/help command completed successfully.
- **`1` — GENUINE FAILURE:** The agent ran successfully without harness error, but violated one or more recovery assertions (e.g. duplicate side effects committed, idempotency key unstable, retry budget exceeded).
- **`2` — CONFIGURATION ERROR:** User input error, invalid scenario YAML syntax, non-existent scenario ID, unresolvable agent, or invalid argument.
- **`3` — INFRASTRUCTURE ERROR:** The test harness could not execute due to environment or container issues (Docker daemon down, runtime container image missing, permission denied on host storage, Python < 3.11).

#### Exit Code Consistency Evaluation:
The exit code mapping is implemented uniformly in `run`, `suite`, `demo`, `doctor`, and `validate`.

---

### 2.3 CLI Flag & Ergonomic Inconsistencies

1. **`doctor` and `validate` lack `--json`:** Both commands are critical in CI pipelines. When running in automation, CI scripts cannot parse the output of `recoverybench doctor` or `recoverybench validate` without resorting to text scraping.
2. **`suite` lacks `--html / --no-html` flag:** `suite` automatically generates HTML files in `output_dir` for every scenario, but unlike `--junit / --no-junit`, there is no flag to disable HTML generation or specify an aggregated HTML report name.
3. **`demo` defaults to `"trusted"`, but `run` and `suite` default to `"isolated"`:** While sensible for zero-dependency local demos, the help text must clearly explain why `demo` defaults to trusted mode while `run` enforces container isolation.

---

## 3. Scenario Authoring Format & Schemas

### 3.1 Declarative YAML Specification (`src/recoverybench/scenarios/schema.py`)

A scenario is defined in YAML and loaded into `ScenarioModel`:

```yaml
scenario_id: payment_timeout_after_commit
name: "Payment Timeout After Commit"
description: "Simulates dropped packet after server commits charge."
target_tool: payment_gateway
target_action: charge

faults:
  - type: timeout_after_commit
    target_tool: payment_gateway
    target_action: charge
    failure_count: 1
    error_message: "Network connection dropped after commit"

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
  verify_unknown_outcome_handling: false

seed: 42
single_operation: true
```

---

### 3.2 Schema Models & Validation Rules

#### `ScenarioAssertions`:
- `expected_side_effects: int | None = 1` (ge=0; 0 for read-only scenarios, None if unspecified).
- `max_duplicate_effects: int = 0` (ge=0).
- `require_stable_idempotency_key: bool = True`.
- `max_allowed_retries: int = 3` (ge=0).
- `expect_agent_success: bool = True`.
- `verify_unknown_outcome_handling: bool = False`.

#### Semantic Constraints Enforced by `ScenarioValidator`:
1. `scenario_id` must be non-empty and cannot contain spaces.
2. `fault.target_tool` must match `scenario.target_tool`.
3. `single_operation: true` is strictly incompatible with `expected_side_effects > 1`.
4. Directory scanning (`validate_directory`) requires valid `.yaml` or `.yml` files.

---

### 3.3 Supported Fault Types (`FaultType`)

```python
class FaultType(StrEnum):
    TIMEOUT_BEFORE_COMMIT = "timeout_before_commit"
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    HTTP_429_THEN_SUCCESS = "http_429_then_success"
    HTTP_500_TRANSIENT = "http_500_transient"
    MALFORMED_TOOL_RESPONSE = "malformed_tool_response"
```

- `TIMEOUT_BEFORE_COMMIT`: Connection fails before mutating state; safe to retry.
- `TIMEOUT_AFTER_COMMIT`: Two Generals' Hazard. State commits, but network acknowledgment is lost. Agent must retry with identical idempotency key to prevent double mutation.
- `DUPLICATE_DELIVERY`: Network/broker redelivers identical request; idempotency key must deduplicate.
- `HTTP_429_THEN_SUCCESS`: Transient rate limit. Injects `retry_after_seconds` header hint.
- `HTTP_500_TRANSIENT`: Server error without commit; agent must back off and retry.
- `MALFORMED_TOOL_RESPONSE`: Returns corrupted JSON payload or raises malformed format exception.

---

### 3.4 Schema Versioning Gap

**Critical Observation:**  
Currently, `ScenarioModel` contains **no schema version field** (`schema_version: str`). 
When users create scenario YAML files or third-party tooling generates scenarios, there is no declarative marker distinguishing v1.0 syntax from future v2.0 breaking changes. If a user includes `version: "1.0"` in their YAML file, Pydantic's default model configuration ignores it silently.

**Recommendation:**
Add an explicit `schema_version: str = Field(default="1.0", description="Scenario specification format version")` to `ScenarioModel`.

---

## 4. Reporting & Artifact Formats

### 4.1 JSON Report Format (`JSONReporter` in `src/recoverybench/reporting/json_reporter.py`)

Outputs a single deterministic JSON payload:

```json
{
  "version": "0.1.0",
  "run_id": "run_9a8b7c6d5e4f",
  "scenario": {
    "id": "payment_timeout_after_commit",
    "name": "Payment Timeout After Commit",
    "target_tool": "payment_gateway",
    "target_action": "charge"
  },
  "agent_id": "idempotent_payment_agent",
  "isolation_mode": "isolated",
  "evaluation": {
    "verdict": "PASS",
    "passed": true,
    "isolation_mode": "isolated",
    "expected_side_effects": 1,
    "actual_side_effects": 1,
    "duplicate_effect_count": 0,
    "idempotency_key_stable": true,
    "max_retries_observed": 1,
    "duration_ms": 124.5,
    "failure_reasons": [],
    "evidence_summary": { ... },
    "assertions": [
      {
        "name": "side_effect_count",
        "passed": true,
        "expected": 1,
        "actual": 1,
        "message": "Exactly 1 side effect committed as expected.",
        "evidence": ["Effect committed with effect_id 'eff_ch_sim_0001'"],
        "event_ids": [1]
      }
    ]
  },
  "events": [
    {
      "event_id": 1,
      "operation_id": "op_payment_timeout_after_commit",
      "attempt_id": "att_run_1",
      "attempt_number": 1,
      "tool_name": "payment_gateway",
      "action_type": "charge",
      "execution_status": "FAULT_INJECTED",
      "commit_status": "COMMITTED",
      "effect_id": "eff_ch_sim_0001",
      "idempotency_key_hash": "a1b2c3d4e5f6...",
      "request_fingerprint": "f0e1d2c3...",
      "simulated_timestamp": "2026-01-01T00:00:00Z",
      "error_type": "timeout_after_commit",
      "evidence_reference": "tx_sim_1001"
    }
  ],
  "agent_result": { ... }
}
```

#### Evaluation:
- **Redaction:** `redact_sensitive_data()` automatically redacts credentials, raw API keys, card numbers, and auth headers before serialization.
- **Versioning Defect:** The top-level key is `"version": "0.1.0"`. This conflates the software version of RecoveryBench with the schema specification of the report. If a user ingests reports from RecoveryBench `0.2.0`, their parser must not break if the report schema remains version `1.0`.
- **Recommendation:** Provide `"schema_version": "1.0"` alongside `"generator_version": "0.1.0"`.

---

### 4.2 HTML Report Format (`HTMLReporter` in `src/recoverybench/reporting/html_reporter.py`)

- **Design:** Standalone, zero-external-dependency HTML document with embedded dark-theme CSS.
- **Key Sections:**
  1. Header with run ID, timestamp, and scenario badge.
  2. Large verdict status pill (`PASS` green, `GENUINE_FAILURE` red, `INSUFFICIENT_EVIDENCE` yellow).
  3. Metric cards (Duration, Side Effects Expected vs Actual, Duplicate Count, Retries, Idempotency Stability, Isolation Mode).
  4. Assertion Results Table with collapsible evidence drawers.
  5. Immutable Event Ledger Sequence Table (Event #, Attempt, Tool, Action, Exec Status, Commit Status, Idempotency Hash, Error).
- **Security:** Self-contained, no external CDN calls or script tags.

---

### 4.3 JUnit XML Report Format (`JUnitReporter` in `src/recoverybench/reporting/junit_reporter.py`)

- **Root Element:** `<testsuites name="RecoveryBench" tests="N" failures="M" errors="0" time="...">`
- **Child Element:** `<testsuite name="{scenario_id}" tests="N" failures="M" errors="0" time="..." timestamp="...">`
- **Properties Element:** Includes `run_id`, `scenario_id`, `agent_id`, `verdict`, `expected_side_effects`, `actual_side_effects`, `duplicate_effects`, `idempotency_key_stable`, `duration_ms`.
- **Testcase Mapping:** Each evaluation assertion (`side_effect_count`, `idempotency_key_stability`, `retry_count_bounded`, `agent_completion_truth`, `unknown_outcome_handling`) maps to a separate `<testcase>` element.
- **Failures:** Mapped to `<failure message="..." type="AssertionFailure">` with full ledger evidence output.

---

### 4.4 Suite Summary JSON (`recoverybench suite --json`)

Saved as `reports/suite_summary.json`:
```json
{
  "suite_directory": "scenarios",
  "agent": "idempotent",
  "total_scenarios": 2,
  "passed": 2,
  "failed": 0,
  "scenarios": [
    {
      "id": "payment_timeout_after_commit",
      "name": "Payment Timeout After Commit",
      "verdict": "PASS",
      "passed": true,
      "expected_effects": 1,
      "actual_effects": 1,
      "duplicate_effects": 0,
      "duration_ms": 42.1
    }
  ]
}
```

---

## 5. Framework Integrations & Extensibility

### 5.1 Generic Python Callables
- **Status:** **Production Ready (STABLE)**.
- Any function or method can be tested without framework dependencies.
- Handles return types strictly (bool, dict, AgentResult) and catches unhandled exceptions.

### 5.2 LangGraph Framework Adapter
- **Status:** **Production Ready (STABLE for payment scenarios)**.
- First-class support for LangChain `@tool` and LangGraph `StateGraph`.
- Bridges mock tools, supports `ToolNode(tools, handle_tool_errors=True)`.
- Prebuilt reference implementations (`NaiveLangGraphAgent`, `IdempotentLangGraphAgent`).
- **Limitation:** Does not yet bridge arbitrary mock tools; hardcoded to payment gateway.

### 5.3 Emerging Agent Frameworks (CrewAI, AutoGen, MCP)
- **Status:** **PROVISIONAL / ROADMAP**.
- CrewAI agents, Microsoft AutoGen, and Anthropic MCP servers can be tested today using the generic callable adapter (`wrap_agent`), but do not yet have dedicated prebuilt adapter classes.

### 5.4 Mock Tool Extensibility (`ToolService` & `ToolRegistry`)
- Developers can implement custom mock services by subclassing `ToolService`:
  ```python
  class ToolService(ABC):
      name: str
      @abstractmethod
      def execute(self, action: str, params: dict, idempotency_key=None, operation_id=None, attempt_id=None) -> ToolResult: ...
      @abstractmethod
      def reset(self) -> None: ...
  ```
- Registered into `ToolRegistry` and passed to `test_agent(custom_tools=...)`.

### 5.5 Ledger Storage Extensibility (`Ledger`)
- Built-in implementations: `InMemoryLedger` (zero-overhead ephemeral testing) and `SQLiteLedger` (persistent audit trails and replay).
- Subclassing `Ledger` allows teams to connect Postgres or DynamoDB if enterprise archival is required.

---

## 6. Contradictions, Ambiguities & Breaking Risk Analysis

### Inconsistency Table

| ID | Interface / Surface | Documented / Expected Behavior | Actual Code Implementation | Severity |
| :--- | :--- | :--- | :--- | :--- |
| **INC-1** | `test_agent(isolation=...)` | Documented as defaulting to `"isolated"` in SDK Guide L438 | Defaults to `isolation="trusted"` in `src/recoverybench/sdk/tester.py:65` | **HIGH** |
| **INC-2** | `RecoveryBenchTester` | Documented as `RecoveryBenchTester(isolation="isolated")` in SDK Guide L452 | `__init__` does not accept `isolation` or `backend`; crashes with `TypeError` | **HIGH** |
| **INC-3** | `RecoveryBenchTester.test` | Expected to accept `AgentSpec` for container-isolated testing | Type annotation is `AgentAdapter \| Callable`; fails if passed `AgentSpec` | **MEDIUM** |
| **INC-4** | `ScenarioModel` | Expected to declare a schema version for scenario files | No `schema_version` field; ignores version declarations in YAML | **MEDIUM** |
| **INC-5** | `JSONReporter` version | Expected to report a stable report format schema version | Hardcodes `"version": "0.1.0"` (package version), conflating schema with release | **LOW** |
| **INC-6** | `CallableAgentAdapter` | Transparent parameter passing | Sniffs parameter names (`params`, `task_params`, `input_data`); breaks if user names first arg `inputs` | **LOW** |
| **INC-7** | CLI Flag Asymmetry | All commands should support `--json` | `doctor` and `validate` lack `--json` output flags | **LOW** |
| **INC-8** | `suite` HTML flag | `suite` supports `--junit / --no-junit` | `suite` lacks `--html / --no-html` flag to control HTML artifact generation | **LOW** |
| **INC-9** | LangGraph Tool Bridging | Dynamic bridging of registered tools | `bridge_tools` hardcodes payment gateway tool; ignores other registered services | **MEDIUM** |

---

### Accidental vs. Contractual Behaviors

1. **`AgentResult` Arbitrary Attributes:** Because `AgentResult` has `ConfigDict(extra="allow")`, developers can attach arbitrary attributes (`res.my_custom_field = 123`). This is an accidental behavior. In v1.0, developers must store custom metadata in `AgentResult.data`.
2. **`test_agent()` Backend Matching:** Backend selection checks `if clean_iso in ("isolated", "docker", "sandbox")`. String-matching synonyms like `"sandbox"` is accidental; the formal enum is `IsolationMode.ISOLATED` or `IsolationMode.TRUSTED`.
3. **Implicit SQLite File Creation:** Running `recoverybench report` or `replay` without an existing DB creates an empty SQLite database instead of immediately returning exit code 2.

---

### Breaking Change Risks for v1.0.0

1. **Aligning `test_agent` isolation default to `"isolated"`:**
   - If changed to default to `"isolated"`, local tests without Docker installed will immediately fail with `InfrastructureError`.
   - *Recommendation:* Keep `test_agent` default as `isolation="trusted"` for rapid local pytest execution, BUT update documentation to accurately reflect this, and require explicit `isolation="isolated"` when container isolation is desired. Alternatively, add an environment variable `RECOVERYBENCH_DEFAULT_ISOLATION`.
2. **Adding `schema_version` to scenarios:**
   - Fully backward-compatible if given default `schema_version: str = "1.0"`.
3. **Disallowing extra attributes in `AgentResult`:**
   - Setting `extra="forbid"` would break code that passes unexpected dictionary keys into `AgentResult`.
   - *Recommendation:* Keep `extra="allow"` in `AgentResult` for dictionary tolerance, but document that only defined fields are serialized into reports.

---

## 7. Contract Stability Matrix & v1.0 Roadmap

### 7.1 Contract Stability Matrix

| Interface / Component | Category | Current Status | Stability Rating | Breaking Change Risk | Recommended v1 Contract |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `@agent` / `wrap_agent` | Python SDK | Hardened | **STABLE** | Low | Preserve current decorator & wrapper signatures. Formalize parameter inspection rules. |
| `AgentResult` | Python SDK | Hardened | **STABLE** | None | Preserve strict boolean validation, status reconciler, and conflict detection. |
| `CompletionStatus` | Python SDK | Hardened | **STABLE** | None | Freeze enum values: `SUCCEEDED`, `FAILED`, `UNKNOWN`. |
| `TaskContext` | Python SDK | Hardened | **STABLE** | None | Freeze fields: `scenario_id`, `task_params`, `clock`, `operation_id`. |
| `ToolCaller` / `ToolClient`| Python SDK | Hardened | **STABLE** | Low | Preserve callable dispatch and `with tool.operation(id):` context manager. Encourage kwargs. |
| `test_agent()` | Python SDK | Inconsistent default | **EVOLVING** | Medium | Align documentation to state default is `trusted`; support explicit `isolation="isolated"`. |
| `RecoveryBenchTester` | Python SDK | Missing isolation arg | **EVOLVING** | Medium | Add `isolation: str = "trusted"` and `backend: ExecutionBackend \| None = None` to `__init__` and `test()`. |
| `LangGraphAdapter` | Framework | Hardened (Single Tool)| **EVOLVING** | Low | Generalize `bridge_tools` to dynamically bridge all registered mock tools. |
| `AgentSpec` | Sandbox | Hardened | **STABLE** | None | Freeze security validations, path sanitization, and environment allowlists. |
| `ExecutionBackend` | Sandbox | Hardened | **STABLE** | None | Freeze `mode`, `is_isolated`, and `run()` signatures. |
| `ScenarioModel` | Schemas | Missing version | **EVOLVING** | Low | Add `schema_version: str = "1.0"` with default `"1.0"`. |
| `FaultConfig` | Schemas | Hardened | **STABLE** | None | Freeze 6 fault types and parameters. |
| `ScenarioAssertions` | Schemas | Hardened | **STABLE** | None | Freeze assertion rule names and semantics. |
| `JSONReporter` | Reporting | Version conflation | **EVOLVING** | Low | Add `"schema_version": "1.0"` alongside `"generator_version": "0.1.0"`. |
| `HTMLReporter` | Reporting | Hardened | **STABLE** | None | Freeze self-contained dark-theme HTML format. |
| `JUnitReporter` | Reporting | Hardened | **STABLE** | None | Freeze XML hierarchy and `<testcase>` assertion naming. |
| CLI Commands & Codes | CLI | Hardened (0, 1, 2, 3) | **STABLE** | Low | Preserve all subcommands and exit codes. Add `--json` to `doctor` and `validate`. |

**Legend:**
- **STABLE:** Production-hardened, frozen developer contract; guaranteed backward compatibility across v1.x.
- **EVOLVING:** Structurally sound; requires non-breaking parameter additions, documentation alignment, or minor ergonomic fixes.
- **PROVISIONAL:** Experimental or roadmap capability (e.g. multi-tool LangGraph bridging, CrewAI/AutoGen native adapters).
- **DEPRECATED:** Flawed or ambiguous patterns to be phased out with warnings.

---

### 7.2 Recommended Implementation Roadmap for Phase 3B

#### Milestone 3B.1: Contract Harmonization & Critical Bug Fixes (High Priority)
1. **Fix `RecoveryBenchTester` Constructor:**
   - Update `RecoveryBenchTester.__init__` to accept `isolation: str = "trusted"`, `backend: ExecutionBackend | None = None`.
   - Update `RecoveryBenchTester.test()` to accept `AgentSpec` in addition to `AgentAdapter` and `Callable`.
   - Wire the active backend through to `ExecutionEngine`.
2. **Align Documentation on `test_agent` Isolation:**
   - Correct `docs/SDK_INTEGRATION_GUIDE.md` to state that `test_agent` defaults to `isolation="trusted"` for rapid local pytest execution, and that `isolation="isolated"` enables container testing.
3. **Add `schema_version` to `ScenarioModel`:**
   - Add `schema_version: str = Field(default="1.0", description="Scenario specification format version")`.
   - Update `SAMPLE_CONFIG_YAML` and sample scenarios to include `schema_version: "1.0"`.

#### Milestone 3B.2: Reporting & CLI Ergonomics (Medium Priority)
1. **Refine JSON Report Versioning:**
   - Add `"schema_version": "1.0"` to JSON report dictionary in `JSONReporter`.
   - Rename `"version"` to `"generator_version"` while keeping `"version"` as an alias for backward compatibility.
2. **Add `--json` Support to `doctor` and `validate`:**
   - Provide machine-readable JSON output for `recoverybench doctor --json` and `recoverybench validate --json` to enable automated CI gating.
3. **Add `--html / --no-html` Flag to `recoverybench suite`:**
   - Bring flag symmetry with `--junit / --no-junit`.

#### Milestone 3B.3: Extensibility & Dynamic Tool Bridging (Medium Priority)
1. **Dynamic LangChain Tool Bridging:**
   - Update `LangGraphAdapter.bridge_tools()` to iterate over all registered `ToolService` instances in `ToolRegistry` and generate LangChain `@tool` functions dynamically.
2. **Parameter Detection Refinement:**
   - Update `CallableAgentAdapter._detect_param_count()` to support any dictionary parameter name (e.g. `payload`, `inputs`, `data`) when type annotations are present.

#### Milestone 3B.4: Final v1.0 Freeze & Contract Certification (Final Step)
1. Run complete matrix test validation.
2. Publish `v1.0.0-rc1` release candidate.
3. Seal developer contract documentation.

---

## 8. Verification & Audit Sign-Off

- **Audit Execution Method:** Read-only AST, runtime model inspection, and documentation cross-referencing.
- **Application Code Modified:** None (Audit-only constraint strictly enforced).
- **Test Suite Modified:** None.
- **Git Commits / Pushes:** None.
- **Status:** Phase 3A Audit Complete. Deliverable file generated at `PHASE_3A_PUBLIC_API_AUDIT.md`.
