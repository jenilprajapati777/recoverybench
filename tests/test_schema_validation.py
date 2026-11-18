"""Unit tests for scenario schema validation, versioning, and registry."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from recoverybench.errors import ConfigurationError, ScenarioValidationError
from recoverybench.scenarios import (
    CURRENT_SCENARIO_SCHEMA_VERSION,
    SUPPORTED_SCENARIO_SCHEMA_VERSIONS,
    ScenarioModel,
    ScenarioRegistry,
    ScenarioValidator,
    load_scenario,
    validate_scenario_schema_version,
)


def _sample_scenario_dict(
    schema_version: object = CURRENT_SCENARIO_SCHEMA_VERSION,
    scenario_id: str = "test_payment_recovery",
) -> dict[str, object]:
    """Helper returning a valid scenario dictionary."""
    data: dict[str, object] = {
        "scenario_id": scenario_id,
        "name": "Payment Recovery Test",
        "description": "Tests recovery after gateway timeout",
        "target_tool": "payment_gateway",
        "target_action": "charge",
        "faults": [
            {
                "type": "timeout_after_commit",
                "target_tool": "payment_gateway",
                "target_action": "charge",
                "failure_count": 1,
            }
        ],
        "task": {"amount": 5000, "currency": "USD"},
        "assertions": {
            "expected_side_effects": 1,
            "max_duplicate_effects": 0,
            "require_stable_idempotency_key": True,
        },
    }
    if schema_version is not ...:
        data["schema_version"] = schema_version
    return data


# =============================================================================
# 1. Schema Version Contract Tests
# =============================================================================


def test_valid_version_1_dict() -> None:
    data = _sample_scenario_dict(schema_version=1)
    scenario = ScenarioValidator.validate_dict(data)
    assert scenario.schema_version == 1
    assert scenario.scenario_id == "test_payment_recovery"
    assert len(scenario.faults) == 1
    assert scenario.faults[0].type.value == "timeout_after_commit"
    assert scenario.assertions.expected_side_effects == 1


def test_missing_schema_version_rejected() -> None:
    data = _sample_scenario_dict(schema_version=...)
    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_dict(data)
    err_str = str(exc_info.value)
    assert "schema_version" in err_str
    assert "Field required" in err_str
    assert "1" in err_str


def test_none_schema_version_rejected() -> None:
    data = _sample_scenario_dict(schema_version=None)
    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_dict(data)
    err_str = str(exc_info.value)
    assert "schema_version" in err_str


@pytest.mark.parametrize("unsupported_version", [0, -1, 2, 99, 100])
def test_unsupported_schema_versions_rejected(unsupported_version: int) -> None:
    data = _sample_scenario_dict(schema_version=unsupported_version)
    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_dict(data)
    err_str = str(exc_info.value)
    assert f"Unsupported scenario schema_version: {unsupported_version}" in err_str
    assert "Supported versions: [1]" in err_str


@pytest.mark.parametrize("invalid_type_value", [True, False, "1", "v1", 1.0, 1.5, [1], {"version": 1}])
def test_invalid_schema_version_types_rejected(invalid_type_value: object) -> None:
    data = _sample_scenario_dict(schema_version=invalid_type_value)
    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_dict(data)
    err_str = str(exc_info.value)
    assert "schema_version" in err_str
    assert "Invalid schema_version type" in err_str or "must be an integer" in err_str


def test_standalone_version_validator() -> None:
    assert validate_scenario_schema_version(1) == 1
    assert CURRENT_SCENARIO_SCHEMA_VERSION == 1
    assert SUPPORTED_SCENARIO_SCHEMA_VERSIONS == frozenset({1})

    with pytest.raises(ValueError, match="required"):
        validate_scenario_schema_version(None)

    with pytest.raises(ValueError, match="Invalid schema_version type 'bool'"):
        validate_scenario_schema_version(True)

    with pytest.raises(ValueError, match="Invalid schema_version type 'str'"):
        validate_scenario_schema_version("1")

    with pytest.raises(ValueError, match="Invalid schema_version type 'float'"):
        validate_scenario_schema_version(1.0)

    with pytest.raises(ValueError, match="Unsupported scenario schema_version: 2"):
        validate_scenario_schema_version(2)


def test_direct_model_instantiation_contract() -> None:
    # 1. Missing schema_version fails
    with pytest.raises(ValidationError) as exc_info:
        ScenarioModel(  # type: ignore[call-arg]
            scenario_id="direct_model",
            name="Direct",
            description="Direct model test",
            target_tool="payment_gateway",
        )
    assert "schema_version" in str(exc_info.value)

    # 2. Unsupported schema_version fails
    with pytest.raises(ValidationError) as exc_info:
        ScenarioModel(
            schema_version=2,
            scenario_id="direct_model",
            name="Direct",
            description="Direct model test",
            target_tool="payment_gateway",
        )
    assert "Unsupported scenario schema_version: 2" in str(exc_info.value)

    # 3. Invalid bool type fails
    with pytest.raises(ValidationError) as exc_info:
        ScenarioModel(
            schema_version=True,
            scenario_id="direct_model",
            name="Direct",
            description="Direct model test",
            target_tool="payment_gateway",
        )
    assert "Invalid schema_version type 'bool'" in str(exc_info.value)

    # 4. Valid version 1 passes
    valid_model = ScenarioModel(
        schema_version=1,
        scenario_id="direct_model",
        name="Direct",
        description="Direct model test",
        target_tool="payment_gateway",
    )
    assert valid_model.schema_version == 1
    assert valid_model.scenario_id == "direct_model"


# =============================================================================
# 2. File Loading & Format Tests (YAML, YML, JSON)
# =============================================================================


def test_validate_yaml_file(tmp_path: Path) -> None:
    yaml_content = """
schema_version: 1
scenario_id: yaml_test_scenario
name: "YAML Test"
description: "A scenario from YAML"
target_tool: payment_gateway
target_action: charge
faults:
  - type: timeout_before_commit
    target_tool: payment_gateway
    target_action: charge
    failure_count: 1
task:
  amount: 1000
"""
    file_path = tmp_path / "scenario.yaml"
    file_path.write_text(yaml_content, encoding="utf-8")

    scenario = ScenarioValidator.validate_yaml_file(file_path)
    assert scenario.schema_version == 1
    assert scenario.scenario_id == "yaml_test_scenario"
    assert scenario.target_action == "charge"

    # Also test dispatch via validate_file
    scenario_disp = ScenarioValidator.validate_file(file_path)
    assert scenario_disp.scenario_id == scenario.scenario_id


def test_validate_yml_extension_file(tmp_path: Path) -> None:
    yaml_content = """
schema_version: 1
scenario_id: yml_ext_scenario
name: "YML Extension Test"
description: "A scenario from .yml"
target_tool: payment_gateway
"""
    file_path = tmp_path / "scenario.yml"
    file_path.write_text(yaml_content, encoding="utf-8")

    scenario = ScenarioValidator.validate_file(file_path)
    assert scenario.schema_version == 1
    assert scenario.scenario_id == "yml_ext_scenario"


def test_validate_json_file(tmp_path: Path) -> None:
    json_data = _sample_scenario_dict(schema_version=1, scenario_id="json_test_scenario")
    file_path = tmp_path / "scenario.json"
    file_path.write_text(json.dumps(json_data), encoding="utf-8")

    scenario = ScenarioValidator.validate_json_file(file_path)
    assert scenario.schema_version == 1
    assert scenario.scenario_id == "json_test_scenario"

    # Also test dispatch via validate_file
    scenario_disp = ScenarioValidator.validate_file(file_path)
    assert scenario_disp.scenario_id == scenario.scenario_id


def test_equivalent_yaml_and_json_definitions(tmp_path: Path) -> None:
    raw_data = _sample_scenario_dict(schema_version=1, scenario_id="equiv_scenario")

    yaml_path = tmp_path / "scenario.yaml"
    yaml_content = """
schema_version: 1
scenario_id: equiv_scenario
name: "Payment Recovery Test"
description: "Tests recovery after gateway timeout"
target_tool: payment_gateway
target_action: charge
faults:
  - type: timeout_after_commit
    target_tool: payment_gateway
    target_action: charge
    failure_count: 1
task:
  amount: 5000
  currency: USD
assertions:
  expected_side_effects: 1
  max_duplicate_effects: 0
  require_stable_idempotency_key: true
"""
    yaml_path.write_text(yaml_content, encoding="utf-8")

    json_path = tmp_path / "scenario.json"
    json_path.write_text(json.dumps(raw_data, indent=2), encoding="utf-8")

    yaml_model = ScenarioValidator.validate_file(yaml_path)
    json_model = ScenarioValidator.validate_file(json_path)

    assert yaml_model == json_model
    assert yaml_model.model_dump() == json_model.model_dump()


def test_load_scenario_alias(tmp_path: Path) -> None:
    yaml_path = tmp_path / "s.yaml"
    yaml_path.write_text("schema_version: 1\nscenario_id: s_yaml\nname: S\ndescription: D\ntarget_tool: T\n", encoding="utf-8")
    json_path = tmp_path / "s.json"
    json_path.write_text(json.dumps({"schema_version": 1, "scenario_id": "s_json", "name": "S", "description": "D", "target_tool": "T"}), encoding="utf-8")

    assert load_scenario(yaml_path).scenario_id == "s_yaml"
    assert load_scenario(json_path).scenario_id == "s_json"


# =============================================================================
# 3. Malformed Data, Bad Root & Unsupported Extensions Tests
# =============================================================================


def test_malformed_yaml(tmp_path: Path) -> None:
    file_path = tmp_path / "malformed.yaml"
    file_path.write_text(":\n  - :\n    broken: [", encoding="utf-8")

    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_yaml_file(file_path)
    assert "Failed to parse YAML" in str(exc_info.value)


def test_malformed_json(tmp_path: Path) -> None:
    file_path = tmp_path / "malformed.json"
    file_path.write_text('{"schema_version": 1, "unclosed_string: }', encoding="utf-8")

    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_json_file(file_path)
    assert "Failed to parse JSON" in str(exc_info.value)


def test_yaml_root_not_dict(tmp_path: Path) -> None:
    file_path = tmp_path / "list_root.yaml"
    file_path.write_text("- item1\n- item2\n", encoding="utf-8")

    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_yaml_file(file_path)
    assert "expected dictionary/object" in str(exc_info.value)


def test_json_root_not_dict(tmp_path: Path) -> None:
    file_path = tmp_path / "list_root.json"
    file_path.write_text('["item1", "item2"]', encoding="utf-8")

    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_json_file(file_path)
    assert "expected dictionary/object" in str(exc_info.value)


def test_validate_dict_not_a_dict() -> None:
    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_dict(["not", "a", "dict"])
    assert "expected dictionary/object" in str(exc_info.value)


def test_missing_file_raises_configuration_error(tmp_path: Path) -> None:
    missing_yaml = tmp_path / "does_not_exist.yaml"
    with pytest.raises(ConfigurationError) as exc_info:
        ScenarioValidator.validate_yaml_file(missing_yaml)
    assert "not found" in str(exc_info.value)

    missing_json = tmp_path / "does_not_exist.json"
    with pytest.raises(ConfigurationError) as exc_info:
        ScenarioValidator.validate_json_file(missing_json)
    assert "not found" in str(exc_info.value)

    with pytest.raises(ConfigurationError) as exc_info:
        ScenarioValidator.validate_file(missing_yaml)
    assert "not found" in str(exc_info.value)


def test_unsupported_file_extension(tmp_path: Path) -> None:
    file_path = tmp_path / "scenario.toml"
    file_path.write_text('schema_version = 1\nscenario_id = "test"\n', encoding="utf-8")

    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_file(file_path)
    assert "Unsupported scenario file format '.toml'" in str(exc_info.value)
    assert ".yaml" in str(exc_info.value)
    assert ".json" in str(exc_info.value)


# =============================================================================
# 4. Directory Discovery & Validation Tests
# =============================================================================


def test_directory_discovery_yaml_yml_and_json(tmp_path: Path) -> None:
    # 1. Write a .yaml file
    (tmp_path / "scen_1.yaml").write_text(
        "schema_version: 1\nscenario_id: scen_1\nname: S1\ndescription: D1\ntarget_tool: T\n",
        encoding="utf-8",
    )
    # 2. Write a .yml file
    (tmp_path / "scen_2.yml").write_text(
        "schema_version: 1\nscenario_id: scen_2\nname: S2\ndescription: D2\ntarget_tool: T\n",
        encoding="utf-8",
    )
    # 3. Write a .json file
    (tmp_path / "scen_3.json").write_text(
        json.dumps({"schema_version": 1, "scenario_id": "scen_3", "name": "S3", "description": "D3", "target_tool": "T"}),
        encoding="utf-8",
    )
    # 4. Write an unrelated file that should be ignored
    (tmp_path / "notes.txt").write_text("Not a scenario", encoding="utf-8")

    scenarios, errors = ScenarioValidator.validate_directory(tmp_path)
    assert len(errors) == 0
    assert len(scenarios) == 3
    # Check deterministic sorted order
    assert [s.scenario_id for s in scenarios] == ["scen_1", "scen_2", "scen_3"]


def test_directory_discovery_with_invalid_scenarios(tmp_path: Path) -> None:
    # Valid
    (tmp_path / "valid.yaml").write_text(
        "schema_version: 1\nscenario_id: valid_id\nname: V\ndescription: D\ntarget_tool: T\n",
        encoding="utf-8",
    )
    # Invalid version in JSON
    (tmp_path / "bad_version.json").write_text(
        json.dumps({"schema_version": 99, "scenario_id": "bad_ver", "name": "B", "description": "D", "target_tool": "T"}),
        encoding="utf-8",
    )
    # Missing version in YAML
    (tmp_path / "unversioned.yaml").write_text(
        "scenario_id: no_ver\nname: N\ndescription: D\ntarget_tool: T\n",
        encoding="utf-8",
    )
    # Malformed JSON
    (tmp_path / "corrupt.json").write_text("{broken json", encoding="utf-8")

    scenarios, errors = ScenarioValidator.validate_directory(tmp_path)
    assert len(scenarios) == 1
    assert scenarios[0].scenario_id == "valid_id"
    # 3 files had errors; ScenarioValidationError entries include header and detail lines
    assert len(errors) == 5

    errors_text = " ".join(errors)
    assert "bad_version.json" in errors_text
    assert "unversioned.yaml" in errors_text
    assert "corrupt.json" in errors_text


def test_directory_discovery_empty(tmp_path: Path) -> None:
    scenarios, errors = ScenarioValidator.validate_directory(tmp_path)
    assert len(scenarios) == 0
    assert len(errors) == 1
    assert "No .yaml, .yml, or .json scenario files found" in errors[0]


# =============================================================================
# 5. Semantic & Built-in Scenarios Regression Tests
# =============================================================================


def test_invalid_fault_type() -> None:
    data = {
        "schema_version": 1,
        "scenario_id": "bad_fault",
        "name": "Bad Fault",
        "description": "Invalid fault name",
        "target_tool": "payment_gateway",
        "faults": [{"type": "nonexistent_fault_mode", "target_tool": "payment_gateway"}],
    }
    with pytest.raises(ScenarioValidationError):
        ScenarioValidator.validate_dict(data)


def test_mismatched_target_tool_semantic_error() -> None:
    data = {
        "schema_version": 1,
        "scenario_id": "mismatch_test",
        "name": "Mismatch Tool",
        "description": "Fault targets email_service while scenario targets payment_gateway",
        "target_tool": "payment_gateway",
        "faults": [{"type": "timeout_after_commit", "target_tool": "email_service"}],
    }
    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_dict(data)
    assert "target_tool" in str(exc_info.value)


def test_contradictory_single_operation_semantic_error() -> None:
    data = {
        "schema_version": 1,
        "scenario_id": "contradictory_op",
        "name": "Contradictory",
        "description": "Single operation with 2 expected side effects",
        "target_tool": "payment_gateway",
        "single_operation": True,
        "assertions": {"expected_side_effects": 2},
    }
    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_dict(data)
    assert "Contradictory scenario configuration" in str(exc_info.value)


def test_registry_builtins() -> None:
    registry = ScenarioRegistry()
    scenarios = registry.list_all()
    assert len(scenarios) >= 6
    for s in scenarios:
        assert s.schema_version == CURRENT_SCENARIO_SCHEMA_VERSION == 1

    ids = [s.scenario_id for s in scenarios]
    assert "payment_timeout_after_commit" in ids
    assert "payment_timeout_before_commit" in ids
    assert "payment_rate_limit_429" in ids
    assert "payment_duplicate_delivery" in ids
    assert "payment_http_500_transient" in ids
    assert "payment_malformed_response" in ids


def test_all_repository_built_in_scenario_files_on_disk() -> None:
    scenarios_dir = Path("scenarios")
    valid_scenarios, errors = ScenarioValidator.validate_directory(scenarios_dir)
    assert errors == []
    assert len(valid_scenarios) == 6
    for s in valid_scenarios:
        assert s.schema_version == 1


# =============================================================================
# 6. ScenarioRegistry Multi-Format Discovery & Override Tests
# =============================================================================


def test_registry_load_user_scenarios_json(tmp_path: Path) -> None:
    json_path = tmp_path / "custom_user.json"
    json_path.write_text(
        json.dumps({
            "schema_version": 1,
            "scenario_id": "json_user_scenario",
            "name": "Custom User JSON Scenario",
            "description": "User scenario in JSON format",
            "target_tool": "payment_gateway",
        }),
        encoding="utf-8",
    )
    registry = ScenarioRegistry(scenarios_dir=tmp_path)
    registry.load_user_scenarios()

    loaded = registry.get("json_user_scenario")
    assert loaded is not None
    assert loaded.schema_version == 1
    assert loaded.name == "Custom User JSON Scenario"


def test_registry_discovery_mixed_yaml_and_json(tmp_path: Path) -> None:
    (tmp_path / "scen_a.yaml").write_text(
        "schema_version: 1\nscenario_id: scen_yaml_a\nname: YAML A\ndescription: D\ntarget_tool: T\n",
        encoding="utf-8",
    )
    (tmp_path / "scen_b.json").write_text(
        json.dumps({
            "schema_version": 1,
            "scenario_id": "scen_json_b",
            "name": "JSON B",
            "description": "D",
            "target_tool": "T",
        }),
        encoding="utf-8",
    )
    (tmp_path / "scen_c.yml").write_text(
        "schema_version: 1\nscenario_id: scen_yml_c\nname: YML C\ndescription: D\ntarget_tool: T\n",
        encoding="utf-8",
    )

    registry = ScenarioRegistry(scenarios_dir=tmp_path)
    registry.load_user_scenarios()

    assert registry.get("scen_yaml_a") is not None
    assert registry.get("scen_json_b") is not None
    assert registry.get("scen_yml_c") is not None


def test_registry_discovery_order_deterministic(tmp_path: Path) -> None:
    (tmp_path / "03_charlie.json").write_text(
        json.dumps({"schema_version": 1, "scenario_id": "charlie", "name": "C", "description": "D", "target_tool": "T"}),
        encoding="utf-8",
    )
    (tmp_path / "01_alpha.yaml").write_text(
        "schema_version: 1\nscenario_id: alpha\nname: A\ndescription: D\ntarget_tool: T\n",
        encoding="utf-8",
    )
    (tmp_path / "02_bravo.json").write_text(
        json.dumps({"schema_version": 1, "scenario_id": "bravo", "name": "B", "description": "D", "target_tool": "T"}),
        encoding="utf-8",
    )

    registry = ScenarioRegistry(scenarios_dir=tmp_path)
    registry.load_user_scenarios()

    assert registry.get("alpha") is not None
    assert registry.get("bravo") is not None
    assert registry.get("charlie") is not None


def test_registry_duplicate_scenario_id_override_behavior(tmp_path: Path) -> None:
    # 1. User file overrides built-in scenario with identical scenario_id
    (tmp_path / "payment_timeout_after_commit.json").write_text(
        json.dumps({
            "schema_version": 1,
            "scenario_id": "payment_timeout_after_commit",
            "name": "User Custom Override of Timeout After Commit",
            "description": "Custom user definition overriding default built-in",
            "target_tool": "payment_gateway",
            "target_action": "charge",
        }),
        encoding="utf-8",
    )

    # 2. Two user files in directory with identical scenario_id: later in sorted order overrides earlier
    (tmp_path / "01_item.yaml").write_text(
        "schema_version: 1\nscenario_id: dup_test\nname: First Defined\ndescription: D\ntarget_tool: T\n",
        encoding="utf-8",
    )
    (tmp_path / "02_item.json").write_text(
        json.dumps({
            "schema_version": 1,
            "scenario_id": "dup_test",
            "name": "Second Defined Override",
            "description": "D",
            "target_tool": "T",
        }),
        encoding="utf-8",
    )

    registry = ScenarioRegistry(scenarios_dir=tmp_path)
    registry.load_user_scenarios()

    # User scenario overrides built-in:
    timeout_after = registry.get("payment_timeout_after_commit")
    assert timeout_after is not None
    assert timeout_after.name == "User Custom Override of Timeout After Commit"

    # Sorted discovery overrides earlier with later file:
    dup_scenario = registry.get("dup_test")
    assert dup_scenario is not None
    assert dup_scenario.name == "Second Defined Override"


# =============================================================================
# 7. Resilient Directory Validation Tests
# =============================================================================


def test_validate_directory_handles_per_file_read_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unreadable scenario file records an error and allows directory validation to continue."""
    (tmp_path / "01_good.yaml").write_text(
        "schema_version: 1\nscenario_id: good_01\nname: Good 01\ndescription: D\ntarget_tool: T\n",
        encoding="utf-8",
    )
    unreadable_file = tmp_path / "02_unreadable.yaml"
    unreadable_file.write_text("schema_version: 1\nscenario_id: unreadable_02\n", encoding="utf-8")

    (tmp_path / "03_good.json").write_text(
        json.dumps({"schema_version": 1, "scenario_id": "good_03", "name": "Good 03", "description": "D", "target_tool": "T"}),
        encoding="utf-8",
    )

    original_read_text = Path.read_text

    def mock_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if self.name == "02_unreadable.yaml":
            raise OSError("Permission denied: simulated unreadable file")
        return original_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", mock_read_text)

    valid_scenarios, errors = ScenarioValidator.validate_directory(tmp_path)
    # Validation must NOT abort; valid files 01 and 03 must be successfully processed
    assert len(valid_scenarios) == 2
    assert [s.scenario_id for s in valid_scenarios] == ["good_01", "good_03"]

    assert len(errors) == 1
    assert "02_unreadable.yaml" in errors[0]
    assert "Failed to read scenario file" in errors[0]
    assert "Permission denied" in errors[0]


def test_validate_directory_missing_or_non_dir_preserves_configuration_error(tmp_path: Path) -> None:
    # 1. Non-existent directory raises ConfigurationError
    with pytest.raises(ConfigurationError) as exc_info_missing:
        ScenarioValidator.validate_directory(tmp_path / "does_not_exist_dir")
    assert "Scenarios directory not found" in str(exc_info_missing.value)

    # 2. File path passed instead of directory raises ConfigurationError
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("just a file", encoding="utf-8")
    with pytest.raises(ConfigurationError) as exc_info_file:
        ScenarioValidator.validate_directory(regular_file)
    assert "Scenarios directory not found" in str(exc_info_file.value)
