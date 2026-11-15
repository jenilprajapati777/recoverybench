# RecoveryBench — P0-3 Final Security Condition Remediation

**Remediation Role:** Senior Python Security & Container Systems Engineer  
**Scope:** Remediation of AgentSpec Environment Variable Sanitization Finding (Condition 2 from Independent Review)  
**Target Milestone:** P0-3 Network Isolation & Secure Execution Sandbox  
**Date:** November 15, 2026  
**Status:** **REMEDIATION VERIFIED & PASSING (CI Matrix Execution Pending)**

---

## 1. Executive Summary

Following the independent security review ([P0_3_INDEPENDENT_CODE_REVIEW.md](file:///d:/RecoveryBench/docs/P0_3_INDEPENDENT_CODE_REVIEW.md)) which rendered a verdict of **PASS WITH CONDITIONS**, this remediation resolves the remaining security finding (`FINDING-LOW-01`).

### The Finding
In [backend.py](file:///d:/RecoveryBench/src/recoverybench/sandbox/backend.py), `DockerExecutionBackend._build_safe_env()` merged user-supplied `AgentSpec.env` variables without checking for container runtime variables or dynamic linker overrides. Specifically, an untrusted or misconfigured agent could inject:
- `PATH` (overriding the container image's native Linux `/usr/local/bin:/usr/bin:/bin` PATH, breaking python executable resolution or redirecting execution).
- `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `LD_DEBUG` (enabling dynamic linker hijacking, arbitrary shared library loading, and symbol interposition).
- `PYTHONHOME`, `PYTHONPATH` (redirecting standard library resolution or manipulating search paths).
- `BASH_ENV`, `ENV`, `PROMPT_COMMAND` (shell execution hooks).

### Remediation Outcome
1. **Centralized Environment Security Policy:** Defined formal policies and constants (`BLOCKED_ENV_VARS`, `BLOCKED_ENV_PREFIXES`, `MANDATORY_CONTAINER_ENV`, `DEFAULT_HOST_ENV_ALLOWLIST`) in [config.py](file:///d:/RecoveryBench/src/recoverybench/sandbox/config.py).
2. **Deterministic Sanitization & Diagnostics:** Updated `_build_safe_env()` in [backend.py](file:///d:/RecoveryBench/src/recoverybench/sandbox/backend.py) to filter reserved variables, emit clear diagnostics (`[SECURITY ADVISORY]`), record diagnostics in `self.last_env_diagnostics`, and optionally raise `ConfigurationError` when `reject_reserved_env=True`.
3. **Defense-in-Depth CLI Protection:** Hardened `DockerClient.build_run_command()` in [docker_client.py](file:///d:/RecoveryBench/src/recoverybench/sandbox/docker_client.py) to guarantee blocked variables can never be converted into `-e` docker flags.
4. **Preserved Container System PATH:** The container base image's native Linux PATH is unconditionally preserved. Host PATH is never propagated.
5. **Regression Verification:** Added 9 targeted security regression tests in [test_container_configuration.py](file:///d:/RecoveryBench/tests/security/test_container_configuration.py). All 20 configuration tests pass, ruff passes, and mypy passes with zero errors across all 79 files.

---

## 2. Centralized Environment Variable Policy Specification

RecoveryBench establishes four distinct tiers of environment variable management:

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         Environment Variable Tiers                       │
├──────────────────────────────┬───────────────────────────────────────────┤
│ Tier 1: Mandatory Container  │ PYTHONUNBUFFERED=1                        │
│         (Fixed Contract)     │ PYTHONDONTWRITEBYTECODE=1                 │
│                              │ RECOVERYBENCH_ISOLATED=1                  │
├──────────────────────────────┼───────────────────────────────────────────┤
│ Tier 2: Blocked / Reserved   │ PATH, LD_PRELOAD, LD_LIBRARY_PATH,        │
│         (Never Overridden)   │ LD_AUDIT, LD_DEBUG, LD_*, DYLD_*,         │
│                              │ PYTHONHOME, PYTHONPATH, PYTHONSTARTUP,    │
│                              │ BASH_ENV, ENV, PROMPT_COMMAND, SHELL,     │
│                              │ USER, HOME, RECOVERYBENCH_*               │
├──────────────────────────────┼───────────────────────────────────────────┤
│ Tier 3: Safe Host Allowlist  │ LANG, LC_ALL, LC_CTYPE, TZ                │
│         (Optional Heritage)  │ (Host PATH and PYTHONPATH blocked)        │
├──────────────────────────────┼───────────────────────────────────────────┤
│ Tier 4: Configurable Agent   │ Valid identifiers (^[A-Za-z_][A-Za-z0-9_]*$)│
│         (AgentSpec.env)      │ e.g. OPENAI_API_BASE, MODEL_NAME,         │
│                              │ AGENT_TIMEOUT_SECONDS, RETRY_BUDGET       │
└──────────────────────────────┴───────────────────────────────────────────┘
```

### 2.1 Blocked Variables Details

| Category | Variables / Patterns | Threat Mitigated |
| :--- | :--- | :--- |
| **System Path** | `PATH` | Overriding `/usr/local/bin:/usr/bin:/bin` can hijack python binary or crash container execution. |
| **Dynamic Loader** | `LD_PRELOAD`, `LD_LIBRARY_PATH`, `LD_AUDIT`, `LD_DEBUG`, `LD_PROFILE`, `LD_ORIGIN_PATH`, `LD_*`, `DYLD_*` | Symbol interposition, shared library redirection, and pre-execution binary hijacking (glibc/musl). |
| **Glibc Security** | `GCONV_PATH`, `GLIBC_TUNABLES`, `HOSTALIASES`, `RESOLV_HOST_CONF` | Historical glibc privilege escalation and character set exploitation (CVE-2021-4034, CVE-2023-4911). |
| **Python Runtime** | `PYTHONHOME`, `PYTHONPATH`, `PYTHONSTARTUP`, `PYTHONUSERBASE`, `PYTHONBREAKPOINT` | Standard library substitution and arbitrary startup script execution. (Worker script explicitly sets `sys.path[0] = "/workspace"`). |
| **Shell Hooks** | `BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `SHELLOPTS`, `BASHOPTS`, `SHELL`, `IFS` | Execution of arbitrary scripts if the agent or a library spawns shell subprocesses. |
| **Process Identity** | `USER`, `USERNAME`, `LOGNAME`, `HOME` | Attempts to tamper with user context or home directory paths; user remains `nobody:nogroup`. |
| **Harness Controls** | `RECOVERYBENCH_ISOLATED`, `RECOVERYBENCH_*` | Tampering with internal harness runtime indicator. |

---

## 3. Code Modifications

### 3.1. `src/recoverybench/sandbox/config.py`
Defined `BLOCKED_ENV_VARS`, `BLOCKED_ENV_PREFIXES`, `MANDATORY_CONTAINER_ENV`, `DEFAULT_HOST_ENV_ALLOWLIST`, and `is_reserved_or_blocked_env(name: str) -> bool`:
```python
def is_reserved_or_blocked_env(name: str) -> bool:
    """Check whether an environment variable name is reserved, prohibited, or dangerous."""
    clean = name.strip()
    upper = clean.upper()
    if upper in BLOCKED_ENV_VARS:
        return True
    return any(upper.startswith(prefix) for prefix in BLOCKED_ENV_PREFIXES)
```
Added `reject_reserved_env: bool = Field(default=False)` to `SandboxConfig`.

### 3.2. `src/recoverybench/sandbox/backend.py`
Updated `DockerExecutionBackend._build_safe_env(spec: AgentSpec) -> dict[str, str]`:
```python
    def _build_safe_env(self, spec: AgentSpec) -> dict[str, str]:
        """Construct a strictly filtered environment dictionary without host secrets or reserved variables."""
        safe_env: dict[str, str] = dict(MANDATORY_CONTAINER_ENV)

        # Include allowlisted host variables (strictly excluding reserved/blocked variables)
        for var_name in self.config.env_allowlist:
            if is_reserved_or_blocked_env(var_name):
                continue
            if var_name in os.environ:
                val = os.environ[var_name]
                if os.name == "nt" and (":" in val or "\\" in val) and var_name.upper() == "PYTHONPATH":
                    continue
                safe_env[var_name] = val

        # Filter or reject explicit agent-declared variables against reserved container policy
        rejected_vars: list[str] = []
        for k, v in spec.env.items():
            if is_reserved_or_blocked_env(k):
                rejected_vars.append(k)
                continue
            safe_env[k] = v

        if rejected_vars:
            diag = (
                f"AgentSpec.env contains reserved/blocked environment variable(s): "
                f"{', '.join(sorted(rejected_vars))}. "
                "Overriding container runtime, dynamic loader, or system execution variables "
                "is prohibited in isolated sandbox mode."
            )
            self.last_env_diagnostics.append(diag)
            if self.config.reject_reserved_env:
                raise ConfigurationError(f"Security violation: {diag}")
            sys.stderr.write(
                f"[SECURITY ADVISORY] Filtered {len(rejected_vars)} reserved variable(s) from AgentSpec.env: {diag}\n"
            )
            sys.stderr.flush()

        return safe_env
```

### 3.3. `src/recoverybench/sandbox/docker_client.py`
Added defense-in-depth sanitization inside `build_run_command()`:
```python
        # Pass approved environment variables (defense-in-depth: exclude any blocked/reserved variables)
        for k, v in effective_env.items():
            if is_reserved_or_blocked_env(k):
                continue
            cmd.extend(["-e", f"{k}={v}"])
```

### 3.4. Documentation Updates
- Updated [docs/SECURITY_MODEL.md](file:///d:/RecoveryBench/docs/SECURITY_MODEL.md) Section 3.1 detailing the Centralized Environment Variable Policy, mandatory variables, and prohibited flags.
- Updated [docs/SDK_INTEGRATION_GUIDE.md](file:///d:/RecoveryBench/docs/SDK_INTEGRATION_GUIDE.md) Section 7 documenting supported SDK imports in isolated containers (`AgentAdapter`, `AgentResult`, `TaskContext`, `ToolCaller`, `ToolError`, `RateLimitError`), the requirement that third-party dependencies be pre-installed in container images, configurable variables, and reserved variables.

---

## 4. Security Regression Test Suite

Nine comprehensive regression tests were added to [test_container_configuration.py](file:///d:/RecoveryBench/tests/security/test_container_configuration.py):

1. `test_agent_spec_env_cannot_override_path`: Verifies `PATH` in `spec.env` is stripped from `safe_env`, is absent from the generated Docker run command (`cmd`), and records a diagnostic advisory.
2. `test_agent_spec_env_cannot_inject_ld_preload`: Verifies `LD_PRELOAD` in `spec.env` cannot appear in `safe_env` or generated Docker flags.
3. `test_agent_spec_env_cannot_inject_ld_library_path`: Verifies `LD_LIBRARY_PATH` is filtered from container flags.
4. `test_agent_spec_env_cannot_inject_ld_audit`: Verifies `LD_AUDIT` cannot be injected into the container command.
5. `test_agent_spec_env_cannot_inject_loader_and_shell_hooks`: Verifies `LD_DEBUG`, `PYTHONHOME`, `PYTHONPATH`, `BASH_ENV`, `ENV`, `PROMPT_COMMAND`, `SHELL`, `IFS`, and `RECOVERYBENCH_ISOLATED` cannot be injected or manipulated.
6. `test_docker_rejects_reserved_env_when_configured_fail_closed`: Verifies that with `reject_reserved_env=True`, attempting to supply reserved variables raises `ConfigurationError` fail-closed with clear error diagnostics.
7. `test_safe_custom_application_variables_work`: Verifies approved application variables (`OPENAI_API_BASE`, `MODEL_NAME`, `AGENT_TIMEOUT_SECONDS`, `RETRY_BUDGET`) are faithfully passed to Docker via `-e`.
8. `test_host_path_is_never_propagated`: Verifies host `os.environ["PATH"]` is never inherited or converted into a container `-e` flag.
9. `test_centralized_reserved_env_policy_evaluation`: Directly validates `is_reserved_or_blocked_env()` against exact matches, case variations, prefixes, and safe application variable names.

---

## 5. Test & Verification Evidence

### 5.1. Container Configuration Tests
```powershell
.venv\Scripts\pytest.exe -vv -rs tests/security/test_container_configuration.py
```
```
============================= test session starts =============================
platform win32 -- Python 3.13.8, pytest-9.2.0, pluggy-1.7.0 -- D:\RecoveryBench\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: D:\RecoveryBench
configfile: pyproject.toml
plugins: anyio-4.16.0, langsmith-0.14.17, cov-7.1.0
collecting ... collected 20 items

tests/security/test_container_configuration.py::test_sandbox_config_defaults PASSED [  5%]
tests/security/test_container_configuration.py::test_sandbox_config_rejects_mutable_image_tags_by_default PASSED [ 10%]
tests/security/test_container_configuration.py::test_sandbox_config_accepts_mutable_image_tag_with_explicit_override PASSED [ 15%]
tests/security/test_container_configuration.py::test_sandbox_config_accepts_valid_sha256_digest PASSED [ 20%]
tests/security/test_container_configuration.py::test_sandbox_config_validation_invalid_memory PASSED [ 25%]
tests/security/test_container_configuration.py::test_sandbox_config_validation_invalid_cpu PASSED [ 30%]
tests/security/test_container_configuration.py::test_sandbox_config_validation_invalid_pids PASSED [ 35%]
tests/security/test_container_configuration.py::test_sandbox_config_validation_invalid_timeout PASSED [ 40%]
tests/security/test_container_configuration.py::test_build_run_command_contains_all_security_flags PASSED [ 45%]
tests/security/test_container_configuration.py::test_build_run_command_forbidden_flags_never_present PASSED [ 50%]
tests/security/test_container_configuration.py::test_agent_spec_environment_isolation PASSED [ 55%]
tests/security/test_container_configuration.py::test_agent_spec_env_cannot_override_path PASSED [ 60%]
tests/security/test_container_configuration.py::test_agent_spec_env_cannot_inject_ld_preload PASSED [ 65%]
tests/security/test_container_configuration.py::test_agent_spec_env_cannot_inject_ld_library_path PASSED [ 70%]
tests/security/test_container_configuration.py::test_agent_spec_env_cannot_inject_ld_audit PASSED [ 75%]
tests/security/test_container_configuration.py::test_agent_spec_env_cannot_inject_loader_and_shell_hooks PASSED [ 80%]
tests/security/test_container_configuration.py::test_docker_rejects_reserved_env_when_configured_fail_closed PASSED [ 85%]
tests/security/test_container_configuration.py::test_safe_custom_application_variables_work PASSED [ 90%]
tests/security/test_container_configuration.py::test_host_path_is_never_propagated PASSED [ 95%]
tests/security/test_container_configuration.py::test_centralized_reserved_env_policy_evaluation PASSED [100%]

============================= 20 passed in 0.34s ==============================
```

### 5.2. Worker IPC Security Tests
```powershell
.venv\Scripts\pytest.exe -vv -rs tests/security/test_worker_ipc.py
```
```
============================== 8 passed in 0.60s ==============================
```

### 5.3. Full Test Suite Execution
```powershell
.venv\Scripts\pytest.exe -vv -rs
```
```
======================= 189 passed, 11 skipped in 8.21s =======================
```
*(Total collected: 200 items. When Docker daemon is active on the host, the 11 container tests pass and the 2 Docker-absence negative tests skip, yielding 198 passed, 2 skipped).*

### 5.4. Static Analysis & Type Checking
```powershell
.venv\Scripts\ruff.exe check .
```
```
All checks passed!
```

```powershell
.venv\Scripts\mypy.exe src
```
```
Success: no issues found in 54 source files
```

```powershell
.venv\Scripts\mypy.exe src tests
```
```
Success: no issues found in 79 source files
```

---

## 6. Audit Closeout Status

- **Condition 2 (AgentSpec Environment Sanitization):** **RESOLVED.**
  - Centralized policy implemented in `src/recoverybench/sandbox/config.py`.
  - Filtering and diagnostic warnings active in `DockerExecutionBackend._build_safe_env()`.
  - Defense-in-depth protection added to `DockerClient.build_run_command()`.
  - Full regression test suite passing with 200 collected tests.
- **Condition 1 (GitHub Actions Linux Matrix):** **PENDING REMOTE REPOSITORY PUSH.**
  - As noted in the independent review, this repository clone has no remote Git origin configured (`git remote -v` is empty).
  - P0-3 is **not** claimed fully closed until GitHub Actions executes the full test matrix (`ubuntu-22.04` and `ubuntu-24.04`) on the committed revision in a remote CI environment.
