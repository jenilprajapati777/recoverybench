# Changelog

All notable changes to **RecoveryBench** will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.2.0] - Unreleased (Release Preparation)

### Added
- **Phase 3B.1 - SDK Contract Harmonization:**
  - Standardized `RecoveryBenchTester` context manager and `test_agent()` high-level execution API.
  - Added first-class `AgentSpec` support for containerized execution configurations.
  - Enforced fail-closed behavior (Exit Code 3) when Docker isolation is requested but unavailable, eliminating silent fallback to trusted in-process mode.
  - Harmonized isolation mode parameter naming and aliases across SDK and CLI (`"isolated"`, `"trusted"`).
- **Phase 3B.2 - Scenario Schema Versioning & JSON Support:**
  - Introduced explicit declarative scenario schema versioning policy (`CURRENT_SCENARIO_SCHEMA_VERSION = 1`, `SUPPORTED_SCENARIO_SCHEMA_VERSIONS = frozenset({1})`).
  - Added first-class JSON scenario loading and schema validation via `ScenarioValidator.validate_file()` alongside existing YAML format.
  - Enforced strict rejection of unsupported schema versions with actionable error diagnostics.
  - Implemented deterministic scenario registry discovery order supporting mixed YAML and JSON definitions.
- **Phase 3B.3 - Stable JSON Report Contract:**
  - Established predictable, machine-readable JSON report contract (`ReportModel`, `report_schema_version: 1`) using strict Pydantic v2 validation.
  - Decoupled report specification schema version from application package version.
  - Implemented explicit three-state side-effect safety classification (`SideEffectSafety` enum: `VERIFIED_SAFE`, `UNSAFE`, `UNVERIFIED`) replacing legacy boolean assumptions.
  - Implemented rigorous ledger evidence cross-consistency checks in `ReportValidator`: validates actual committed counts, unique effect IDs, duplicate effect IDs, and non-empty identity semantics against the append-only event stream.
  - Added `recoverybench validate-report` CLI command for schema and semantic validation of external or archived JSON reports.
  - Automated sensitive data, token, and API key redaction in serialized JSON reports.
  - Implemented clean stdout JSON streaming in `recoverybench report <run_id> --json` with Rich console diagnostics routed to stderr.
- **Phase 3B.4 - Developer Experience & Release Readiness:**
  - Clarified PyPI installation documentation distinguishing the published 0.1.0 release from current GitHub development source.
  - Corrected contributor setup documentation to include the `langgraph` extra (`pip install -e ".[langgraph,dev]"`) required for full test suite execution.
  - Added prominent Docker trusted-mode guidance for local in-process development environments.
  - Updated canonical package version to `0.2.0` in preparation for release.

## [0.1.0] - 2026-11-16

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
