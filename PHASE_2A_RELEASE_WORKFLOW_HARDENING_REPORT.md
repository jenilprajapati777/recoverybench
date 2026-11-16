# RecoveryBench — Phase 2A Release Workflow Hardening Report

**Date:** 2026-11-16  
**Auditor / Packaging Engineer:** Senior Python Packaging & Release Engineer  
**Repository:** `https://github.com/jenilprajapati777/recoverybench`  
**Current Working Branch:** `phase-2/cli-distribution`  
**Status:** COMPLETE & VERIFIED  

---

## 1. Executive Summary

A critical release workflow hardening review was conducted on [.github/workflows/publish.yml](.github/workflows/publish.yml). The workflow has been hardened against accidental manual triggering and version desynchronization while preserving PyPI Trusted Publishing (OIDC), strict metadata verification, and automated distribution smoke testing.

All core backend components, CLI commands, package metadata, and CI regression matrices remain intact and passing.

---

## 2. Hardening Measures Implemented

### 2.1 Elimination of Manual Execution Vectors
- **Problem Identified:** `.github/workflows/publish.yml` previously defined `workflow_dispatch` alongside `release: types: [published]`. This permitted manual workflow runs without an explicitly published GitHub Release, creating the risk of un-gated distribution pushes.
- **Remediation:** Removed `workflow_dispatch` completely. The only declared trigger is now:
  ```yaml
  on:
    release:
      types: [published]
  ```

### 2.2 Defensive Job-Level Gating
- Added an explicit `if` condition on the `publish-to-pypi` job:
  ```yaml
  if: github.event_name == 'release' && github.event.action == 'published'
  ```
  This ensures that even in the event of upstream trigger configuration changes, the job requesting the OIDC token and uploading to PyPI will only execute when backed by a published GitHub Release event.

### 2.3 Pre-Build Release Tag vs. Package Version Validation
- Added a dedicated validation step before package compilation in the `build` job:
  ```yaml
  - name: Validate Release Tag Matches pyproject.toml Version
    run: |
      python -c "
      import os, sys, tomllib

      tag_name = os.environ.get('RELEASE_TAG', '').strip()
      if not tag_name:
          sys.exit('Release tag validation failed: RELEASE_TAG environment variable is empty or missing!')

      normalized_tag = tag_name[1:] if tag_name.startswith('v') else tag_name

      with open('pyproject.toml', 'rb') as f:
          pyproject = tomllib.load(f)

      pkg_version = pyproject.get('project', {}).get('version', '').strip()
      if not pkg_version:
          sys.exit('Release tag validation failed: Could not read project.version from pyproject.toml!')

      print(f'Release tag: \'{tag_name}\' (normalized: \'{normalized_tag}\')')
      print(f'pyproject.toml version: \'{pkg_version}\'')

      if normalized_tag != pkg_version:
          sys.exit(
              f'Release tag mismatch! Tag \'{tag_name}\' (normalized \'{normalized_tag}\') '
              f'does not match pyproject.toml version \'{pkg_version}\'.'
          )

      print('Release tag matches pyproject.toml version successfully!')
      "
    env:
      RELEASE_TAG: ${{ github.event.release.tag_name }}
  ```
- **Validation Rules:**
  - The release tag must be non-empty.
  - Strips optional leading `v` (e.g. `v0.1.0` -> `0.1.0`, or `0.1.0` -> `0.1.0`).
  - Asserts exact equality between normalized tag and `project.version` in `pyproject.toml`.
  - Fails closed immediately (`sys.exit(...)`) before building wheels or sdist if a mismatch occurs.

### 2.4 Preserved Security & Packaging Invariants
- **Wheel & Sdist Compilation:** `python -m build` produces standard wheel and sdist artifacts.
- **Strict Metadata Inspection:** `twine check --strict dist/*` asserts PEP 639 and PyPI standard compliance.
- **Distribution Smoke Testing:** `scripts/distribution_smoke_test.py` validates isolated installation and CLI commands outside the source repository.
- **Artifact Segregation:** Artifacts transfer between `build` and `publish-to-pypi` via `upload-artifact@v4` and `download-artifact@v4`.
- **PyPI Trusted Publishing (OIDC):** `id-token: write` permission is granted strictly to the `publish-to-pypi` job.
- **Environment Gating:** Bound to GitHub environment `pypi`.
- **Zero Static Credentials:** No long-lived API tokens or secrets stored in repository.

---

## 3. Automated Workflow Validation Test

Added `test_publish_workflow_configuration()` to [tests/test_ci_integration.py](tests/test_ci_integration.py):
- Verifies that the only workflow trigger is `release: types: [published]`.
- Asserts that `workflow_dispatch`, `push`, and `pull_request` triggers are absent.
- Asserts that `publish-to-pypi` depends on `build` and has the exact defensive `if` condition.
- Asserts that `id-token: write` is configured on `publish-to-pypi` and environment is `pypi`.
- Asserts that the tag/version validation step is present and correctly passes `RELEASE_TAG: ${{ github.event.release.tag_name }}`.
- Asserts that `pypa/gh-action-pypi-publish@release/v1` is used and no static `PYPI_API_TOKEN` is referenced.

---

## 4. Quality & Regression Verification Results

- **Ruff:** `ruff check .` -> **All checks passed!** (0 errors).
- **Mypy:** `mypy src tests scripts` -> **Success: no issues found in 80 source files**.
- **Pytest:** `pytest -v` -> **198 passed, 12 skipped, 0 failed** in 8.41s.
  - `tests/test_ci_integration.py::test_publish_workflow_configuration` -> **PASSED**.
- **Distribution Smoke Test:** `python scripts/distribution_smoke_test.py` -> **ALL DISTRIBUTION SMOKE TESTS PASSED SUCCESSFULLY!**

---

## 5. Release Constraints Confirmation

- **No Remote Pushes:** All changes held locally on `phase-2/cli-distribution`.
- **No PyPI Releases:** No package published to PyPI.
- **No GitHub Releases or Tags:** No tags or releases created.
- **Backend & CI Matrix Intact:** No alterations to Docker isolation, ledger semantics, or existing CI test matrix.
