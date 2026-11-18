# RecoveryBench — Phase 3B.3 Stable JSON Report Contract Report

**Milestone:** Phase 3B.3 — Stable JSON Report Contract (Final Contract Hardening)
**Branch:** `phase-3b/stable-json-report-contract`
**Base Commit:** `ec750eb` (Merge pull request #3 from `phase-3b/scenario-schema-versioning`)
**Package Version:** `0.1.0`
**Status:** Verification Complete — Ready for Review

---

## 1. Executive Summary

Phase 3B.3 establishes a predictable, strictly versioned, and machine-readable JSON execution report contract for RecoveryBench. These reports provide the authoritative execution evidence consumed by local dashboards, CI/CD pipelines, developer tooling, and automated agent reliability analyses.

Following final contract hardening and ledger evidence verification reviews, critical safeguards were implemented:
1. Enforced strict Pydantic field types (`strict=True`) across all report models to eliminate invalid type coercions.
2. Guaranteed that known evidence of duplicate effects and unexpected counts takes strict precedence over insufficient evidence.
3. Enforced bidirectional safety validation ensuring that `ReportValidator` and `JSONReporter` adhere to identical, non-contradictory safety rules via a shared pure helper (`determine_side_effect_safety`).
4. Enforced ledger integrity and commit-status vocabulary validation:
   - Both `EventReport.commit_status` and `InjectedFaultReport.commit_status` strictly validate against the `CommitStatus` enum (`COMMITTED`, `ABORTED`, `NOT_APPLICABLE`). Unknown status values fail closed immediately.
   - `COMMITTED` events must have a non-empty `effect_id`.
   - `COMMITTED` events must have a non-empty `operation_id` when duplicate verification requires it (multiple events, retries, multi-operation, or passing/clean verdicts).
   - Whitespace-only identifiers are rejected across all string identifier fields (`effect_id`, `operation_id`, `attempt_id`, `tool_name`, `action_type`, `request_fingerprint`).
   - Missing identity evidence fails closed and never allows a report to be classified as `VERIFIED_SAFE`.
5. Enforced ledger evidence consistency checks and complete duplicate accounting:
   - Verified `side_effects.actual_count` against the count of unique committed effect IDs derived strictly from ledger events with `commit_status == "COMMITTED"`.
   - Verified `side_effects.committed_effect_ids` against committed effect IDs derived strictly from ledger events with `commit_status == "COMMITTED"`.
   - Verified complete duplicate verification evidence: every `COMMITTED` event is accounted for in duplicate calculations, and committed events with missing or whitespace-only `operation_id` are never silently excluded.
   - Enforced commit status consistency: confirmed that non-committed events (`NOT_APPLICABLE`, `ABORTED`) cannot falsely establish successful side effects.
6. Made CLI `report <run_id> --json` behavior unambiguous: streams pure JSON directly to stdout and creates zero report files on disk when `--output-dir` is omitted; defaults to JSON-only output on disk when `--output-dir` is provided, preventing automatic HTML/JUnit generation unless explicitly requested via `--format`.
7. Universal sensitive-data redaction across every serialization and validation entry point.

---

## 2. Report Schema Contract & Status Semantics

### 2.1 Versioning Policy & Strict Base Model

- **`report_schema_version` (int = 1)**: Authoritative contract schema version.
- **`recoverybench_version` (str = "0.1.0")**: Package release version.
- **`version` (str = "0.1.0")**: Legacy alias preserved for backward compatibility.

All report models inherit from `StrictReportBaseModel`:
```python
class StrictReportBaseModel(BaseModel):
    """Shared base model enforcing strict typing and forbidding unknown fields."""
    model_config = ConfigDict(extra="forbid", strict=True)
```

Validation strictly fails closed:
- Missing `report_schema_version` raises `ReportValidationError`.
- Non-integer types (`"1"`, `True`, `False`, `1.0`) raise `ReportValidationError`.
- Type coercion attempts (strings to ints, booleans to ints, ints to booleans, numbers to strings, non-lists to lists) are strictly rejected.
- Unsupported integer versions (`0`, `2`, `99`) raise `ReportValidationError` citing `Supported versions: [1]`.
- Unknown extra fields fail validation closed (`extra="forbid"`).

### 2.2 Authoritative Side-Effect Classification Priority

Side-effect safety follows a deterministic priority hierarchy implemented in `determine_side_effect_safety`:

```python
class SideEffectSafety(StrEnum):
    VERIFIED_SAFE = "VERIFIED_SAFE"
    UNSAFE = "UNSAFE"
    UNVERIFIED = "UNVERIFIED"
```

1. **`duplicate_count > 0` -> `UNSAFE`**: Proven duplicate side-effects take highest precedence over any other evaluation factor, including `INSUFFICIENT_EVIDENCE`.
2. **Known unexpected count with ledger evidence -> `UNSAFE`**: When expected count is defined and actual count does not match, proven discrepancy results in `UNSAFE`.
3. **`INSUFFICIENT_EVIDENCE` without proven unsafe effects -> `UNVERIFIED`**: When evidence is incomplete or missing, safety is classified as unverified.
4. **Infrastructure crash before events were recorded -> `UNVERIFIED`**: If an infrastructure error aborted execution before ledger events were logged.
5. **Expected count unspecified (`expected_count is None`) -> `UNVERIFIED`**: In open-ended scenarios where expectations are not defined, safety cannot be verified.
6. **Exact expected count with sufficient evidence and 0 duplicates -> `VERIFIED_SAFE`**: Only when actual committed effects equal authoritative expected effects, duplicate count is zero, and evidence is complete.

Boolean `side_effect_safe` is strictly `True` only when `side_effect_safety == SideEffectSafety.VERIFIED_SAFE`.

### 2.3 Bidirectional Safety Semantic Validation

`ReportValidator._validate_semantics()` executes identical classification logic using `determine_side_effect_safety` and rejects all contradictory report states:
- Duplicate effects with safety status `VERIFIED_SAFE` or `UNVERIFIED`.
- Known unexpected effect counts with safety status `VERIFIED_SAFE` or `UNVERIFIED`.
- `VERIFIED_SAFE` with insufficient evidence or missing expected counts.
- Inconsistent `outcome.side_effect_safety` and `side_effects.safety_status`.
- Inconsistent boolean `safe` and `side_effect_safe` fields.

### 2.4 Ledger Evidence Consistency Verification

`ReportValidator._validate_semantics()` directly cross-checks the serialized ledger events against reported summary fields:
1. **Committed Effects Count (`actual_count`)**: Verified against the count of unique committed effect IDs derived from events with `commit_status == "COMMITTED"`. If no events are committed, `actual_count > 0` is strictly rejected.
2. **Committed Effect IDs (`committed_effect_ids`)**: Set-equality check between reported IDs and derived unique effect IDs from `COMMITTED` events. Rejects fabricated or missing IDs, and detects duplicate IDs inside the reported list.
3. **Duplicate Effects Count (`duplicate_count`)**: Derived per `operation_id` by grouping proven commits. Rejects reports attempting to hide duplicates by tampering with `duplicate_count`.
4. **Commit Status Integrity**: Confirms that non-committed events (`NOT_APPLICABLE`, `ABORTED`) do not establish committed effects or appear in `committed_effect_ids`.

### 2.5 Ledger Integrity & Committed Event Identity

`EventReport`, `InjectedFaultReport`, and `ReportValidator` enforce strict ledger integrity and identity completeness:
1. **Commit Status Vocabulary**: Both `EventReport.commit_status` and `InjectedFaultReport.commit_status` strictly validate against `CommitStatus` (`COMMITTED`, `ABORTED`, `NOT_APPLICABLE`). Unknown statuses fail validation closed immediately.
2. **Committed Mutation Proof**: Every `COMMITTED` event must have a non-empty `effect_id`.
3. **Operation Identity for Duplicate Relationships**: A `COMMITTED` event must have a non-empty `operation_id` whenever duplicate verification requires it (multiple events, retries, multi-operation, or passing/clean verdicts).
4. **Complete Duplicate Verification Accounting**: Every `COMMITTED` event is accounted for during duplicate calculations. Committed events with missing or whitespace-only `operation_id` are never silently excluded via filter bypasses.
5. **No Whitespace-Only Identifiers**: All string identifiers are validated to prohibit empty or whitespace-only strings.
6. **Fail-Closed on Incomplete Evidence**: Missing identity evidence fails closed and never allows a report to be classified as `VERIFIED_SAFE`. Legitimate idempotent retries (`COMMITTED` followed by `NOT_APPLICABLE`), independent multi-operation commits, and duplicate commit failures remain fully supported.

---

## 3. Serialized Report Example (Schema v1)

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

---

## 4. Files Created and Modified

| File Path | Status | Nature of Change |
| :--- | :--- | :--- |
| [`src/recoverybench/errors.py`](file:///D:/RecoveryBench/src/recoverybench/errors.py) | Modified | Added `ReportValidationError(RecoveryBenchError)` with `exit_code=2`. |
| [`src/recoverybench/reporting/schema.py`](file:///D:/RecoveryBench/src/recoverybench/reporting/schema.py) | **Created** | Defined `StrictReportBaseModel(extra="forbid", strict=True)`, `SideEffectSafety`, `SUPPORTED_VERDICTS`, `determine_side_effect_safety()`, strict report models, and `ReportValidator` with bidirectional semantic consistency checks. |
| [`src/recoverybench/reporting/json_reporter.py`](file:///D:/RecoveryBench/src/recoverybench/reporting/json_reporter.py) | Modified | Delegated `classify_side_effect_safety()` to `determine_side_effect_safety()`, enforced universal redaction in `to_json_string()`, stable report generation in `generate_report_dict()`, `generate_report_model()`, and `validate_report()`. |
| [`src/recoverybench/reporting/__init__.py`](file:///D:/RecoveryBench/src/recoverybench/reporting/__init__.py) | Modified | Exported `StrictReportBaseModel`, `determine_side_effect_safety`, `SideEffectSafety`, `SUPPORTED_VERDICTS`, report models, `ReportValidator`, and `ReportValidationError`. |
| [`src/recoverybench/engine/context.py`](file:///D:/RecoveryBench/src/recoverybench/engine/context.py) | Modified | Added `to_report()`, `to_report_dict()`, and `to_json()` methods to `RunResult`. |
| [`src/recoverybench/cli.py`](file:///D:/RecoveryBench/src/recoverybench/cli.py) | Modified | Implemented unambiguous CLI JSON behavior: `report <run_id> --json` touches no disk files when `--output-dir` is omitted; when `--output-dir` is provided, saves JSON only by default; generates HTML/JUnit only when explicitly requested; keeps stdout pure JSON; routes status to stderr. |
| [`src/recoverybench/sdk/__init__.py`](file:///D:/RecoveryBench/src/recoverybench/sdk/__init__.py) | Modified | Exported `SideEffectSafety`, `ReportModel`, `ReportValidator`, `ReportValidationError`, and report schema versions. |
| [`src/recoverybench/__init__.py`](file:///D:/RecoveryBench/src/recoverybench/__init__.py) | Modified | Exported `SideEffectSafety`, `ReportModel`, `ReportValidator`, `ReportValidationError`, and report schema versions. |
| [`tests/test_json_report_contract.py`](file:///D:/RecoveryBench/tests/test_json_report_contract.py) | **Created** | 49 comprehensive regression tests covering all 6 safety states, strict Pydantic field types, duplicate-precedence priority, bidirectional semantic validation, ledger consistency, commit-status vocabulary validation, and committed event identity integrity. |
| [`tests/test_cli.py`](file:///D:/RecoveryBench/tests/test_cli.py) | Modified | Added CLI regression tests for fileless report streaming, JSON-only output when `--output-dir` is supplied, explicit `--format all` generation, `run --json`, and `validate-report`. |
| [`README.md`](file:///D:/RecoveryBench/README.md) | Modified | Documented stable JSON report contract, 3-state side-effect safety semantics, CLI usage, SDK usage, and example report structure. |
| [`docs/SDK_INTEGRATION_GUIDE.md`](file:///D:/RecoveryBench/docs/SDK_INTEGRATION_GUIDE.md) | Modified | Added Section 9 with complete report contract specification, strict types, 3-state safety semantics, CLI fileless streaming, and full JSON example. |
| [`PHASE_3B_3_JSON_REPORT_CONTRACT_REPORT.md`](file:///D:/RecoveryBench/PHASE_3B_3_JSON_REPORT_CONTRACT_REPORT.md) | **Created** | Comprehensive Phase 3B.3 completion and final contract hardening report. |

---

## 5. Verification Commands & Actual Results

All tests were executed against `D:\RecoveryBench` using the local virtual environment (`.venv`, Python 3.13.8).

### 5.1 Report Contract Tests (56 tests)
```bash
.venv\Scripts\pytest.exe tests/test_json_report_contract.py -v
```
**Result:**
```
============================= 56 passed in 0.55s ==============================
```

### 5.2 Existing Reporting Tests (3 tests)
```bash
.venv\Scripts\pytest.exe tests/test_reporting.py -v
```
**Result:**
```
============================== 3 passed in 0.12s ==============================
```

### 5.3 CLI Regression Tests (30 tests)
```bash
.venv\Scripts\pytest.exe tests/test_cli.py -v
```
**Result:**
```
======================== 29 passed, 1 skipped in 3.95s ========================
```
*(1 skipped test requires Docker daemon to be unavailable on host to verify fail-closed behavior).*

### 5.4 Combined Contract & CLI Tests (86 tests)
```bash
.venv\Scripts\pytest.exe tests/test_json_report_contract.py tests/test_cli.py -v
```
**Result:**
```
======================== 85 passed, 1 skipped in 4.45s ========================
```

### 5.5 Full Pytest Suite (330 tests)
```bash
.venv\Scripts\pytest.exe -q
```
**Result:**
```
======================= 328 passed, 2 skipped in 38.90s =======================
```
*(2 skipped tests are container lifecycle integration checks that require specific environment preconditions).*

### 5.6 Ruff Linter
```bash
.venv\Scripts\ruff.exe check src tests
```
**Result:**
```
All checks passed!
```

### 5.7 Mypy Strict Type Analysis
```bash
.venv\Scripts\mypy.exe src tests
```
**Result:**
```
Success: no issues found in 81 source files
```

### 5.8 Git Diff Whitespace & Conflict Check
```bash
git diff --check
```
**Result:** Clean (0 errors).

### 5.9 Package Build Verification
```bash
python -m build
```
**Result:** Successfully built `recoverybench-0.1.0.tar.gz` and `recoverybench-0.1.0-py3-none-any.whl`.

---

## 6. Known Limitations

1. **Dashboard UI Out of Scope**: Phase 3B.3 delivers the machine-readable JSON report contract; the browser dashboard UI for visualizing these reports will be implemented in subsequent phases.
2. **Docker Daemon Requirement for Isolated Test Execution**: Generating reports with `isolation="isolated"` requires a running Docker daemon on the host. In environments without Docker, `isolation="trusted"` provides equivalent evaluation fidelity for in-process testing.

---

## 7. Constraint Compliance Checklist

- [x] Stay on branch `phase-3b/stable-json-report-contract`.
- [x] Base branch is `main` (commit `ec750eb`).
- [x] Do not build the dashboard in this phase.
- [x] Do not add new agent framework integrations.
- [x] Do not redesign the execution engine.
- [x] Do not change scenario schema versioning unnecessarily.
- [x] Do not introduce a database or external service.
- [x] Do not commit, push, or merge.
- [x] Do not tag or release.
- [x] Preserve existing public APIs and backward compatibility.
