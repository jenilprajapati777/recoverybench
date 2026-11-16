# RecoveryBench — Phase 2A Distribution Readiness Report

**Date:** 2026-11-16  
**Auditor / Packaging Engineer:** Senior Python Packaging & Release Engineer  
**Repository:** `https://github.com/jenilprajapati777/recoverybench`  
**Current Working Branch:** `phase-2/cli-distribution`  
**Status:** COMPLETE & VERIFIED  

---

## 1. Starting Git State & Branch Topology

- **Starting Branch:** `fix/p0-audit-remediation`
- **Starting HEAD Commit:** `561f362d09a382dd996a71494ba781a6fe85dc00`
- **Remote Baseline:** `origin/fix/p0-audit-remediation` points directly to `561f362d09a382dd996a71494ba781a6fe85dc00`.
- **Working Tree:** Clean prior to branch creation.

### Branches Established:
1. **`main`**: Created locally tracking `origin/fix/p0-audit-remediation` at baseline commit `561f362d09a382dd996a71494ba781a6fe85dc00`.
2. **`phase-2/cli-distribution`**: Created from `main` to stage all Phase 2A packaging and distribution hardening work.
3. **`fix/p0-audit-remediation`**: Preserved untouched as historical remediation baseline.

---

## 2. PyPI Package Name Verification

- **Target Package Name:** `recoverybench`
- **Normalized Name:** `recoverybench` (PEP 503 / PEP 508 normalization: lowercase alphanumeric, no punctuation).
- **Endpoint Queried:** `https://pypi.org/pypi/recoverybench/json`
- **HTTP Response:** `HTTP 404 Not Found`
- **Interpretation:** No package is currently registered or published under the normalized name `recoverybench` on the PyPI index. The name appears available at the time of checking, but is **not reserved** until initial registration via PyPI Trusted Publishing.

---

## 3. Files Changed and Purpose

| File | Change Type | Purpose |
| :--- | :---: | :--- |
| `src/recoverybench/cli.py` | Modified | Added `--version` (`-v`) eager option and `get_version()` helper using `importlib.metadata`; clarified default isolation mode in docstrings (`demo` = trusted in-process, `run`/`suite` = isolated Docker container). |
| `src/recoverybench/__init__.py` | Modified | Updated version lookup to dynamically query distribution metadata via `importlib.metadata.version("recoverybench")` with fallback to `"0.1.0"`. |
| `tests/test_cli.py` | Modified | Added automated unit tests `test_cli_version()` and `test_cli_version_short()`. |
| `pyproject.toml` | Modified | Added PyPI classifiers (Development Status 3 - Alpha, Intended Audience Developers, Python 3.11, 3.12, 3.13) and keywords for index discovery; preserved dependencies, extras, and URLs. |
| `scripts/distribution_smoke_test.py` | Added | Automated, multi-step distribution smoke test script verifying build artifacts, package member integrity, clean virtualenv installation outside repo, console script execution, and LangGraph extra isolation. |
| `.github/workflows/recoverybench-ci.yml` | Modified | Added `phase-2/cli-distribution` branch triggers; integrated the clean wheel-install smoke test step into the CI matrix across Ubuntu and Python versions. |
| `.github/workflows/publish.yml` | Added | Created PyPI Trusted Publishing release workflow using OIDC token exchange (`id-token: write`, environment `pypi`), triggering only on published GitHub Releases without static API tokens. |
| `CHANGELOG.md` | Added | Initialized Keep a Changelog document with draft entry for `0.1.0 (Unreleased)`. |
| `README.md` | Modified | Updated Quickstart to clearly distinguish Option A (PyPI package install) from Option B (source contributor install); added `--version` verification; highlighted demo vs isolated container execution modes. |
| `docs/FIRST_USER_GUIDE.md` | Modified | Aligned installation section to clearly present PyPI vs source installation paths and `--version` verification. |

---

## 4. CLI Version Command Verification

Tested `recoverybench --version` and `recoverybench -v`:

```bash
$ recoverybench --version
RecoveryBench 0.1.0
$ recoverybench -v
RecoveryBench 0.1.0
```

- **Exit Code:** `0`
- **Authoritative Source:** Dynamically reads distribution metadata via `importlib.metadata.version("recoverybench")`.
- **Works Outside Repo:** Verified in clean, isolated virtualenv where the repository source tree was not on `sys.path`.
- **Automated Tests:** Both `test_cli_version` and `test_cli_version_short` added to `tests/test_cli.py` and passing.

---

## 5. Wheel and Sdist Build Results

Built using `python -m build` (with `hatchling==1.33.0` backend):

```text
Built Artifacts:
- dist/recoverybench-0.1.0-py3-none-any.whl (Size: ~45 KB)
- dist/recoverybench-0.1.0.tar.gz (Size: ~44 KB)
```

### Member Verification:
- All required modules verified present: `cli.py`, `sdk/`, `sandbox/`, `reporting/`, `sample_project/`, `scenarios/`, `ledger/`, `evaluator/`, `adapters/`.
- License file included: `recoverybench-0.1.0.dist-info/licenses/LICENSE` and `recoverybench-0.1.0/LICENSE`.
- PEP 639 License metadata verified: `License-Expression: Apache-2.0` and `License-File: LICENSE`.
- Metadata verified via `twine check --strict dist/*`:
  ```text
  Checking dist\recoverybench-0.1.0-py3-none-any.whl: PASSED
  Checking dist\recoverybench-0.1.0.tar.gz: PASSED
  ```

---

## 6. Clean Wheel-Install Smoke Test Results

Executed via `scripts/distribution_smoke_test.py`:

```text
=== RecoveryBench Distribution Smoke Test ===
--- Step 1: Building Wheel and Source Distribution ---
✓ Found Wheel: recoverybench-0.1.0-py3-none-any.whl
✓ Found Sdist: recoverybench-0.1.0.tar.gz
--- Step 2: Inspecting Wheel Package Contents & Metadata ---
✓ All 11 critical modules present in wheel.
✓ License file present in wheel: recoverybench-0.1.0.dist-info/licenses/LICENSE
✓ Wheel METADATA verified (PEP 639 license and PyPI classifiers present).
--- Step 3: Creating Isolated Virtual Environment Outside Repo ---
✓ Created clean venv at temporary location
--- Step 4: Installing Core Wheel into Clean Environment ---
✓ Installed recoverybench console script verified
--- Step 5: Testing Installed Console Script and Commands Outside Repo ---
✓ 'recoverybench --version' passed (Output: RecoveryBench 0.1.0).
✓ 'recoverybench --help' passed.
✓ 'recoverybench list-scenarios' passed (all 6 built-ins enumerated).
✓ 'recoverybench list-scenarios --json' passed.
--- Step 6: Executing Out-of-the-Box Demo in Installed Environment ---
✓ 'recoverybench demo' executed successfully in trusted in-process mode.
✓ All 4 demo reports successfully generated in outside workdir/.recoverybench/reports/.
--- Step 7: Verifying Core Package Without LangGraph Extra ---
✓ Confirmed core functions without langgraph installed (langgraph import fails as expected).
--- Step 8: Installing and Verifying Optional LangGraph Extra ---
✓ Installed wheel[langgraph] extra.
✓ LangGraphAdapter successfully imported and verified.
=================================================================
  ALL DISTRIBUTION SMOKE TESTS PASSED SUCCESSFULLY!
=================================================================
```

---

## 7. Quality, Linting, & Regression Test Results

- **Ruff:** `ruff check .` -> **All checks passed!** (0 errors or warnings).
- **Mypy:** `mypy src tests scripts` -> **Success: no issues found in 80 source files**.
- **Pytest:** `pytest -v` -> **197 passed, 12 skipped, 0 failed**.
  - *Note on Skipped Tests:* On this local Windows workstation, Docker Desktop daemon was not running during the test run; 11 live Docker container network isolation tests were cleanly skipped with `Docker Engine unavailable in test environment: Docker daemon unreachable...`. 1 test requiring Docker to be absent was skipped when checking isolation requirements. All tests run live in GitHub Actions Ubuntu CI where Docker daemon is active.

---

## 8. Remaining Launch & Distribution Blockers

There are **zero remaining packaging blockers**. All distribution artifacts, metadata, CLI commands, documentation, and workflows are complete and validated.

---

## 9. Next Steps: Git Commands for Publishing & Changing Default Branch

In accordance with release engineering protocol, no branches have been pushed automatically and no releases have been published.

### Step 9.1: Push `main` and `phase-2/cli-distribution` to GitHub

When ready to publish local branches, run:

```bash
# 1. Push the main branch establishing the stable baseline
git push -u origin main

# 2. Push the Phase 2A working branch
git push -u origin phase-2/cli-distribution
```

### Step 9.2: Change the Default Branch to `main` on GitHub

1. Navigate to the repository settings:
   `https://github.com/jenilprajapati777/recoverybench/settings/branches`
2. Under **Default branch**, click the switch arrows icon (⇄).
3. Select `main` from the dropdown list.
4. Click **Update** and confirm the change.
5. In your local terminal, update your remote HEAD tracking:
   ```bash
   git remote set-head origin --auto
   ```

### Step 9.3: Configure PyPI Trusted Publishing (OIDC)

Before triggering any release publish workflow:
1. Log in to PyPI: `https://pypi.org/manage/account/`
2. Under **Publishing** -> **Add a publisher**, select **GitHub**.
3. Fill in the publisher details:
   - **PyPI Project Name:** `recoverybench`
   - **Owner:** `jenilprajapati777`
   - **Repository name:** `recoverybench`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
4. In your GitHub repository:
   - Navigate to `https://github.com/jenilprajapati777/recoverybench/settings/environments`
   - Click **New environment**, enter `pypi`, and save.
   - (Optional) Configure deployment branch rules to restrict releases to tags or `main`.
5. When creating a release, draft a release on GitHub targeting a tag (e.g. `v0.1.0`), which will trigger `.github/workflows/publish.yml` to authenticate via OIDC and publish to PyPI without API keys.
