# RecoveryBench - GitHub Actions & CI/CD Integration Guide

> **"Test the failure before production does."**

This guide explains how to set up continuous agent recovery testing in GitHub Actions, validate scenario definitions, execute test suites, publish JUnit test reports, upload CI artifacts, and block pull requests that violate side-effect safety invariants.

---

## 1. Why Recovery Testing Belongs in CI/CD

Traditional CI/CD pipelines verify that code compiles, lints, and passes standard unit tests. However, **standard unit tests rarely evaluate distributed network failures, tool drops, or recovery mechanics.**

When an autonomous AI agent code change is proposed in a pull request:
1. Did an engineer introduce an unhandled timeout?
2. Did a retry loop drop the transaction idempotency key?
3. Will network timeouts trigger duplicate charges in production?

RecoveryBench runs deterministically in CI in **milliseconds**, providing ground-truth verification of state commits before code is merged.

---

## 2. GitHub Actions Workflows

RecoveryBench provides two pre-configured workflows and one composite action:

| Workflow / Action | Location | Purpose |
| :--- | :--- | :--- |
| **Main CI Pipeline** | [`.github/workflows/recoverybench-ci.yml`](../.github/workflows/recoverybench-ci.yml) | Matrix testing across Python 3.11, 3.12, and 3.13 on `push` and `pull_request`. |
| **PR Quality Gate** | [`.github/workflows/recoverybench-pr-gate.yml`](../.github/workflows/recoverybench-pr-gate.yml) | Fast gating for pull requests with artifact uploads. |
| **Composite Action** | [`.github/actions/recoverybench-test/action.yml`](../.github/actions/recoverybench-test/action.yml) | Reusable action for external repos to drop into their workflows. |

---

## 3. Production Workflow Reference

Here is the complete `.github/workflows/recoverybench-ci.yml`:

```yaml
name: RecoveryBench CI

on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]

permissions:
  contents: read
  checks: write
  pull-requests: write

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  recovery-test:
    name: Recovery Evaluation (Python ${{ matrix.python-version }})
    runs-on: ubuntu-latest
    strategy:
      fail-fast: false
      matrix:
        python-version: ["3.11", "3.12", "3.13"]

    steps:
      - name: Check out repository
        uses: actions/checkout@v4

      - name: Set up Python ${{ matrix.python-version }}
        uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
          cache: "pip"

      - name: Install RecoveryBench & Dependencies
        run: |
          python -m pip install --upgrade pip
          python -m pip install -e ".[langgraph,dev]"

      - name: Cache/Pull Docker Sandbox Image
        run: |
          docker pull python:3.11-slim || true

      - name: Environment Health Check
        run: recoverybench doctor

      - name: Validate Scenario Definitions
        run: recoverybench validate scenarios

      - name: Execute RecoveryBench Suite
        run: |
          recoverybench suite scenarios \
            --agent "idempotent" \
            --isolation "isolated" \
            --output-dir "reports" \
            --junit

      - name: Publish JUnit Test Results
        uses: mikepenz/action-junit-report@v4
        if: always()
        with:
          report_paths: "reports/*_report.xml"
          check_name: "RecoveryBench Results (${{ matrix.python-version }})"
          fail_on_failure: true
          require_tests: true

      - name: Upload Test Report Artifacts
        uses: actions/upload-artifact@v4
        if: always()
        with:
          name: recoverybench-reports-py${{ matrix.python-version }}
          path: reports/
          retention-days: 30
```

---

## 4. Using the Turnkey Composite Action

If you want to integrate RecoveryBench into an existing repository without copying CLI commands, use the composite action:

```yaml
steps:
  - uses: actions/checkout@v4
  - uses: actions/setup-python@v5
    with:
      python-version: "3.11"
  - run: pip install -e .

  - name: Run RecoveryBench Testing
    uses: ./.github/actions/recoverybench-test
    with:
      scenarios-dir: "scenarios"
      agent: "idempotent"
      output-dir: "reports"
      fail-on-failure: "true"
```

### Action Inputs & Outputs

| Input | Description | Default |
| :--- | :--- | :--- |
| `scenarios-dir` | Directory containing YAML recovery scenarios | `"scenarios"` |
| `agent` | Agent to test (`idempotent`, `naive`, `langgraph-idempotent`, `langgraph-naive`) | `"idempotent"` |
| `output-dir` | Directory to save generated reports | `"reports"` |
| `isolation` | Execution isolation mode (`isolated` container or `trusted` in-process) | `"isolated"` |
| `junit` | Whether to emit JUnit XML reports | `"true"` |
| `fail-on-failure` | Whether to fail step on assertion violations | `"true"` |

---

## 5. Exit Codes & PR Gating

RecoveryBench returns standard exit codes that integrate seamlessly with CI step status:

- `0` (`EXIT_PASS`): All recovery assertions passed. Job succeeds.
- `1` (`EXIT_RECOVERY_FAILED`): One or more assertions failed (e.g. duplicate charge detected). **Fails the CI job.**
- `2` (`EXIT_CONFIG_ERROR`): Invalid scenario YAML or configuration schema error. **Fails the CI job.**
- `3` (`EXIT_INFRASTRUCTURE_ERROR`): System error or unhandled runtime crash. **Fails the CI job.**

---

## 6. GitHub Step Summary & Artifacts

### 6.1 Automatic Step Summaries
When running in GitHub Actions, RecoveryBench automatically detects `$GITHUB_STEP_SUMMARY` and writes a rich GitHub Flavored Markdown summary directly to the job overview tab:

```
## 🛡️ RecoveryBench Recovery Test Summary

- **Total Scenarios Evaluated:** 6
- **Passed:** ✅ 6
- **Failed:** 0
- **Duplicate Side-Effects Detected:** 0

### 📊 Execution & Ground-Truth Verification Matrix

| Status | Scenario | Agent | Verdict | Effects (Exp/Act) | Duplicates | Stable Key? | Duration |
| :---: | :--- | :--- | :---: | :---: | :---: | :---: | ---: |
| ✅ PASS | Payment Timeout After Commit | `idempotent_payment_agent` | **PASS** | 1 / 1 | 0 | Yes | 10.3ms |
| ✅ PASS | Payment HTTP 429 Rate Limiting | `idempotent_payment_agent` | **PASS** | 1 / 1 | 0 | Yes | 8.5ms |
```

If an assertion fails, the summary embeds a collapsible `<details>` drawer citing the exact ledger events and transaction receipt tokens backing the failure.

### 6.2 CI Artifact Retention
By using `actions/upload-artifact@v4` with `if: always()`, all reports are retained on GitHub:
- `*_report.html`: Interactive visual report with collapsible evidence drawers.
- `*_report.json`: Machine-readable schema for automated metric extraction.
- `*_report.xml`: Standard JUnit XML ingested by GitHub Actions check runs.
- `suite_summary.json`: Aggregated suite matrix.

---

## 7. Security Model: Keeping Secrets Out of CI Logs

RecoveryBench is built specifically to prevent credential leakage in CI/CD pipelines:

1. **Deterministic Hashing:**
   - Idempotency keys are hashed using SHA-256 (`idempotency_key_hash`).
   - Request payloads are fingerprinted using SHA-256 (`request_fingerprint`).
2. **Sensitive Key Redaction:**
   - The security engine ([`security.py`](../src/recoverybench/security.py)) automatically detects and redacts sensitive keys:
     `api_key`, `authorization`, `token`, `secret`, `password`, `credential`, `card_number`, `cvv`, `ssn`.
   - Redacted fields are replaced with `"[REDACTED]"`.
3. **Regex Pattern Masking:**
   - Credit card numbers (13-19 digits) are masked as `"[REDACTED_CARD]"`.
   - Bearer authorization tokens are masked as `"Bearer [REDACTED_TOKEN]"`.
4. **Zero External Calls:**
   - Mocks run in local memory or isolated SQLite instances. No external API keys are needed or logged.
