# Changelog

All notable changes to **RecoveryBench** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] - Unreleased (Release Preparation)

### Added
- **Core Recovery Testing Harness:**
  - Precision fault injector supporting 6 deterministic fault modes: `timeout_before_commit`, `timeout_after_commit` (Two Generals' Hazard), `duplicate_delivery`, `http_429_then_success`, `http_500_transient`, and `malformed_tool_response`.
  - Zero-sleep virtual time engine simulating exponential backoffs and retry intervals in milliseconds without wall-clock sleep.
- **Append-Only Effect Ledger:**
  - SQLite and in-memory transactional ledger tracking all mutation attempts.
  - Decoupled transaction identity model distinguishing `operation_id` (logical business boundary), `attempt_id` (transmission attempt), `effect_id` (ground-truth server mutation), and `request_fingerprint` (diagnostic payload SHA-256 hash).
- **Deterministic Recovery Evaluator:**
  - Rule-based evaluation asserting side-effect counts, duplicate side-effect detection, idempotency key stability, retry budgets, circuit breakers, and unknown-outcome reconciliation.
  - Three-state completion verification model (`SUCCEEDED`, `FAILED`, `UNKNOWN`) with strict type validation (no boolean coercion) to prevent false successes and orphaned mutations.
- **Container Execution Sandbox & Network Isolation:**
  - Linux container isolation configured with `--network=none`, `--cap-drop=ALL`, `--read-only`, non-root execution (`nobody:nogroup`), and `--security-opt=no-new-privileges`.
  - Workspace staging with automated secret and credential exclusion (`.env*`, `.ssh`, `.aws`, `.git`, certificates) and symlink escape detection.
  - Framed JSON Lines IPC protocol over stdin/stdout with stderr redirection and secret masking.
  - Fail-closed enforcement (Exit Code 3) when Docker isolation is requested but unavailable.
  - Optional trusted in-process execution mode (`--isolation trusted`) for local iteration.
- **Reusable Agent Framework Adapters:**
  - Generic Python callable adapter (`@agent`, `wrap_agent()`) supporting arbitrary functions and methods.
  - Native LangGraph framework adapter bridging LangGraph `StateGraph` and LangChain `@tool` definitions.
- **Command-Line Interface:**
  - Commands: `doctor`, `init`, `list-scenarios`, `validate`, `demo`, `run`, `suite`, `replay`, `report`.
  - Global `--version` (`-v`) flag displaying authoritative package version metadata.
  - Strict exit codes: 0 (PASS), 1 (GENUINE_FAILURE), 2 (CONFIG_ERROR), 3 (INFRASTRUCTURE_ERROR).
- **Multi-Format Reporting:**
  - Self-contained dark-mode HTML reports with interactive collapsible evidence drawers citing ledger events.
  - Machine-readable JSON reports.
  - Standard JUnit XML test reports with GitHub Step Summary annotations.
- **Packaging & CI/CD:**
  - Full PEP 621 and PEP 639 license compliance (`Apache-2.0`).
  - PyPI classifiers, metadata, and keywords for automated index discovery.
  - Comprehensive 6-matrix GitHub Actions CI pipeline testing across Ubuntu 22.04 / 24.04 and Python 3.11, 3.12, 3.13.
  - Dedicated distribution smoke test (`scripts/distribution_smoke_test.py`) verifying clean wheel installations outside the repository.
