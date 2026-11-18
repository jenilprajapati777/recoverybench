# RecoveryBench — Phase 3B.3 Review Corrections Report

**Milestone:** Phase 3B.3 — Stable JSON Report Contract (Review Corrections)  
**Branch:** `phase-3b/stable-json-report-contract`  
**Base Commit:** `ec750eb` (Merge pull request #3 from `phase-3b/scenario-schema-versioning`)  
**Pull Request:** #4  
**Date:** 2026-11-18  
**Status:** All Corrections Implemented and Verified  

---

## 1. Executive Summary

This report documents the resolution of the three review findings identified during the code review of Phase 3B.3 (Stable JSON Report Contract). All corrections maintain strict backward compatibility, preserve the public report schema version (`1`), and enforce fail-closed security and integrity invariants.

---

## 2. Review Findings & Implemented Corrections

### Finding 1 — Strict Commit Status Validation

#### Context & Problem
In `src/recoverybench/reporting/schema.py`, `EventReport.commit_status` strictly validated against the `SUPPORTED_COMMIT_STATUSES` vocabulary (`COMMITTED`, `ABORTED`, `NOT_APPLICABLE`), but `InjectedFaultReport.commit_status` was only typed as a generic `str`. This allowed invalid, arbitrary, or unknown statuses (such as `"UNKNOWN"`, `"PENDING"`, `"ROLLED_BACK"`) and invalid types to bypass vocabulary validation on injected fault records.

#### Implemented Solution
Applied identical strict vocabulary validation on `InjectedFaultReport.commit_status` in `src/recoverybench/reporting/schema.py`:
```python
class InjectedFaultReport(StrictReportBaseModel):
    ...
    commit_status: str = Field(description="Commit status when fault occurred")
    ...

    @field_validator("commit_status", mode="before")
    @classmethod
    def validate_commit_status(cls, v: Any) -> str:
        if isinstance(v, CommitStatus):
            return v.value
        if isinstance(v, str):
            if v in SUPPORTED_COMMIT_STATUSES:
                return v
            raise ValueError(
                f"Invalid commit_status '{v}'. Must be one of {sorted(SUPPORTED_COMMIT_STATUSES)}."
            )
        raise ValueError(f"Invalid commit_status type '{type(v).__name__}'. Must be string or CommitStatus.")
```

#### Test Coverage
Added dedicated tests in `tests/test_json_report_contract.py`:
- `test_injected_fault_report_accepts_valid_commit_statuses`: Verifies acceptance of `"COMMITTED"`, `"ABORTED"`, `"NOT_APPLICABLE"` as strings and `CommitStatus` enums.
- `test_injected_fault_report_rejects_unknown_commit_status`: Proves rejection of `"UNKNOWN"`, `"PENDING"`, `"ROLLED_BACK"`, `""`, and unsupported statuses.
- `test_injected_fault_report_rejects_invalid_commit_status_types`: Proves rejection of integers, booleans, lists, dictionaries, and `None`.
- `test_report_validator_rejects_injected_fault_with_unknown_commit_status`: End-to-end rejection of serialized reports containing invalid injected fault statuses.

---

### Finding 2 — Complete Duplicate Verification Evidence

#### Context & Problem
In `ReportValidator._validate_semantics()`, duplicate calculation previously grouped events by:
```python
op_id = e.operation_id.strip() if e.operation_id and e.operation_id.strip() else ""
if op_id:
    op_to_effects.setdefault(op_id, set()).add(eff_id)
```
If a `COMMITTED` event had a missing or whitespace-only `operation_id`, it was silently excluded from `op_to_effects`. In adversarial scenarios, this could allow reports with multiple committed events lacking operation IDs to escape duplicate calculation and falsely claim `duplicate_count == 0`.

#### Implemented Solution
1. **Never Silently Exclude Committed Events**:
   In `ReportValidator._validate_semantics()` (Check 4), if any committed event lacks `operation_id`, duplicate verification immediately fails closed with an explicit error:
   ```python
   # Check 4: Duplicate effect IDs and duplicate count consistency derived from ledger events
   if missing_op_committed:
       errors.append(
           f"Cannot establish complete duplicate verification evidence: "
           f"{len(missing_op_committed)} COMMITTED event(s) lack operation_id "
           f"(Event IDs: {[e.event_id for e in missing_op_committed]}). "
           "Committed events must not be silently excluded from duplicate calculations."
       )
   else:
       op_to_effects: dict[str, set[str]] = {}
       for e in committed_events:
           if e.effect_id and e.effect_id.strip():
               eff_id = e.effect_id.strip()
               assert e.operation_id is not None
               op_id = e.operation_id.strip()
               op_to_effects.setdefault(op_id, set()).add(eff_id)

       derived_duplicate_count = sum(
           len(effs) - 1 for effs in op_to_effects.values() if len(effs) > 1
       )
       if model.side_effects.duplicate_count != derived_duplicate_count:
           errors.append(...)
   ```
2. **Fail Closed on Incomplete Evidence**:
   In `determine_side_effect_safety()`, if `has_incomplete_identity` is True or `side_effect_assertion_passed is False`, the safety status fails closed to `SideEffectSafety.UNVERIFIED` (unless proven duplicates or count discrepancies mandate `UNSAFE`).
3. **Preserve Legitimate Behaviors**:
   - Single committed event with complete identity (`VERIFIED_SAFE`).
   - Committed event followed by `NOT_APPLICABLE` idempotent retry (`VERIFIED_SAFE`).
   - Multiple distinct operations with distinct operation IDs (`VERIFIED_SAFE`).
   - Double-commit failures with same operation ID and multiple effects (`UNSAFE`).

#### Test Coverage
Added adversarial and valid behavior tests in `tests/test_json_report_contract.py`:
- `test_adversarial_multiple_committed_events_missing_all_operation_ids`: Confirms rejection when all committed events lack `operation_id`.
- `test_adversarial_multiple_committed_events_partially_missing_operation_ids`: Confirms rejection when some committed events have `operation_id` and others lack it.
- `test_valid_multiple_committed_events_distinct_operation_ids`: Proves that multi-operation scenarios with distinct `operation_id` values compute `duplicate_count == 0` and validate cleanly as `VERIFIED_SAFE`.
- `test_valid_behavior_legitimate_duplicate_commit_failure`: Confirms naive agent producing double charge evaluates to `UNSAFE` with 1 duplicate.
- `test_valid_behavior_committed_followed_by_not_applicable_retry`: Confirms idempotent deduplicated retries validate cleanly.

---

### Finding 3 — CLI JSON Stdout Regression Test

#### Context & Problem
The CLI command `recoverybench report <run_id> --json` with no `--output-dir` must stream pure, machine-readable JSON directly to stdout without producing console logs, progress bars, or disk artifacts. A regression test was required to lock down this invariant.

#### Implemented Solution
Enhanced `test_cli_report_json_stdout_without_output_dir_creates_no_files` in `tests/test_cli.py`:
- Invokes `report demo_safe_agent_run --json` with no `--output-dir`.
- Asserts exit code is 0.
- Asserts stdout starts with `{` and ends with `}` and parses cleanly via `json.loads`.
- Asserts parsed data validates against `ReportValidator.validate_dict()`.
- Asserts zero report files (`*.json`, `*.html`, `*.xml`) are created in the isolated reports directory, temporary path, or working directory.

---

## 3. Verification Evidence

### 3.1 Report Contract Tests (56 tests)
```bash
.venv\Scripts\pytest.exe tests/test_json_report_contract.py -v
```
**Output:**
```
============================= 56 passed in 0.55s ==============================
```

### 3.2 CLI Regression Tests (30 tests)
```bash
.venv\Scripts\pytest.exe tests/test_cli.py -v
```
**Output:**
```
======================== 29 passed, 1 skipped in 3.95s ========================
```
*(1 skipped test verifies fail-closed container isolation when Docker daemon is unavailable).*

### 3.3 Full Test Suite (330 tests)
```bash
.venv\Scripts\pytest.exe
```
**Output:**
```
======================= 328 passed, 2 skipped in 38.90s =======================
```

### 3.4 Code Quality & Static Analysis
- **Ruff:** `ruff check src tests` -> `All checks passed!`
- **Mypy:** `mypy src tests` -> `Success: no issues found in 81 source files`
- **Git Diff:** `git diff --check` -> `Clean (0 errors)`
- **Build:** `python -m build` -> `Successfully built recoverybench-0.1.0.tar.gz and recoverybench-0.1.0-py3-none-any.whl`

---

## 4. Constraint Compliance Checklist

- [x] Stay on branch `phase-3b/stable-json-report-contract`.
- [x] Base branch is `main` (commit `ec750eb`).
- [x] Pull Request is #4.
- [x] Do not merge or push.
- [x] Do not create a new branch.
- [x] Do not change public report schema version (remains `1`).
- [x] Do not delete unrelated untracked files (`PHASE_3B_2_FINAL_REVIEW.patch` preserved).
- [x] All 3 code review findings addressed and validated.
