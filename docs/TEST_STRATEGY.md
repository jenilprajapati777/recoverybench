# RecoveryBench - Test Strategy & Verification Plan

**Version:** 0.3.0  
**Framework:** Pytest  
**Goal:** Deterministic, High-Confidence Verification, Real Sandbox Isolation

---

## 1. Testing Philosophy

Recovery testing tools must themselves be immune to non-determinism. Flaky tests erode developer trust. To promote rock-solid stability:

1. **Virtual Time Exclusively:** Real-time sleeps (`time.sleep`) are strictly banned from automated unit and integration tests. Simulated clocks advance deterministically by explicit ticks.
2. **Seeded Pseudo-Randomness:** Any fault modes with probabilistic triggers (e.g., jitter, dropped response odds) accept a deterministic integer seed.
3. **In-Memory & SQLite Dual Verification:** Fast test cases run against in-memory ledgers; integration and CLI suites verify SQLite file persistence.
4. **Isolated Mock Sandboxes:** Every test case initializes a fresh, unpolluted mock service registry.
5. **Real Container Isolation with Honest Skipping:** Security tests verify real Linux container isolation with `--network=none`. If Docker Engine is unavailable in the execution environment, tests are explicitly marked and reported as SKIPPED—never fabricated as passed.

---

## 2. Test Classification Matrix

```
+-----------------------------------------------------------------------------------+
| LAYER 4: Security & Execution Sandbox Tests (tests/security/)                     |
| - Container configuration, resource bounds, capabilities dropping                 |
| - Filesystem staging, credential stripping, symlink escape detection              |
| - Worker stdio IPC protocol, JSON Lines framing, stdout redirection, log masking  |
| - Container lifecycle, Docker fail-closed policies, cleanup guarantees            |
| - Network isolation: DNS blocking, outbound TCP socket blocking, HTTP blocking    |
+-----------------------------------------------------------------------------------+
| LAYER 3: CLI & CI/CD End-to-End Tests                                             |
| - CLI runner verifying exit codes (0, 1, 2, 3), JSON/HTML/XML output, demo command |
| - GitHub Actions workflow integration, suite batch runner, secret redaction       |
+-----------------------------------------------------------------------------------+
| LAYER 2: Integration & Recovery Scenarios                                         |
| - NaiveAgent vs. timeout_after_commit -> Evaluator asserts DUPLICATE_EFFECT       |
| - SafeAgent vs. timeout_after_commit  -> Evaluator asserts PASS (1 Commit)        |
| - Replay engine bit-for-bit event reconstruction                                  |
| - LangGraph adapter integration and state persistence                             |
+-----------------------------------------------------------------------------------+
| LAYER 1: Core Unit Tests                                                          |
| - Scenario schema validation & error reporting                                    |
| - Mock payment service state & idempotency handling                               |
| - Fault injection interceptors (timeout_before, timeout_after, 429, 500)          |
| - Append-only ledger hashing, sequence ordering, decoupled transaction identity   |
| - Evaluator rule assertions (Rules A through G) & false-success prevention        |
+-----------------------------------------------------------------------------------+
```

---

## 3. Specific Test Specifications

### 3.1 Unit Test Suites
- `test_schema_validation.py`:
  - Validates correct YAML scenario schemas.
  - Rejects unknown fault types with clear validation error.
  - Rejects missing required fields (`scenario_id`, `tool`, `assertions`).
- `test_mock_payment_service.py`:
  - Direct execution without faults records single committed charge.
  - Duplicate request with identical idempotency key returns cached receipt without double-debiting balance.
  - Request with different idempotency key debits balance independently.
- `test_fault_injection.py`:
  - `timeout_before_commit` triggers exception before mock service commits state.
  - `timeout_after_commit` commits state to mock store, then raises `ToolTimeoutError`.
  - `http_429_then_success` fails $N$ times with rate limit, then succeeds on attempt $N+1$.
- `test_effect_ledger.py`:
  - Append events updates monotonically increasing `event_id`.
  - Anonymization: Idempotency keys and payload arguments are hashed via SHA-256.
  - SQLite persistence correctly loads across connections.
- `test_recovery_evaluator.py`:
  - Detects duplicate commits when actual commits > expected commits.
  - Detects missing commits when actual commits < expected commits.
  - Detects unstable idempotency keys across retry attempts.

### 3.2 Integration & Identity Suites
- `test_transaction_identity.py` (Exhaustive Transaction Identity Hardening Matrix):
  - **Case A (Single-Operation Declared Default Identity)**: Verifies scenario-level default `op_{scenario_id}` assignment, unique attempt generation, single effect commit, and evidence citation.
  - **Case B (Multi-Operation 5 Independent Operations)**: Verifies that 5 independent operations using `with tool.operation(op_id):` succeed with 5 unique effect IDs, 0 duplicates, and retry budgets computed per-operation.
  - **Case C (Independent Identical Operations)**: Verifies two operations with identical payloads and identical `request_fingerprint`s produce 2 proven effects with 0 duplicates when given distinct `operation_id`s.
  - **Case D (Multi-Operation Omitted Identity)**: Verifies that omitting `operation_id` in a multi-operation scenario yields `INSUFFICIENT_EVIDENCE` and cites the affected event IDs without falling back to fingerprint guessing.
  - **Case E (Retry Identity Preservation)**: Verifies that retries preserve the original `operation_id`. Safe retry yields `PASS` (cached effect returned); unsafe retry yields `GENUINE_FAILURE` (duplicate detected under the same operation).
  - **Case F (Committed Event Lacking Effect ID)**: Verifies that committed events without `effect_id` yield `INSUFFICIENT_EVIDENCE`, are excluded from proven commits, and cite event IDs; while preserving `GENUINE_FAILURE` if independent violations (retry budget or unstable keys) occur.
  - **Case G (Historical SQLite Event Migration & Evaluation)**: Verifies legacy databases with NULL identity columns auto-migrate safely, and evaluating legacy records yields `INSUFFICIENT_EVIDENCE` rather than false passes.
  - **Case H (Existing Payment Demo & LangGraph Integration)**: Verifies that CLI demo single-operation scenarios and LangGraph tool invocations within operation contexts function seamlessly.
- `test_demo_comparison.py`:
  - Runs `NaivePaymentAgent` vs `IdempotentPaymentAgent` on `payment_timeout_after_commit`.
  - Verifies double-charge detection on naive retry vs clean recovery on idempotent retry.

### 3.3 False-Success Prevention Suites
- `test_false_success_prevention.py`:
  - Verifies 16 distinct false-success scenarios covering Rules A through G.
  - Verifies return value normalization across `None`, `bool`, `dict`, unhandled exceptions, and unsupported types.
  - Verifies phantom success detection (`actual="phantom_success_no_commits"`).
  - Verifies orphaned commit detection (`actual="agent_reported_failure_after_commit"`).
  - Verifies independent violation precedence over ambiguous completion states.
  - Verifies backwards compatibility of `AgentResult(success=True)` and `AgentResult(success=False)`.
- `test_false_success_final_hardening.py`:
  - 20 comprehensive end-to-end scenarios validating P0-2 Final Hardening.
  - Task 1: Read-only and zero-side-effect scenarios (`expected_side_effects: 0`, positive, and `null`), proving clean `PASS` for read-only success, detection of prohibited mutations, and prevention of false passes on `UNKNOWN` completion.
  - Task 2: Strict boolean type validation, explicitly rejecting string booleans (`"true"`, `"false"`, `"0"`), integer flags (`1`, `0`), and nulls.
  - Task 3: Conflicting indicator detection (`{"status": "failed", "success": True}`), eliminating silent priority bias and normalizing to `UNKNOWN` with conflict diagnostics.
  - Task 4: Infrastructure vs user exception classification, ensuring harness `InfrastructureError` and `ConfigurationError` bypass agent failure absorption, classify as `INFRASTRUCTURE_ERROR`, and sanitize API keys (`sk_live_...`) and credentials without exposing raw tracebacks in standard reports.

### 3.4 Security & Execution Sandbox Test Suite (`tests/security/`)
- `test_container_configuration.py`:
  - Validates `SandboxConfig` default security parameters (512MB RAM, 1.0 CPU, 64 PIDs, 30s timeout, nobody user, ALL cap-drop).
  - Validates rejection of dangerously low or invalid memory formats, negative CPU/PID limits, and invalid timeout values.
  - Validates `build_run_command` generates exact hardened arguments: `--network=none`, `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`, `--pids-limit`, `--memory`, `--cpus`, `--user`, `--tmpfs`, and read-only volume mounts (`:ro`).
  - Asserts that forbidden flags are NEVER present in generated container commands: no `--privileged`, no `--net=host`, no `--pid=host`, no `--device`, no `/var/run/docker.sock`.
  - Validates `AgentSpec` environment isolation: unapproved host secrets (`AWS_SECRET_ACCESS_KEY`, `OPENAI_API_KEY`) are stripped.
- `test_filesystem_isolation.py`:
  - Validates `WorkspaceStaging.is_safe_path` rejects excluded directories (`.git`, `.venv`, `node_modules`, `.ssh`, `.aws`, `.azure`) and credential files (`.env*`, `*.pem`, `*.key`, `id_rsa`, `credentials.json`).
  - Validates `stage_workspace` copies safe source files while strictly omitting excluded files and directories.
  - Validates symlink escape detection: symlinks targeting external files outside the workspace root are detected and aborted with `ConfigurationError`.
  - Validates valid internal symlinks are preserved safely.
  - Validates cleanup removes temporary directories deterministically.
- `test_worker_ipc.py`:
  - Validates JSON Lines protocol encoding and decoding for all message types (`InitRequest`, `ReadyResponse`, `ExecuteRequest`, `ToolCallRequest`, `ToolCallResponse`, `CompletionResponse`, `ShutdownRequest`).
  - Validates message size limits: frames exceeding `max_bytes` raise `ValueError`.
  - Validates worker secret masking: API keys (`sk_live_...`), bearer tokens, and passwords in diagnostic logs are replaced with `[REDACTED_SECRET]`.
  - Validates full end-to-end IPC roundtrip by launching `worker.py` as an isolated subprocess with stdio pipes:
    - Host sends `init` -> Worker responds `ready`.
    - Host sends `execute` -> Worker invokes agent and sends `tool_call`.
    - Host sends `tool_response` -> Worker processes data and sends `completion` (`SUCCEEDED`).
    - Host sends `shutdown` -> Worker exits cleanly with code 0.
    - Asserts agent `print()` statements to stdout are redirected to stderr, preventing protocol stream corruption.
  - Validates agent crashes (unhandled exceptions) are captured by worker and returned as `completion` with `status="FAILED"`.
- `test_container_lifecycle.py`:
  - Validates `DockerClient.is_docker_available` detection.
  - Validates fail-closed behavior: `DockerExecutionBackend.run()` raises `InfrastructureError` when Docker is missing, providing clear remediation advice (`--isolation trusted`).
  - Validates missing runtime image raises `ConfigurationError` advising `docker pull <image>`.
  - Validates container cleanup guarantee: `kill_and_remove` is called in the `finally` block even when unexpected errors occur.
  - Validates `InProcessExecutionBackend` lifecycle runs in trusted mode and marks results as `TRUSTED / NOT ISOLATED`.
  - Validates `recoverybench doctor` reports Docker and sandbox resource limit diagnostics.
  - Validates `recoverybench doctor --require-isolation` exits with code 3 when Docker is unavailable.
- `test_network_isolation.py`:
  - Real container integration test (executed when Docker Engine is running, gracefully skipped when absent):
    - Asserts DNS resolution fails inside `--network=none` container (`socket.gaierror`).
    - Asserts outbound TCP connections fail inside container (`OSError: Network is unreachable`).
    - Asserts outbound HTTP requests fail inside container (`URLError`).
  - Complies with zero-fabrication testing rules: skipped tests are reported truthfully as SKIPPED, never forged as PASSED.
