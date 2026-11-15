# RecoveryBench - Implementation Plan

**Target Timeline & Phasing Strategy**

---

## 1. Roadmap & Milestones

```mermaid
flowchart LR
    M1[Milestone 1: Core Foundation & Demo] --> M2[Milestone 2: Extended Faults & Evaluator]
    M2 --> M3[Milestone 3: Framework & MCP Adapters]
    M3 --> M4[Milestone 4: Cloud & Production Packaging]
    
    style M1 fill:#22c55e,stroke:#15803d,color:#ffffff
    style M2 fill:#22c55e,stroke:#15803d,color:#ffffff
    style M3 fill:#3b82f6,stroke:#1d4ed8,color:#ffffff
    style M4 fill:#64748b,stroke:#334155,color:#ffffff
```

---

## 2. Milestone 1: Core Foundation & Deterministic Demo (COMPLETED)

### Scope & Deliverables
1. **Repository & Packaging Foundation:**
   - `pyproject.toml` configuration with dependencies: `typer`, `rich`, `pydantic>=2.0`, `pyyaml`, `jinja2`, `pytest`, `ruff`, `mypy`.
   - Source tree structure under `src/recoverybench/`.
   - Virtual environment and toolchain setup.

2. **Scenario Schema & Validator:**
   - Pydantic models for scenario definitions (`ScenarioModel`, `FaultConfig`, `AssertionRules`).
   - YAML loader and schema validator.
   - Built-in scenarios: `payment_timeout_after_commit.yaml`, `payment_timeout_before_commit.yaml`.

3. **Effect Ledger Engine:**
   - In-memory and SQLite-backed append-only event store.
   - Event data models capturing all required ledger attributes.
   - SHA-256 anonymization for idempotency keys and request payloads.

4. **Fault Injection & Mock Payment Service:**
   - `MockPaymentService` simulating bank transaction state, balance updates, and idempotency tracking.
   - Fault injector executing `timeout_after_commit` (committing transaction state before dropping client connection), `timeout_before_commit`, etc.

5. **Agent Adapters & Sample Implementations:**
   - Base `AgentAdapter` interface.
   - `NaivePaymentAgent`: Retries on timeout without reusing an idempotency key, causing duplicate charges.
   - `IdempotentPaymentAgent`: Uses stable client-generated idempotency key across retries, avoiding duplicate charges.

6. **Recovery Evaluator Foundation:**
   - Ledger analyzer asserting expected vs. actual side effects, duplicate detection, and idempotency stability.
   - Classification system: `PASS`, `GENUINE_FAILURE`, `INFRASTRUCTURE_ERROR`.

7. **CLI Suite:**
   - `recoverybench doctor`
   - `recoverybench init`
   - `recoverybench list-scenarios`
   - `recoverybench validate`
   - `recoverybench run`
   - `recoverybench demo`
   - `recoverybench replay`
   - `recoverybench report`
   - Strict exit code mappings (0, 1, 2, 3).

---

## 3. Milestone 2: Extended Faults, Evaluator & Reporting (COMPLETED)

### Scope & Deliverables
1. **Full Fault Suite (All 6 Modes):**
   - `timeout_before_commit`: Connection dropped prior to mutation.
   - `timeout_after_commit`: Two Generals' Hazard (server committed, ACK lost).
   - `duplicate_delivery`: Redundant delivery triggers tested against idempotent state.
   - `http_429_then_success`: Deterministic rate-limiting with `Retry-After` header.
   - `http_500_transient`: Transient server failure recovering on attempt $N$.
   - `malformed_tool_response`: JSON corruption and schema fuzzing modes.
   - Deterministic PRNG seeding via scenario `seed: int = 42`.

2. **Deterministic Recovery Evaluator:**
   - Duplicate side-effect detection (with cited receipt tokens).
   - Missing side-effect detection.
   - Idempotency-key stability verification with attempt hashes.
   - Retry budget enforcement and circuit breaker limits.
   - Unknown-outcome handling assertion for ambiguous commit drops.
   - False-success / phantom-completion detection.
   - Every assertion backed by supporting ledger evidence citations.

3. **Multi-Format Reporting Engine:**
   - Interactive HTML reports with collapsible `<details>` evidence drawers.
   - Machine-readable JSON output with complete assertion metrics and event IDs.
   - CI/CD JUnit XML reporting (`--junit` and `report --format junit/all`).

4. **Comprehensive Test Suite:**
   - 53 passing automated unit and integration tests (91% coverage, < 2.0s runtime).

---

## 4. Milestone 3: Framework & Protocol Adapters (IN PROGRESS)

1. **Generic Callable Adapter & Decorator (COMPLETED):**
   - `@agent` decorator and `wrap_agent` for arbitrary Python functions.
   - Signature inspection and return normalization.
   - High-level `test_agent()` and `RecoveryBenchTester` harness.

2. **LangGraph StateGraph Recovery Adapter (COMPLETED):**
   - Official LangGraph framework adapter (`LangGraphAdapter`).
   - Mock tool bridging into LangChain `@tool` definitions (`BaseTool`).
   - Reference `IdempotentLangGraphAgent` and `NaiveLangGraphAgent`.
   - Comprehensive test suite and runnable working examples.

3. **Model Context Protocol (MCP) Adapter (Upcoming):**
   - MCP client/server adapter for testing MCP tools over stdio/SSE.
4. **CrewAI / AutoGen Test Harnesses (Upcoming).**

---

## 5. Milestone 4: CI/CD & Enterprise Readiness (COMPLETED)

1. **GitHub Actions Integration (COMPLETED):**
   - Turnkey CI workflow (`.github/workflows/recoverybench-ci.yml`) with matrix builds across Python 3.11, 3.12, 3.13.
   - PR Quality Gate (`.github/workflows/recoverybench-pr-gate.yml`) with artifact uploads.
   - Composite Action (`.github/actions/recoverybench-test/action.yml`) for single-step integration in external repositories.
   - `$GITHUB_STEP_SUMMARY` automatic markdown reporting with failure details drawers.
   - Batch scenario validation (`recoverybench validate scenarios`).
   - Batch suite execution (`recoverybench suite scenarios --agent idempotent --output-dir reports --junit`).
   - Strict exit codes: 0 (pass), 1 (recovery invariant failure), 2 (config error), 3 (infra error).
   - Sensitive credential redaction & masking (`security.py`).
   - Complete CI documentation (`docs/GITHUB_ACTIONS_GUIDE.md`).
