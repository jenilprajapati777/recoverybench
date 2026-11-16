# RecoveryBench Phase 3B.1 — SDK Contract Harmonization Report

**Repository:** `https://github.com/jenilprajapati777/recoverybench`  
**Working Directory:** `D:\RecoveryBench`  
**Branch:** `phase-3/public-contracts` (branched from `main` at `v0.1.0`)  
**Date:** 2026-11-16  
**Auditor / Engineer:** Senior Python Security & SDK Infrastructure Engineer  

---

## 1. Executive Summary

Phase 3B.1 addresses the critical public SDK contract discrepancies, isolation defaults, and silent fallbacks identified in the Phase 3A audit. All changes strictly preserve the released `v0.1.0` behavior for valid code while eliminating latent security hazards (such as invalid isolation modes silently running in trusted in-process mode), adding first-class `AgentSpec` support to `RecoveryBenchTester`, and bringing full parity between implementation and documentation.

### Core Achievements
1. **`RecoveryBenchTester` Isolation Support:** Added `isolation: str = "trusted"` and `backend: ExecutionBackend | None = None` to `RecoveryBenchTester.__init__`, correctly passing the active backend to `ExecutionEngine`.
2. **First-Class `AgentSpec` Support:** Updated `RecoveryBenchTester.test()` to accept `AgentAdapter | AgentSpec | Callable[..., Any]`, passing `AgentSpec` directly to `ExecutionEngine.run()` without improper wrapping by `wrap_agent()`.
3. **Strict, Security-Sensitive Isolation Validation:** Implemented `resolve_backend()` with case/whitespace normalization across all documented aliases (`isolated`, `docker`, `sandbox`, `trusted`, `in-process`, `inprocess`, `local`). Unrecognized isolation strings immediately raise `ConfigurationError` and **never** fall back silently to trusted execution.
4. **Backend Precedence Rules:** Defined and documented that explicit `backend` instances take precedence over the `isolation` string while validating `isolation` for correctness.
5. **Strict Ledger Type Validation:** Implemented `resolve_ledger()`. Any value other than `"memory"` or `"sqlite"` raises `ConfigurationError`.
6. **Documentation Synchronization:** Updated `docs/SDK_INTEGRATION_GUIDE.md` to accurately document the trusted default for Python SDK entry points, explicit container isolation syntax, constructor arguments, and fail-closed behaviors.
7. **Comprehensive Test Suite & Quality Verification:** Added 12 focused regression test cases in `tests/test_sdk_high_level_api.py`. Passed all 210 pytest tests, clean Ruff linting, clean Mypy static type checking across 79 source files, and 100% clean distribution smoke test in an isolated virtual environment.

---

## 2. Exact Files Changed

| File Path | Nature of Change | Description |
| :--- | :--- | :--- |
| `src/recoverybench/sdk/tester.py` | Modified | Added `resolve_backend()` and `resolve_ledger()`; updated `test_agent()` and `RecoveryBenchTester` constructor and `test()` method signatures; added strict validation and backend wiring. |
| `src/recoverybench/sdk/__init__.py` | Modified | Exported `resolve_backend` and `resolve_ledger` in the SDK namespace and `__all__`. |
| `src/recoverybench/__init__.py` | Modified | Exported `resolve_backend` and `resolve_ledger` in the top-level package namespace and `__all__`. |
| `src/recoverybench/sandbox/backend.py` | Modified | Updated `InProcessExecutionBackend._load_agent_from_spec` to instantiate `AgentAdapter` subclasses when specified as entrypoints, matching `worker.py` container behavior. |
| `tests/test_sdk_high_level_api.py` | Modified | Added 12 focused regression test cases covering trusted/isolated defaults, alias mapping, invalid value rejections, `AgentSpec` support, backend precedence, and fail-closed guarantees. |
| `docs/SDK_INTEGRATION_GUIDE.md` | Modified | Updated SDK reference signatures, documented constructor options for `RecoveryBenchTester`, clarified `test_agent()` trusted default, and documented isolation precedence and validation rules. |

---

## 3. Before and After Public SDK Signatures

### 3.1 `RecoveryBenchTester.__init__`

```python
# BEFORE (Phase 3A baseline)
class RecoveryBenchTester:
    def __init__(
        self,
        *,
        ledger_type: str = "memory",
        db_path: str | Path | None = None,
        custom_tools: ToolRegistry | None = None,
        default_seed: int = 42,
    ) -> None: ...

# AFTER (Phase 3B.1 Harmonized)
class RecoveryBenchTester:
    def __init__(
        self,
        *,
        isolation: str = "trusted",
        backend: ExecutionBackend | None = None,
        ledger_type: str = "memory",
        db_path: str | Path | None = None,
        custom_tools: ToolRegistry | None = None,
        default_seed: int = 42,
    ) -> None: ...
```

### 3.2 `RecoveryBenchTester.test()`

```python
# BEFORE (Phase 3A baseline - crashed if passed AgentSpec due to wrap_agent())
    def test(
        self,
        scenario: str | Path | ScenarioModel,
        agent: AgentAdapter | Callable[..., Any],
        *,
        run_id: str | None = None,
        seed: int | None = None,
    ) -> RunResult:
        resolved_agent = agent if isinstance(agent, AgentAdapter) else wrap_agent(agent)
        return self.engine.run(model, resolved_agent, run_id=run_id)

# AFTER (Phase 3B.1 Harmonized - passes AgentSpec directly to ExecutionEngine)
    def test(
        self,
        scenario: str | Path | ScenarioModel,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        *,
        run_id: str | None = None,
        seed: int | None = None,
    ) -> RunResult:
        model = resolve_scenario(scenario)
        target_seed = seed if seed is not None else self.default_seed
        model = model.model_copy(update={"seed": target_seed})

        return self.engine.run(model, agent, run_id=run_id, backend=self.backend)
```

### 3.3 `test_agent()`

```python
# Preserves signature while incorporating strict validation helper functions
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
) -> RunResult: ...
```

### 3.4 New Exported Public Resolution Helpers

```python
def resolve_backend(
    isolation: str = "trusted",
    backend: ExecutionBackend | None = None,
) -> ExecutionBackend: ...

def resolve_ledger(
    ledger_type: str = "memory",
    db_path: str | Path | None = None,
) -> Ledger: ...
```

---

## 4. Backend-Selection, Precedence, and Isolation Validation

### 4.1 Strict Isolation Aliases

Leading and trailing whitespace is stripped, and values are converted to lowercase:

- **Isolated Aliases:** `"isolated"`, `"docker"`, `"sandbox"` -> Maps to `DockerExecutionBackend()`
- **Trusted Aliases:** `"trusted"`, `"in-process"`, `"inprocess"`, `"local"` -> Maps to `InProcessExecutionBackend()`

### 4.2 Security-Sensitive Validation Contract
- **No Silent Fallback:** Previously, `test_agent()` evaluated `if clean_iso in ("isolated", "docker", "sandbox"): ... else: active_backend = InProcessExecutionBackend()`. Any invalid value (e.g. `isolation="secure"`, `"container"`, `"remote"`) silently defaulted to in-process execution.
- **Enforcement:** In Phase 3B.1, any unsupported string raises `ConfigurationError` immediately:
  ```python
  ConfigurationError: Invalid isolation mode 'remote'. Supported modes: ['docker', 'in-process', 'inprocess', 'isolated', 'local', 'sandbox', 'trusted'].
  ```

### 4.3 Explicit Precedence Rules
When both `backend` and `isolation` are supplied:
1. `backend` must be an instance of `ExecutionBackend`; otherwise, a `ConfigurationError` is raised.
2. `isolation` is validated against recognized aliases to catch configuration typos.
3. If both are valid, `backend` takes precedence and is passed directly to `ExecutionEngine`.

---

## 5. AgentSpec Execution Behavior

1. **`RecoveryBenchTester.test()`:**
   - Accepts `AgentSpec` directly without wrapping via `wrap_agent()`.
   - Passes `AgentSpec` to `ExecutionEngine.run()`, which dispatches to `backend.run(spec, ...)`.
2. **In-Process (`InProcessExecutionBackend`):**
   - In Phase 3A, `InProcessExecutionBackend._load_agent_from_spec` failed if `entrypoint` was an `AgentAdapter` class rather than an instantiated object.
   - Updated to:
     ```python
     if isinstance(entrypoint, AgentAdapter):
         return entrypoint
     if isinstance(entrypoint, type) and issubclass(entrypoint, AgentAdapter):
         try:
             return entrypoint()
         except Exception as exc:
             raise ConfigurationError(f"Failed to instantiate AgentAdapter class '{spec.entrypoint}': {exc}") from exc
     if callable(entrypoint):
         return wrap_agent(entrypoint, agent_id=spec.name)
     ```
   - Matches container worker behavior (`src/recoverybench/sandbox/worker.py`), allowing declarative specs to run cleanly in trusted local tests.
3. **Container-Isolated (`DockerExecutionBackend`):**
   - Passes `spec` to the Docker staging pipeline, stages the workspace, and runs inside a container with `--network=none`.
   - If Docker is unavailable or the runtime image is missing, it fails closed with `InfrastructureError` or `ConfigurationError`.

---

## 6. Ledger Type Validation

Both `test_agent()` and `RecoveryBenchTester()` now call `resolve_ledger()`:
- `"memory"`, `"  MEMORY "` -> Returns `InMemoryLedger()`
- `"sqlite"`, `" SQLite "` -> Returns `SQLiteLedger(path or ":memory:")`
- Any other string (e.g. `"postgres"`, `"redis"`, `"unknown"`) -> Raises `ConfigurationError(f"Invalid ledger_type '{ledger_type}'. Supported ledger types: 'memory', 'sqlite'.")`.

---

## 7. Documentation Corrections

Updated [`docs/SDK_INTEGRATION_GUIDE.md`](file:///d:/RecoveryBench/docs/SDK_INTEGRATION_GUIDE.md):
- Corrected Section 4.1 (`test_agent` signature) to include `backend: ExecutionBackend | None = None` and `isolation: str = "trusted"`.
- Corrected Section 4.3 (`RecoveryBenchTester`) to document `isolation`, `backend`, `ledger_type`, `db_path`, `custom_tools`, `default_seed`.
- Corrected Section 6.2 and 6.3 to explain that `test_agent()` and `RecoveryBenchTester()` default to `isolation="trusted"` for rapid local pytest runs without Docker, and that `isolation="isolated"` explicitly enforces container network isolation.
- Added Section 6.4 documenting the Isolation Modes, Aliases, and Precedence Contract table.
- Removed examples that promised unsupported constructor arguments or silent defaults.

---

## 8. Regression Tests Added (`tests/test_sdk_high_level_api.py`)

Added 12 targeted regression tests:

1. `test_test_agent_trusted_default`: Proves `test_agent()` defaults to in-process trusted execution (`result.isolation_mode == "trusted"`).
2. `test_test_agent_isolated_selection`: Proves `test_agent(isolation="isolated")` instantiates and calls `DockerExecutionBackend`.
3. `test_invalid_isolation_raises_configuration_error`: Proves invalid isolation strings in `test_agent()` and `RecoveryBenchTester` raise `ConfigurationError` and never fall back silently.
4. `test_documented_isolation_aliases_mapping`: Proves all 7 aliases (`isolated`, `docker`, `sandbox`, `trusted`, `in-process`, `inprocess`, `local`) map to the expected backends with whitespace and case normalization.
5. `test_recoverybench_tester_trusted_configuration`: Proves `RecoveryBenchTester()` defaults to and supports trusted in-process execution.
6. `test_recoverybench_tester_isolated_configuration`: Proves `RecoveryBenchTester(isolation="isolated")` instantiates `DockerExecutionBackend`.
7. `test_recoverybench_tester_accepts_agent_spec`: Proves `RecoveryBenchTester.test()` accepts `AgentSpec` without wrapping and runs successfully.
8. `test_recoverybench_tester_accepts_ordinary_callables`: Proves generic Python functions continue running cleanly.
9. `test_recoverybench_tester_accepts_agent_adapter_implementations`: Proves `AgentAdapter` subclasses continue running cleanly.
10. `test_invalid_ledger_type_raises_configuration_error`: Proves invalid ledger strings raise `ConfigurationError` across both entry points.
11. `test_existing_sdk_behavior_and_backend_precedence`: Proves explicit `backend` instance takes precedence over `isolation`, and invalid backend types raise `ConfigurationError`.
12. `test_docker_unavailable_isolated_execution_fails_closed`: Proves isolated execution fails closed with `InfrastructureError` when Docker is unavailable across backend, `test_agent()`, and `RecoveryBenchTester()`.

---

## 9. Quality & Test Results

### 9.1 Pytest Test Suite
```
collected 222 items
====================== 210 passed, 12 skipped in 32.01s =======================
```
*Note: The 12 skipped tests are Docker-daemon-dependent integration tests that gracefully skip when Docker Engine is not running on the local host.*

### 9.2 Ruff Linter
```
$ .venv\Scripts\ruff check .
All checks passed!
```

### 9.3 Mypy Static Type Checker
```
$ .venv\Scripts\mypy src/ tests/
Success: no issues found in 79 source files
```

### 9.4 Distribution Smoke Test
Executed via `scripts/distribution_smoke_test.py`:
```
=== RecoveryBench Distribution Smoke Test ===
Repository Root: D:\RecoveryBench

--- Step 1: Building Wheel and Source Distribution ---
Found Wheel: recoverybench-0.1.0-py3-none-any.whl
Found Sdist: recoverybench-0.1.0.tar.gz

--- Step 2: Inspecting Wheel Package Contents & Metadata ---
✓ All 11 critical modules present in wheel.
✓ License file present in wheel: recoverybench-0.1.0.dist-info/licenses/LICENSE
✓ Wheel METADATA verified (PEP 639 license and PyPI classifiers present).

--- Step 3: Creating Isolated Virtual Environment Outside Repo ---
--- Step 4: Installing Core Wheel into Clean Environment ---
✓ Installed recoverybench console script verified at .../recoverybench.exe

--- Step 5: Testing Installed Console Script and Commands Outside Repo ---
✓ 'recoverybench --version' passed.
✓ 'recoverybench --help' passed.
✓ 'recoverybench list-scenarios' passed.
✓ 'recoverybench list-scenarios --json' passed.

--- Step 6: Executing Out-of-the-Box Demo in Installed Environment ---
✓ 'recoverybench demo' executed successfully.
✓ All 4 demo reports successfully generated.

--- Step 7: Verifying Core Package Without LangGraph Extra ---
✓ Confirmed langgraph is absent in core install and core functions independently.

--- Step 8: Installing and Verifying Optional LangGraph Extra ---
✓ LangGraph extra installed and LangGraphAdapter successfully imported.

=================================================================
  ALL DISTRIBUTION SMOKE TESTS PASSED SUCCESSFULLY!
=================================================================
```

---

## 10. Scope Boundaries Preserved

The following boundaries were strictly respected:
- Core effect ledger schema and append-only invariants were untouched.
- Transaction identity and multi-operation evaluator rules were untouched.
- Fault injector logic (6 fault types) was untouched.
- Execution engine evaluation verdict rules were untouched.
- Package version remains `0.1.0` in `pyproject.toml`.
- No scenario schema or JSON report format changes were introduced.
- No git tags or releases were created, and no commits were pushed to remotes.
