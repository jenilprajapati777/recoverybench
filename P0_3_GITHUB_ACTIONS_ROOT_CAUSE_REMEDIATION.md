# RecoveryBench — Root-Cause Repair for GitHub Actions Run #3

**Date:** 2026-11-16  
**Auditor / Engineer:** Senior Python Security & CI/CD Systems Engineer  
**Repository:** `https://github.com/jenilprajapati777/recoverybench`  
**Branch:** `fix/p0-audit-remediation`  
**Failing Commit Analyzed:** `ff89bcb900a85413ae8d90ef42bd4c9b9f0f8f2f`  
**Status:** **REPAIRED & VERIFIED LOCALLY — READY FOR REMOTE PUSH**

---

## 1. Confirmed Root Cause

In GitHub Actions Run #3, the CI matrix failed across all six Linux/Python runner combinations (Ubuntu 22.04 & 24.04 across Python 3.11, 3.12, and 3.13) at:
```text
tests/security/test_filesystem_isolation.py::test_normalize_staged_permissions_exception_cleans_up
```

### Stack Trace Analysis
```text
TypeError: failing_chmod() got an unexpected keyword argument 'follow_symlinks'
```

### Mechanism of Failure
1. The previous test implementation installed a global monkeypatch on `os.chmod`:
   ```python
   def failing_chmod(path: Any, mode: int) -> None:
       raise OSError("Permission denied on chmod")
   monkeypatch.setattr(os, "chmod", failing_chmod)
   ```
2. When `WorkspaceStaging.stage_workspace(source_dir)` was invoked, it traversed `source_dir` and copied files via `_copy_safe_file()` -> `shutil.copy2()`.
3. In Python's standard library on POSIX/Linux platforms, `shutil.copy2()` invokes `shutil.copystat(src, dst, follow_symlinks=...)`, which calls:
   ```python
   os.chmod(dst, mode, follow_symlinks=follow_symlinks)
   ```
4. Because `failing_chmod` was mocked globally before `shutil.copy2()` ran, and because the mock signature only accepted `(path: Any, mode: int)`, Python raised `TypeError: failing_chmod() got an unexpected keyword argument 'follow_symlinks'` during the **initial copy phase**, before `normalize_staged_permissions` was ever invoked.
5. On Windows, `shutil.copystat()` does not invoke `os.chmod` with `follow_symlinks=True`, so the test passed on the local Windows development machine but failed deterministically across all Linux GitHub Actions runners.

---

## 2. Corrected Regression Test Behavior

File modified: [`tests/security/test_filesystem_isolation.py`](file:///d:/RecoveryBench/tests/security/test_filesystem_isolation.py)

The regression test was refactored into two dedicated, deterministic tests:

### 1. Staging Cleanup on Normalization Failure (`test_normalize_staged_permissions_exception_cleans_up`)
- Allows `shutil.copy2()` and file staging to complete naturally without global `os.chmod` interference.
- Monkeypatches `WorkspaceStaging.normalize_staged_permissions` specifically so that:
  - The staging path is captured in `captured_staged_dirs`.
  - The test asserts that the temporary staging directory and staged files (`staged / "agent.py"`) actually exist on disk prior to the failure.
  - A `ConfigurationError` is raised from the normalization phase.
- `WorkspaceStaging.stage_workspace()` catches the exception in its `try ... except Exception` block, invokes `shutil.rmtree(target_temp, ignore_errors=True)`, and re-raises.
- The test asserts:
  - `ConfigurationError` was raised with the expected diagnostic message.
  - The captured staging directory **no longer exists on disk** (`assert not staged_path.exists()`), verifying guaranteed cleanup.
  - The original source file in `source_dir` **still exists and is completely unchanged** in both content and filesystem mode.

### 2. Direct Normalization `OSError` Wrapping (`test_normalize_staged_permissions_wraps_chmod_os_error`)
- Tests `WorkspaceStaging.normalize_staged_permissions()` directly on a pre-existing temporary directory without touching `stage_workspace()`.
- Implements `failing_chmod(path: Any, mode: int, *args: Any, **kwargs: Any)` accepting all POSIX keyword arguments (`follow_symlinks=True`).
- Verifies that any underlying `OSError` during `os.chmod` is safely wrapped and raised as `ConfigurationError("Failed to normalize staging root permissions: ...")`.

---

## 3. Audit of Production Cleanup and Permission Invariants

Review of [`src/recoverybench/sandbox/staging.py`](file:///d:/RecoveryBench/src/recoverybench/sandbox/staging.py):

| Security Invariant | Verification Result |
|---|---|
| **1. Non-Root Traversal** | Staging root and all subdirectories are normalized to `0o755` (`drwxr-xr-x`), allowing container user `nobody:nogroup` (UID 65534) to traverse and list. |
| **2. Non-Root Read** | Regular files are normalized to `0o644` (`-rw-r--r--`), allowing `nobody:nogroup` to read all agent modules and `_recoverybench_worker.py`. |
| **3. No Group/Other Write** | Write permission is granted exclusively to the owner (`0o200` mask). Group and other write bits are strictly `0`. Mount is `:ro`. |
| **4. Symlink Protection** | Staging root cannot be a symlink (`raw_path.is_symlink()` check). `os.walk(followlinks=False)` skips directory and file symlinks. Symlinks are never followed during chmod. |
| **5. Original Workspace Protection** | `normalize_staged_permissions()` operates strictly on `target_temp` (`rb_sandbox_...`). Original workspace files in `source_dir` are never chmod'd. |
| **6. Cleanup on Exception** | In `stage_workspace()`, any exception in file copying, worker injection, or normalization triggers `shutil.rmtree(target_temp, ignore_errors=True)` and re-raises. |
| **7. No Silent Success** | Cleanup failures in `except Exception:` never mask or convert an unsuccessful staging operation into success. |
| **8. Allowlist & Content Checks** | Default-deny extensions, credential patterns, secret-scanning regexes, and symlink escape checks remain intact. |

---

## 4. Audit of Live Docker Integration Test Step

Review of [`.github/workflows/recoverybench-ci.yml`](file:///d:/RecoveryBench/.github/workflows/recoverybench-ci.yml):

### Why Was the Step Marked "Skipped" in Run #3?
- Step 12 (`Run Security & Sandbox Configuration Suites`) failed due to the `TypeError` in `test_normalize_staged_permissions_exception_cleans_up`.
- In GitHub Actions, when a step fails with a non-zero exit code, all subsequent steps lacking `if: always()` are skipped by the runner.
- The dedicated live network isolation step was **not** skipped due to pytest markers, network connectivity, or missing Docker; it was skipped because Step 12 failed.

### Hardening Applied:
In `.github/workflows/recoverybench-ci.yml`:
```yaml
      - name: Run Live Docker Network Isolation Integration Tests
        run: |
          docker info
          pytest -v -rs tests/security/test_network_isolation.py
```
- Added `docker info` before the pytest execution. If the Docker daemon is absent or unresponsive, the step fails closed immediately at `docker info` with exit code 1.
- `pytest -v -rs` provides detailed logging and prints the exact reason for any skipped test.
- When Docker is running, all 14 network isolation tests execute and pass without skips.

---

## 5. Full Verification Results

Local verification executed with active Docker Engine:

```powershell
pytest -vv tests/security/test_filesystem_isolation.py::test_normalize_staged_permissions_exception_cleans_up
pytest -vv tests/security/test_filesystem_isolation.py
pytest -vv tests/security/test_container_configuration.py
pytest -vv tests/security/test_worker_ipc.py
pytest -vv tests/security/test_container_lifecycle.py
pytest -vv -rs tests/security/test_network_isolation.py
pytest -v
ruff check .
mypy src tests
```

### Test Outcomes

| Suite / Check | Command | Count | Outcome |
|---|---|---|---|
| Focused Test | `test_normalize_staged_permissions_exception_cleans_up` | 1 | **1 passed** in 0.52s |
| Filesystem Isolation | `tests/security/test_filesystem_isolation.py` | 24 | **24 passed** in 10.71s |
| Container Configuration | `tests/security/test_container_configuration.py` | 20 | **20 passed** in 0.20s |
| Worker IPC | `tests/security/test_worker_ipc.py` | 8 | **8 passed** in 0.54s |
| Container Lifecycle | `tests/security/test_container_lifecycle.py` | 7 | **6 passed, 1 skipped** (Docker available) |
| Live Network Isolation | `tests/security/test_network_isolation.py` | 14 | **14 passed** in 15.04s |
| **Complete Test Suite** | `pytest -v` | **207** | **205 passed, 2 skipped** in 24.93s |
| **Ruff Linter** | `ruff check .` | 133 files | **All checks passed!** |
| **Mypy Type Analysis** | `mypy src tests` | 79 files | **Success: no issues found** |

### Skipped Tests (2 Expected):
1. `tests/security/test_container_lifecycle.py:125`: `Docker is available on this host; testing require-isolation failure requires Docker to be absent`.
2. `tests/test_cli.py:160`: `Docker is available on this host; testing fail-closed requires Docker to be unavailable`.

Both skipped tests are explicit fail-closed tests designed to verify CLI failure when Docker is completely missing from the system.

---

## 6. Exact Files Changed

1. [`.github/workflows/recoverybench-ci.yml`](file:///d:/RecoveryBench/.github/workflows/recoverybench-ci.yml#L121-L125) — Added `docker info` to live network isolation step for fail-closed validation.
2. [`tests/security/test_filesystem_isolation.py`](file:///d:/RecoveryBench/tests/security/test_filesystem_isolation.py#L470-L520) — Refactored normalization exception test to monkeypatch `normalize_staged_permissions` and added `test_normalize_staged_permissions_wraps_chmod_os_error`.
3. [`P0_3_GITHUB_ACTIONS_ROOT_CAUSE_REMEDIATION.md`](file:///d:/RecoveryBench/P0_3_GITHUB_ACTIONS_ROOT_CAUSE_REMEDIATION.md) — Comprehensive root-cause investigation and verification deliverable.

---

## 7. Remaining Risks & Considerations

- **GitHub Actions Execution:** The fix addresses the exact POSIX `copystat` keyword signature discrepancy that caused Run #3 to fail. Once pushed, the 6-job matrix on GitHub Actions should execute end-to-end green, including the live network-isolation step.
- **Docker Daemon Dependency:** GitHub-hosted Ubuntu runners have Docker Engine pre-installed and running by default. The fail-closed checks (`recoverybench doctor --require-isolation` and `docker info`) guarantee execution integrity.

---

## 8. Commit & Push Instructions

The changes have been committed on branch `fix/p0-audit-remediation`.

To push and trigger GitHub Actions Run #4:

```bash
git push origin fix/p0-audit-remediation
```
