# RecoveryBench - Product Specification

**Version:** 0.1.0 (MVP)  
**Status:** In Active Development  
**Tagline:** *"Test the failure before production does."*

---

## 1. Executive Summary & Problem Statement

Autonomous AI agents are increasingly entrusted with executing real-world side effects: charging credit cards via payment APIs, booking reservations, updating CRM records, provisioning cloud infrastructure, and dispatching external webhooks.

However, standard distributed systems realities apply:
1. Networks are unreliable (timeouts, dropped packets, connection resets).
2. Third-party APIs experience intermittent delays, 5xx outages, and rate limits (429).
3. The "Two Generals' Problem" guarantees that an agent cannot know whether a request committed on the server if the acknowledgement network response drops after commit (`timeout_after_commit`).

When naive LLM agents face unexpected timeouts or transient tool errors, their default prompt loops retry blindly. Without stable idempotency keys, pre-flight reconciliation, or explicit transaction state handling, these retries trigger catastrophic duplicated side-effects: **double-charging customers, creating duplicate shipments, or triggering redundant workflows.**

Existing LLM tooling focuses almost exclusively on **observability** (traces, token counts, latency) or **benchmark evaluation** (accuracy, MMLU scores, hallucination detection). **None of these evaluate post-fault recovery mechanics against ground-truth side-effects.**

**RecoveryBench** is a local-first, deterministic AI agent recovery testing platform. It injects precision faults into mock tool environments, monitors every side effect in an immutable ledger, and deterministically asserts whether an agent recovered safely without duplicate or orphaned side-effects.

---

## 2. Target Personas & Use Cases

### 2.1 Target Personas
- **AI Agent Engineers & Framework Users:** Building autonomous agents with LangGraph, CrewAI, AutoGen, or custom tool-calling loops.
- **Backend & Platform Engineers:** Responsible for system reliability, API integrations, and preventing financial/data discrepancies caused by AI automation.
- **MCP Tool Developers:** Creating tools conforming to Model Context Protocol (MCP) requiring stress testing under adverse fault conditions.
- **QA & Reliability Engineers:** Setting up automated CI/CD pipelines to certify that agent releases meet side-effect safety invariants.

### 2.2 Primary Use Cases
1. **The Double-Charge Test (`timeout_after_commit`):** Simulate server-side commit followed by client socket timeout. Assert that agent retries use an identical idempotency key or reconcile prior state before attempting another charge.
2. **The Transient 500 & Backoff Test (`http_500_transient`):** Simulate transient backend downtime and assert backoff/retry bounds without exceeding max attempt budgets.
3. **The Duplicate Delivery Test (`duplicate_delivery`):** Simulate webhook or message queue redeliveries to test whether the agent processes payloads idempotently.
4. **The Malformed Response Test (`malformed_tool_response`):** Assert that schema breakage or truncated JSON from tools triggers safe escalation rather than unhandled crashes or hallucinations of phantom success.
5. **Deterministic Replay & CI Regression:** Replay historical runs bit-for-bit using recorded event streams and virtual time to prove regressions are fixed.

---

## 3. Product Principles & Boundaries

1. **Local-First & Zero Cloud Lock-in:** RecoveryBench runs locally offline. No telemetry, no mandatory SaaS accounts, no cloud credits required.
2. **Ground Truth Over LLM-as-a-Judge:** Recovery verification never relies on prompting an LLM to guess if an action succeeded. Evaluation is derived from an append-only SQLite/Memory effect ledger and deterministic assertions.
3. **Virtual Time & Zero Flaky Sleep:** Faults and retry policies are evaluated using deterministic simulated time steps, eliminating brittle `time.sleep()` calls in automated test suites.
4. **Mock Tool Isolation:** Mock services strictly encapsulate state. Agents interact with injected tool interfaces rather than production services. *(Note: OS-level socket interception is scheduled for P0-3).*
5. **Honest Evaluation:** RecoveryBench does not claim universal proof of distributed consensus; it empirically verifies agent behavior against defined fault models.

---

## 4. Feature Requirements (Milestone 1 & MVP Scope)

| Feature ID | Feature Name | Description | Status |
| :--- | :--- | :--- | :--- |
| **REQ-01** | **Scenario Format (YAML)** | Declarative schema for defining tools, fault injection rules, seed, agent target, and deterministic assertions. | Completed |
| **REQ-02** | **Append-Only Effect Ledger** | SQLite/Memory event log tracking every tool request with decoupled transaction identity (`operation_id`, `attempt_id`, `effect_id`, `request_fingerprint`, `idempotency_key_hash`). | Completed |
| **REQ-03** | **Mock Payment Service** | Simulated mock payment provider supporting ledger-backed simulated balance/transaction states and hookable fault points. | Completed |
| **REQ-04** | **Fault Injector** | Precision interceptor supporting all 6 deterministic fault modes (`timeout_before_commit`, `timeout_after_commit`, `duplicate_delivery`, `http_429_then_success`, `http_500_transient`, `malformed_tool_response`). | Completed |
| **REQ-05** | **Agent Adapter Contract** | Clean Python protocol allowing arbitrary agents to invoke tools and handle recovery loops. | Completed |
| **REQ-06** | **Naive vs. Safe Demo** | Deterministic comparison demonstrating an unsafe naive retry (duplicate charge) vs. safe idempotent retry (single charge). | Completed |
| **REQ-07** | **Recovery Evaluator** | Rule-based engine computing duplicate effects, missing effects, idempotency stability, retry bounds, agent truthfulness, and unknown-outcome handling using explicit transaction identity. | Completed |
| **REQ-08** | **CLI Suite** | Commands: `init`, `list-scenarios`, `demo`, `run`, `replay`, `report`, `validate`, `doctor` with strict exit codes (0, 1, 2, 3). | Completed |
| **REQ-09** | **Reporting Engine** | Multi-format reporting: machine-readable JSON, collapsible HTML with ledger evidence, and standard JUnit XML. | Completed |
| **REQ-10** | **Replay Engine** | Deterministic rerun from stored traces to reproduce failures and audit historical runs. | Completed |
| **REQ-11** | **Container Execution Sandbox** | Real OS-level container isolation (`--network=none`), staged workspace credential exclusion, symlink escape detection, resource bounds, and stdio IPC protocol. | Completed |

---

## 4.1 Transaction Identity Model

To prevent false duplicate detection when agents execute multiple independent operations with identical parameters, RecoveryBench decouples identity into three distinct concepts:
- **`operation_id`:** Identifies one logical business operation. Stable across retries of the same operation; distinct for separate operations.
- **`attempt_id`:** Identifies one specific invocation transmission attempt. Generated per call by the execution engine.
- **`effect_id`:** Identifies an actual committed mutation in the simulated tool state. Generated by the mock service on commit; reused on idempotent cache hits.
- **`request_fingerprint`:** Canonical SHA-256 hash of payload arguments, retained purely as diagnostic metadata. Duplicate detection never relies on fingerprint matching alone.

---

## 4.2 Agent Completion States & False-Success Prevention

To prevent silent failures, phantom successes, and ambiguous completion assumptions (AUDIT-02 and P0-2 Final Hardening), RecoveryBench establishes a three-state completion model (`CompletionStatus`):
- **`SUCCEEDED`:** Agent explicitly confirms successful task completion (`success=True`). Evaluated against ground-truth committed side-effects.
- **`FAILED`:** Agent explicitly reports failure or unrecoverable error (`success=False`). Evaluated against expected failure invariants (prevents orphaned commits).
- **`UNKNOWN`:** Agent completed without an explicit, verifiable signal (e.g. returned `None`, empty dict, unhandled scalar, rejected string/int booleans, or conflicting status fields). Evaluates to `INSUFFICIENT_EVIDENCE` unless independent violations exist.

### Strict Boolean Validation & Conflict Handling
- **No Boolean Coercion:** Values for `success` must strictly have `type(val) is bool`. Strings (e.g. `"true"`, `"false"`, `"0"`, `"1"`), integers (`0`, `1`), floats, and `None` are rejected as invalid types and normalized to `status=CompletionStatus.UNKNOWN`.
- **Conflicting Indicators:** If an agent return contains mutually contradictory signals (e.g., `{"status": "failed", "success": True}`), the harness refuses to apply arbitrary priority bias. It immediately normalizes to `status=CompletionStatus.UNKNOWN, success=False` with a detailed conflict diagnostic.
- **Harness Exception Classification:** Distinguishes user agent application failures (classified as `FAILED` / `GENUINE_FAILURE`) from test harness configuration or environment errors (`ConfigurationError`, `InfrastructureError`, classified as `INFRASTRUCTURE_ERROR` without leaking stack traces or sensitive credentials).

### Evaluation Decision Matrix (Rules A through G):
1. **Rule A:** Side effects satisfied, completion UNKNOWN (or conflicting/coerced indicators) -> `INSUFFICIENT_EVIDENCE`.
2. **Rule B:** Side effects satisfied, completion SUCCEEDED, stable keys -> `PASS`.
3. **Rule C:** Side effects committed, but agent reports FAILED -> `GENUINE_FAILURE` (orphaned commit).
4. **Rule D:** Positive side effects expected (`expected_side_effects > 0`), zero committed, but agent reports SUCCEEDED -> `GENUINE_FAILURE` (phantom success).
5. **Rule E (Clean Failure & Read-Only Success):**
   - **Zero-Effect Scenarios (`expected_side_effects: 0`)**: If agent reports `SUCCEEDED` with 0 commits -> `PASS`. If mutations occur -> `GENUINE_FAILURE` (prohibited side effect). If agent reports `UNKNOWN` -> `INSUFFICIENT_EVIDENCE` (never `PASS`).
   - **Expected Failure Scenarios (`expect_agent_success: false`)**: Agent reports `FAILED` with 0 commits -> `PASS`.
   - **Expected Success Scenarios (`expect_agent_success: true`)**: Agent reports `FAILED` with 0 commits -> `GENUINE_FAILURE`.
6. **Rule F:** Independent violations (duplicate effects, exceeded retry bounds, unstable idempotency keys) ALWAYS trigger `GENUINE_FAILURE`, even if completion is UNKNOWN.
7. **Rule G:** Empty ledger with unspecified expectations (`expected_side_effects: null`) or missing `effect_id` yields `INSUFFICIENT_EVIDENCE`, unless independent violations prove a failure.

---

## 5. Non-Functional Requirements

- **Performance:** A full scenario run with 3 retries must execute in < 150ms using virtual simulated clocks.
- **Type Safety:** 100% type annotations verified with `mypy --strict`.
- **Code Hygiene:** Linted and formatted with `ruff` rules (E, F, B, I, UP).
- **Test Coverage:** Unit and integration test suite covering > 90% of core logic.
- **OS Portability:** Works seamlessly on Windows, macOS, and Linux across Python 3.11+.
