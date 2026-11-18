# RecoveryBench — Phase 3B.2 Scenario Schema Versioning Report

**Milestone:** Phase 3B.2 — Explicit, Deterministic, Independently Versioned Scenario Schemas  
**Branch:** `phase-3b/scenario-schema-versioning`  
**Base Commit:** `4f2168f` (Merge pull request #2 from `phase-3/public-contracts`)  
**Package Version:** `0.1.0`  
**Status:** Verification Complete — Ready for Merge  

---

## 1. Executive Summary

Phase 3B.2 establishes an explicit, deterministic, and independently versioned contract for RecoveryBench scenarios. Previously, `ScenarioModel` lacked schema version tracking, user scenario files lacked version metadata, and only YAML file loading was supported.

This implementation delivers:
1. **Centralized Schema Version Policy**: Defines `CURRENT_SCENARIO_SCHEMA_VERSION = 1` and `SUPPORTED_SCENARIO_SCHEMA_VERSIONS = frozenset({1})`. The scenario specification version is completely decoupled from the application package version (`0.1.0`).
2. **Strict Version Validation**: Enforces `schema_version: int` on `ScenarioModel` with zero silent fallbacks. Missing versions, unsupported versions (e.g., `0`, `2`, `99`), and non-integer types (e.g., `bool`, `str`, `float`) fail closed with clear, informative errors. Direct Python constructor invocation (`ScenarioModel(...)`) validates identically to serialized files.
3. **Unified Validation Pipeline**: Extends `ScenarioValidator` to validate dictionaries, YAML files (`.yaml`, `.yml`), and JSON files (`.json`) through a single unified validation and semantic checking pipeline without duplicated logic.
4. **Resilient & Deterministic Directory Discovery**: Extends `validate_directory` and `ScenarioRegistry.load_user_scenarios` to discover, sort, and validate `.yaml`, `.yml`, and `.json` scenarios in deterministic order, with per-file read error resilience allowing valid scenarios to be processed even if an individual file is unreadable.
5. **Full Built-In Migration**: Migrated all 6 repository built-in scenario files, in-memory registry definitions, sample project templates, documentation, and test fixtures to `schema_version: 1`.

---

## 2. Schema Version Policy

The schema version policy is centralized in [`src/recoverybench/scenarios/schema.py`](file:///D:/RecoveryBench/src/recoverybench/scenarios/schema.py):

```python
CURRENT_SCENARIO_SCHEMA_VERSION: int = 1
SUPPORTED_SCENARIO_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})
```

### Key Principles

- **Decoupled from Package Version:** The RecoveryBench package version (`0.1.0`) tracks application releases, CLI tools, and runtime dependencies. The scenario schema version (`1`) tracks the declarative schema structure (fault configuration, task payload, assertion rules).
- **Explicit Version Support Scope:** A scenario authored with `schema_version: 1` is supported by all RecoveryBench releases that explicitly maintain support for schema version 1. RecoveryBench does not promise unconditional forward compatibility across arbitrary future major schema revisions; when future schema revisions (e.g. version 2) are introduced, the platform's version policy will define the explicit set of supported versions.
- **Fail-Closed Type Safety:** In Python, `bool` is a subclass of `int` (`isinstance(True, int) == True`). The version validator explicitly enforces `type(version) is int`, ensuring `True` and `False` are strictly rejected. Floats (`1.0`), strings (`"1"`), and missing values (`None`) are similarly rejected.
- **Breaking Change for Unversioned Scenarios:** Existing unversioned scenario files are **not** backward-compatible because `schema_version: 1` is now mandatory. Scenarios without a version are never assumed to be the current version. They fail explicitly, requiring the author to supply `schema_version: 1`.
- **Descriptive Error Messaging:** Errors cite both the received version/type and the list of currently supported versions:
  - *Missing*: `[schema_version]: Field required. Missing required field 'schema_version'. Supported versions: [1].`
  - *Unsupported*: `[schema_version]: Value error, Unsupported scenario schema_version: 2. Supported versions: [1].`
  - *Invalid Type*: `[schema_version]: Value error, Invalid schema_version type 'str'. schema_version must be an integer, supported versions: [1].`

---

## 3. Supported Input Formats & Validation Interfaces

The validator is implemented in [`src/recoverybench/scenarios/validator.py`](file:///D:/RecoveryBench/src/recoverybench/scenarios/validator.py):

| Interface | Input | Behavior |
| :--- | :--- | :--- |
| `validate_dict(data, source_label="inline")` | `dict[str, Any]` | Validates root is a dictionary, runs Pydantic schema validation, enriches error messages, and executes semantic checks (`scenario_id`, tool matching, single-operation consistency). |
| `validate_yaml_file(path)` | `str \| Path` | Safely parses UTF-8 YAML (`yaml.safe_load`), checks root is an object/dict, and delegates to `validate_dict`. |
| `validate_json_file(path)` | `str \| Path` | Parses UTF-8 JSON (`json.loads`), checks root is an object/dict, and delegates to `validate_dict`. |
| `validate_file(path)` | `str \| Path` | Inspects file extension (`.yaml`, `.yml`, `.json`), dispatches to YAML or JSON loader, or raises `ScenarioValidationError` for unsupported extensions. |
| `validate_directory(directory)` | `str \| Path` | Validates directory existence, scans for `.yaml`, `.yml`, and `.json` in deterministic sorted order, validates each file, catches per-file read/validation errors into `errors`, and continues processing remaining files. |
| `load_scenario(path)` | `str \| Path` | Public Python SDK entry point aliasing `validate_file`. Actively maintained and dynamically validates both YAML (`.yaml`, `.yml`) and JSON (`.json`) files. |

### Equivalence Guarantee
YAML and JSON files defining identical scenario structures produce identical `ScenarioModel` instances (`yaml_model == json_model` and `yaml_model.model_dump() == json_model.model_dump()`).

---

## 4. Migration Instructions for Existing Scenarios

Existing unversioned custom scenario files are **not** backward-compatible and must be migrated by adding `schema_version: 1` as a top-level field.

### YAML Migration
```diff
+ schema_version: 1
  scenario_id: custom_order_retry
  name: "Order Service Timeout Recovery"
  description: "Tests recovery when order placement times out"
  target_tool: order_service
  target_action: place_order
  faults:
    - type: timeout_after_commit
      target_tool: order_service
      target_action: place_order
      failure_count: 1
  task:
    order_id: "ord_9901"
  assertions:
    expected_side_effects: 1
    require_stable_idempotency_key: true
```

### JSON Migration
```diff
  {
+   "schema_version": 1,
    "scenario_id": "custom_order_retry",
    "name": "Order Service Timeout Recovery",
    "description": "Tests recovery when order placement times out",
    "target_tool": "order_service",
    "target_action": "place_order",
    "faults": [
      {
        "type": "timeout_after_commit",
        "target_tool": "order_service",
        "target_action": "place_order",
        "failure_count": 1
      }
    ],
    "task": {
      "order_id": "ord_9901"
    },
    "assertions": {
      "expected_side_effects": 1,
      "require_stable_idempotency_key": true
    }
  }
```

---

## 5. Files Changed

| File | Changes Made |
| :--- | :--- |
| [`src/recoverybench/scenarios/schema.py`](file:///D:/RecoveryBench/src/recoverybench/scenarios/schema.py) | Added `CURRENT_SCENARIO_SCHEMA_VERSION`, `SUPPORTED_SCENARIO_SCHEMA_VERSIONS`, `validate_scenario_schema_version()`, `schema_version: int` field, and `@field_validator("schema_version", mode="before")`. |
| [`src/recoverybench/scenarios/validator.py`](file:///D:/RecoveryBench/src/recoverybench/scenarios/validator.py) | Implemented `validate_json_file()`, `validate_file()`, extension constants (`SUPPORTED_YAML_EXTENSIONS`, `SUPPORTED_JSON_EXTENSIONS`), enhanced error reporting on missing schema version, updated `validate_directory()` to discover `.yaml`, `.yml`, and `.json` with per-file `ConfigurationError` handling, and set `load_scenario = ScenarioValidator.validate_file`. |
| [`src/recoverybench/scenarios/registry.py`](file:///D:/RecoveryBench/src/recoverybench/scenarios/registry.py) | Updated `_register_builtins()` to supply `schema_version=CURRENT_SCENARIO_SCHEMA_VERSION` on all 6 built-ins; updated `load_user_scenarios()` to discover and sort `.yaml`, `.yml`, and `.json` files via `validate_file()`. |
| [`src/recoverybench/scenarios/__init__.py`](file:///D:/RecoveryBench/src/recoverybench/scenarios/__init__.py) | Exported `CURRENT_SCENARIO_SCHEMA_VERSION`, `SUPPORTED_SCENARIO_SCHEMA_VERSIONS`, `validate_scenario_schema_version`, extension constants, and `load_scenario`. |
| [`src/recoverybench/cli.py`](file:///D:/RecoveryBench/src/recoverybench/cli.py) | Updated `validate` and `run` commands to use `ScenarioValidator.validate_file()`, enabling direct validation and execution of both YAML and JSON scenario files. |
| [`src/recoverybench/sample_project/resources.py`](file:///D:/RecoveryBench/src/recoverybench/sample_project/resources.py) | Added `schema_version: 1` to `SAMPLE_TIMEOUT_AFTER_COMMIT_YAML` and `SAMPLE_TIMEOUT_BEFORE_COMMIT_YAML`. |
| [`scenarios/*.yaml`](file:///D:/RecoveryBench/scenarios/) | Migrated all 6 repository built-in scenario files (`payment_duplicate_delivery.yaml`, `payment_http_500_transient.yaml`, `payment_malformed_response.yaml`, `payment_rate_limit_429.yaml`, `payment_timeout_after_commit.yaml`, `payment_timeout_before_commit.yaml`) to declare `schema_version: 1`. |
| [`docs/SDK_INTEGRATION_GUIDE.md`](file:///D:/RecoveryBench/docs/SDK_INTEGRATION_GUIDE.md) | Added Section 8 detailing scenario schema versioning, file formats, `load_scenario` entry point, migration steps (breaking change for unversioned files), and failure modes; updated read-only scenario YAML example to include `schema_version: 1`. |
| [`README.md`](file:///D:/RecoveryBench/README.md) | Updated declarative scenario documentation with versioned YAML and JSON guidance, breaking change notice, and `load_scenario` capabilities. |
| [`tests/test_schema_validation.py`](file:///D:/RecoveryBench/tests/test_schema_validation.py) | Comprehensive 44-test regression suite covering dictionary validation, version errors, type errors, YAML/YML/JSON file loading, equivalence checks, malformed syntax, non-dict roots, missing files, unsupported extensions, registry JSON loading, mixed discovery, deterministic sorting, duplicate override behavior, and per-file read resilience. |
| [`tests/test_cli.py`](file:///D:/RecoveryBench/tests/test_cli.py) | Added CLI regression tests for JSON scenario validation, JSON scenario execution, malformed JSON error output, and unsupported version error output. |
| Test fixtures across `tests/` | Added `schema_version=1` across test fixtures in `tests/conftest.py`, `tests/test_all_fault_modes_recovery.py`, `tests/test_cli.py`, `tests/test_execution_engine.py`, `tests/test_false_success_final_hardening.py`, `tests/test_false_success_prevention.py`, and `tests/test_transaction_identity.py`. |

---

## 6. Tests Added & Coverage

### Focused Schema & Registry Suite (44 tests in `tests/test_schema_validation.py`):
1. `test_valid_version_1_dict`: Validates valid version 1 dictionary.
2. `test_missing_schema_version_rejected`: Verifies missing `schema_version` fails with explicit missing field and supported version message.
3. `test_none_schema_version_rejected`: Verifies `None` schema version is rejected.
4. `test_unsupported_schema_versions_rejected` (parametrized across `[0, -1, 2, 99, 100]`): Verifies lower and higher unsupported versions are rejected with supported versions cited.
5. `test_invalid_schema_version_types_rejected` (parametrized across `[True, False, "1", "v1", 1.0, 1.5, [1], {"version": 1}]`): Verifies bools, strings, floats, and containers are strictly rejected.
6. `test_standalone_version_validator`: Unit tests standalone `validate_scenario_schema_version()` function and exported constants.
7. `test_direct_model_instantiation_contract`: Verifies direct `ScenarioModel(...)` constructor rejects missing versions, unsupported versions, and booleans while accepting version 1.
8. `test_validate_yaml_file`: Verifies loading `.yaml` files.
9. `test_validate_yml_extension_file`: Verifies loading `.yml` files.
10. `test_validate_json_file`: Verifies loading `.json` files.
11. `test_equivalent_yaml_and_json_definitions`: Asserts identical `ScenarioModel` instances produced from YAML and JSON sources.
12. `test_load_scenario_alias`: Verifies `load_scenario` alias dispatches across `.yaml` and `.json`.
13. `test_malformed_yaml`: Verifies informative `ScenarioValidationError` on broken YAML syntax.
14. `test_malformed_json`: Verifies informative `ScenarioValidationError` on broken JSON syntax.
15. `test_yaml_root_not_dict`: Verifies rejection of YAML lists or scalar roots.
16. `test_json_root_not_dict`: Verifies rejection of JSON arrays or primitive roots.
17. `test_validate_dict_not_a_dict`: Verifies rejection of non-dictionary inputs passed to `validate_dict`.
18. `test_missing_file_raises_configuration_error`: Verifies non-existent files raise `ConfigurationError`.
19. `test_unsupported_file_extension`: Verifies unsupported extensions (e.g. `.toml`) raise `ScenarioValidationError`.
20. `test_directory_discovery_yaml_yml_and_json`: Verifies deterministic sorted multi-format discovery (`.yaml`, `.yml`, `.json`) ignoring non-scenario files.
21. `test_directory_discovery_with_invalid_scenarios`: Verifies directory validation captures all errors from invalid versions and malformed files.
22. `test_directory_discovery_empty`: Verifies empty directory reporting.
23. `test_invalid_fault_type`: Verifies fault schema validation remains intact.
24. `test_mismatched_target_tool_semantic_error`: Verifies semantic tool mismatch checks remain intact.
25. `test_contradictory_single_operation_semantic_error`: Verifies single-operation semantic consistency checks remain intact.
26. `test_registry_builtins`: Verifies all 6 built-ins in `ScenarioRegistry` declare `schema_version == 1`.
27. `test_all_repository_built_in_scenario_files_on_disk`: Verifies all 6 on-disk built-in files validate cleanly.
28. `test_registry_load_user_scenarios_json`: Verifies loading user scenarios in JSON format via `ScenarioRegistry.load_user_scenarios()`.
29. `test_registry_discovery_mixed_yaml_and_json`: Verifies co-discovery of YAML, YML, and JSON files in the registry.
30. `test_registry_discovery_order_deterministic`: Verifies deterministic alphabetical discovery sorting across formats.
31. `test_registry_duplicate_scenario_id_override_behavior`: Verifies user scenarios override built-in scenarios, and later files in sorted discovery order override earlier files for duplicate IDs.
32. `test_validate_directory_handles_per_file_read_configuration_error`: Verifies that an unreadable scenario file reports a per-file `ConfigurationError` without aborting directory validation for remaining valid files.
33. `test_validate_directory_missing_or_non_dir_preserves_configuration_error`: Verifies that invalid or missing directory roots fail fast with `ConfigurationError`.

### CLI Suite (21 tests in `tests/test_cli.py`):
Includes new tests:
- `test_cli_validate_json_success`: Validates `.json` scenario via CLI `validate`.
- `test_cli_run_json_scenario_success`: Executes `.json` scenario via CLI `run`.
- `test_cli_validate_json_malformed_error`: Verifies readable error and exit code 2 on malformed JSON.
- `test_cli_validate_json_unsupported_version_error`: Verifies readable error and exit code 2 citing unsupported version and supported versions `[1]`.

---

## 7. Exact Verification Commands and Results

All checks executed directly in the project environment:

| Command | Status | Result Summary |
| :--- | :--- | :--- |
| `.venv\Scripts\pytest tests/test_schema_validation.py tests/test_cli.py -v` | **PASS** | 65 passed in 2.71s |
| `.venv\Scripts\pytest -q` | **PASS** | **253 passed, 12 skipped in 8.42s** |
| `.venv\Scripts\ruff check src tests` | **PASS** | All checks passed |
| `.venv\Scripts\mypy src tests` | **PASS** | Success: no issues found in 79 source files |
| `git diff --check` | **PASS** | Clean: zero whitespace or format issues |
| `.venv\Scripts\recoverybench validate scenarios` | **PASS** | All 6 scenario files in 'scenarios' validated successfully |
| `.venv\Scripts\recoverybench demo` | **PASS** | Comparative demo executed and reports written |
| `.venv\Scripts\recoverybench suite scenarios --agent idempotent --isolation trusted` | **PASS** | All 6 suite scenarios passed in 94.0ms |
| `.venv\Scripts\python -m build --sdist --wheel` | **PASS** | Built `recoverybench-0.1.0.tar.gz` and wheel cleanly |

---

## 8. Known Limitations & Forward Roadmap

1. **Single Supported Version**: Currently, only `schema_version: 1` is valid. Future schema versions (e.g., `2`) will expand `SUPPORTED_SCENARIO_SCHEMA_VERSIONS` without requiring automatic migration of version 1 files.
2. **Single Document per File**: Multi-document YAML streams (multiple documents separated by `---` in one file) are intentionally rejected by root dictionary validation. Each scenario must reside in its own dedicated file.
3. **Explicit Breaking Change**: Unversioned scenario files are not backward-compatible. This breaking change was deliberate to prevent false-success risks in benchmark evaluations.
