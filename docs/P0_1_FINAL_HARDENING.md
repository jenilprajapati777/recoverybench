# RecoveryBench — P0-1 Final Hardening Report

**Date:** November 15, 2026  
**Author:** Principal Python Architect & Distributed Systems Reliability Engineer  
**Milestone:** P0-1 Final Hardening (Transaction Identity Boundary & Unverified Mutation Remediation)  
**Status:** Completed, Fully Verified, All Invariants Enforced  

---

## 1. Executive Summary & Objective

Following the initial P0-1 transaction identity remediation (`AUDIT-01`), two critical identity-boundary limitations were documented in [P0_1_TRANSACTION_IDENTITY_REMEDIATION.md](file:///d:/RecoveryBench/docs/P0_1_TRANSACTION_IDENTITY_REMEDIATION.md):

1. **Unscoped Default Inadvertent Sharing:** Single-operation scenarios defaulted `operation_id` to `op_{scenario_id}`. In multi-operation scenarios where an agent performed several distinct business operations without setting an `operation_id`, all operations silently inherited `op_{scenario_id}`, causing independent transactions to be conflated and evaluated as duplicate mutations under a single operation.
2. **Unverified Commits Lacking `effect_id`:** When ledger events recorded `commit_status == COMMITTED` without an `effect_id` (e.g., from mock errors, legacy SQLite tables, or unverified mutations), the evaluator risked misclassifying unverified state as missing effects (`GENUINE_FAILURE`) rather than returning `INSUFFICIENT_EVIDENCE` with supporting event citations, while failing to preserve genuine failures when independent violations occurred.

This hardening milestone resolves both architectural limitations without redesigning working subsystems.

---

## 2. Key Architectural Decisions & Implementation

### 2.1 Task 1: Safe Operation ID Defaults & Explicit Operation Scopes

#### Declarative Scenario Scopes (`ScenarioModel`)
The scenario schema was extended in [src/recoverybench/scenarios/schema.py](file:///d:/RecoveryBench/src/recoverybench/scenarios/schema.py) with declarative operation scope detection:
```python
class ScenarioModel(BaseModel):
    ...
    single_operation: bool | None = Field(
        default=None,
        description="Whether this scenario models a single logical operation (default: True unless expected_side_effects > 1)",
    )

    @property
    def is_single_operation(self) -> bool:
        """Determine whether the scenario is scoped to a single logical operation."""
        if self.single_operation is not None:
            return self.single_operation
        if self.task.get("single_operation") is False:
            return False
        if self.task.get("multi_operation") is True:
            return False
        if self.task.get("operation_scope") in ("multi", "multiple"):
            return False
        if self.assertions.expected_side_effects > 1:
            return False
        return True
```
A semantic validation check was added to `ScenarioValidator` in [src/recoverybench/scenarios/validator.py](file:///d:/RecoveryBench/src/recoverybench/scenarios/validator.py) to reject contradictory scenarios where `single_operation: true` is configured alongside `expected_side_effects > 1`.

#### Execution Engine Boundary Resolution (`ExecutionEngine.run`)
In [src/recoverybench/engine/runner.py](file:///d:/RecoveryBench/src/recoverybench/engine/runner.py):
1. **Single-Operation Scenarios**: Scenarios with `is_single_operation == True` establish a scenario-level default:
   $$\text{default\_op\_id} = \text{scenario.task.get}("operation\_id", \text{f"op\_\{scenario.scenario\_id\}"})$$
   All tool invocations and retries within that scenario inherit this default if not explicitly overridden.
2. **Multi-Operation Scenarios**: Scenarios with `is_single_operation == False` set:
   $$\text{default\_op\_id} = \text{None}$$
   No scenario-level identity is created. Every logical business operation **must** establish its own distinct `operation_id`.
3. **Fail-Closed on Omission**: If an agent in a multi-operation scenario omits `operation_id`, the ledger records `operation_id = None`. The engine **never silently synthesizes random operation IDs** per tool call (which would destroy retry tracking), and the evaluator refuses to guess from request fingerprints, immediately yielding `INSUFFICIENT_EVIDENCE`.

#### Scoped Operation Context API (`BoundToolClient.operation`)
In [src/recoverybench/agent/base.py](file:///d:/RecoveryBench/src/recoverybench/agent/base.py) and [src/recoverybench/engine/runner.py](file:///d:/RecoveryBench/src/recoverybench/engine/runner.py):
```python
@contextlib.contextmanager
def operation(self, operation_id: str) -> Generator[str, None, None]:
    """Establish an active operation_id context scope for tool calls."""
    if not operation_id or not operation_id.strip():
        raise ValueError("operation_id must be a non-empty string.")
    prev = self._scoped_operation_id
    self._scoped_operation_id = operation_id.strip()
    try:
        yield self._scoped_operation_id
    finally:
        self._scoped_operation_id = prev
```
- **Context-Bound Scope**: Any call to `tool.call(...)` or `tool(...)` inside `with tool.operation("op_1"):` automatically inherits `"op_1"`.
- **Automatic Retry Preservation**: If an agent catches a timeout and retries inside the context block, the retry automatically preserves the exact same `operation_id`.
- **Explicit Override**: Explicitly passing `tool.call(..., operation_id="...")` overrides the active context.

---

### 2.2 Task 2: Committed Effect Without `effect_id`

#### Evaluator Handling of Unverified Commits (`RuleEngine.evaluate_side_effects`)
In [src/recoverybench/evaluator/rules.py](file:///d:/RecoveryBench/src/recoverybench/evaluator/rules.py):
1. Committed ledger events (`commit_status == COMMITTED`) are partitioned into **proven commits** (`effect_id` is present and non-empty) and **unverified commits** (`effect_id` is None or empty).
2. **Exclusion from Proven Commits**: Unverified commits are excluded from `actual_commits`:
   $$\text{actual\_commits} = |\{\text{e.effect\_id} \mid e \in \text{proven\_commits}\}|$$
3. **Insufficient Evidence on Missing Effect Identity**: If unverified commits exist and no duplicate violation was independently proven:
   ```python
   actual = "insufficient_evidence_missing_effect_id"
   passed = False
   event_ids = unverified_ids
   ```
4. **Preservation of Genuine Independent Failures**:
   In [src/recoverybench/evaluator/evaluator.py](file:///d:/RecoveryBench/src/recoverybench/evaluator/evaluator.py):
   ```python
   has_independent_violation = (
       not idempotency_assertion.passed
       or not retry_assertion.passed
       or (not completion_assertion.passed and completion_assertion.actual not in (
           "insufficient_evidence_missing_operation_id", "insufficient_evidence_missing_effect_id"
       ))
       or duplicate_count > assertions.max_duplicate_effects
   )
   if has_independent_violation:
       verdict = EvaluationVerdict.GENUINE_FAILURE
   else:
       verdict = EvaluationVerdict.INSUFFICIENT_EVIDENCE
   ```
   If independent evidence proves a violation (e.g. retry budget exceeded, unstable idempotency key, or agent crash), `GENUINE_FAILURE` is strictly preserved.

#### Retry Budget Overhaul (`RuleEngine.evaluate_retry_bounds`)
Previously, `evaluate_retry_bounds` computed retries as $\text{total\_events} - 1$, assuming all events were retries of one operation. In a multi-operation scenario with 5 operations (1 attempt each), it computed 4 retries and falsely failed!
`evaluate_retry_bounds` was overhauled to group attempts per logical operation:
$$\text{Retries}(op) = \max\left(0, |\text{Attempts}(op)| - 1\right)$$
$$\text{Total Retries} = \sum_{op} \text{Retries}(op)$$
$$\text{Max Op Retries} = \max_{op} \text{Retries}(op)$$
Independent operations no longer increment the retry counter.

---

## 3. Modified Codebase Files

| File | Changes Made |
| :--- | :--- |
| [src/recoverybench/agent/base.py](file:///d:/RecoveryBench/src/recoverybench/agent/base.py) | Added `is_single_operation` property to `TaskContext`. Added `operation(op_id)` context manager and `current_operation_id` property to `ToolClient`. Extended `ToolCaller` type alias. |
| [src/recoverybench/scenarios/schema.py](file:///d:/RecoveryBench/src/recoverybench/scenarios/schema.py) | Added `single_operation: bool \| None` field and `is_single_operation` property to `ScenarioModel`. |
| [src/recoverybench/scenarios/validator.py](file:///d:/RecoveryBench/src/recoverybench/scenarios/validator.py) | Added semantic validation check ensuring `single_operation: true` cannot be combined with `expected_side_effects > 1`. |
| [src/recoverybench/engine/runner.py](file:///d:/RecoveryBench/src/recoverybench/engine/runner.py) | Enhanced `BoundToolClient` with context-scoped `operation(op_id)` stack. Established default `operation_id` only for single-op scenarios; set to `None` for multi-op scenarios. Strips and cleans `operation_id` arguments. |
| [src/recoverybench/evaluator/rules.py](file:///d:/RecoveryBench/src/recoverybench/evaluator/rules.py) | Rewrote `evaluate_side_effects` to handle unverified commits with `insufficient_evidence_missing_effect_id`, citing event IDs. Updated `evaluate_agent_completion` with `has_unverified_commits`. Fixed `evaluate_retry_bounds` to calculate retries per logical operation. |
| [src/recoverybench/evaluator/evaluator.py](file:///d:/RecoveryBench/src/recoverybench/evaluator/evaluator.py) | Enhanced verdict classifier to preserve `GENUINE_FAILURE` when independent violations exist alongside missing operation/effect identity. |
| [tests/test_transaction_identity.py](file:///d:/RecoveryBench/tests/test_transaction_identity.py) | Added and verified all required test cases A through H with assertions on both verdicts and supporting evidence. |
| [docs/ARCHITECTURE.md](file:///d:/RecoveryBench/docs/ARCHITECTURE.md) | Documented operation scope resolution, `BoundToolClient.operation` context API, and unverified mutation handling. |
| [docs/SDK_INTEGRATION_GUIDE.md](file:///d:/RecoveryBench/docs/SDK_INTEGRATION_GUIDE.md) | Documented API contract, single-op default identity, multi-op context scopes, safe retry behavior, and missing identity handling. |
| [docs/TEST_STRATEGY.md](file:///d:/RecoveryBench/docs/TEST_STRATEGY.md) | Documented test matrix Cases A through H. |

---

## 4. Test Verification Matrix (Cases A through H)

All required test cases in [tests/test_transaction_identity.py](file:///d:/RecoveryBench/tests/test_transaction_identity.py) passed cleanly:

| Case | Description | Expected Verdict | Verified Result | Evidence Verified |
| :---: | :--- | :---: | :---: | :--- |
| **A** | Single-operation scenario using declared default identity (`single_operation: true`). | `PASS` | **PASSED** | Captures `op_single_op_demo`, `att_..._1`, `eff_ch_sim_0001`, 0 duplicates. Evidence cites `operation_id` and `effect_id`. |
| **B** | Multi-operation scenario using 5 independent operations with distinct IDs. | `PASS` | **PASSED** | 5 distinct `operation_id`s, 5 unique `effect_id`s, 0 duplicates, retry budget computed per-operation (0 retries). |
| **C** | Multi-operation scenario with identical payloads but distinct operation IDs. | `PASS` | **PASSED** | Identical request fingerprints produce 2 proven effects with 0 duplicates. Evidence proves no false duplicate triggered. |
| **D** | Multi-operation scenario where operation ID is omitted by agent. | `INSUFFICIENT_EVIDENCE` | **PASSED** | `actual="insufficient_evidence_missing_operation_id"`, cites event IDs, prohibits fingerprint guessing. |
| **E** | Retry attempts preserving original `operation_id` (Safe vs Unsafe). | `PASS` (Safe) / `GENUINE_FAILURE` (Unsafe) | **PASSED** | Safe retry shares `op_id` with cached effect. Unsafe retry shares `op_id` with different key -> duplicate detected! |
| **F** | Committed event with no `effect_id` (Missing Effect Identity). | `INSUFFICIENT_EVIDENCE` / `GENUINE_FAILURE` | **PASSED** | Unverified commit yields `INSUFFICIENT_EVIDENCE` and cites event IDs. When retry budget is exceeded, `GENUINE_FAILURE` is preserved! |
| **G** | Historical SQLite event with missing new identifiers. | `INSUFFICIENT_EVIDENCE` | **PASSED** | Legacy database with NULL columns auto-migrates and evaluates to `INSUFFICIENT_EVIDENCE`, never silently awarding PASS. |
| **H** | Existing payment demo and LangGraph integrations. | `PASS` | **PASSED** | Verified single-operation demo scenario and LangGraph tool execution within `tool.operation` scope. |

---

## 5. Verification Commands & Results

### 5.1 Pytest Suite
```bash
.venv\Scripts\python.exe -m pytest -v
============================= 91 passed in 10.47s =============================
```

### 5.2 Test Coverage Report
```bash
.venv\Scripts\python.exe -m pytest --cov=recoverybench --cov-report=term-missing
-----------------------------------------------------------------------------
TOTAL                                            1976    222    89%
============================= 91 passed in 23.15s =============================
```

### 5.3 Static Analysis (Ruff)
```bash
.venv\Scripts\ruff.exe check .
All checks passed!
```

### 5.4 Type Checking (Mypy)
```bash
.venv\Scripts\mypy.exe src tests
Success: no issues found in 63 source files
```

### 5.5 CLI Comparative Demonstration
```bash
.venv\Scripts\python.exe -m recoverybench.cli demo
Exit code: 0
Outcome:
- Naive Payment Agent: GENUINE_FAILURE (1 duplicate effect, DOUBLE CHARGE detected)
- Idempotent Payment Agent: PASS (1 effect, 0 duplicates, safely reconciled)
```

---

## 6. Remaining Limitations & Transition to Milestone P0-2

1. **P0-1 Scope Closed**: The transaction identity architecture, boundary scoping, retry grouping, and unverified mutation handling are fully hardened, tested, and verified.
2. **Milestone P0-2 Readiness**: The codebase is ready to proceed to Milestone P0-2 (`AUDIT-02`: Silent Failure & False Success Remediation in `CallableAgentAdapter.normalize_result` and `evaluate_agent_completion`).
