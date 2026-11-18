"""Validation logic for RecoveryBench scenario definitions."""

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from recoverybench.errors import ConfigurationError, ScenarioValidationError
from recoverybench.scenarios.schema import (
    SUPPORTED_SCENARIO_SCHEMA_VERSIONS,
    ScenarioModel,
)

SUPPORTED_YAML_EXTENSIONS: frozenset[str] = frozenset({".yaml", ".yml"})
SUPPORTED_JSON_EXTENSIONS: frozenset[str] = frozenset({".json"})
SUPPORTED_SCENARIO_EXTENSIONS: frozenset[str] = SUPPORTED_YAML_EXTENSIONS | SUPPORTED_JSON_EXTENSIONS


class ScenarioValidator:
    """Validates raw dictionary, YAML, or JSON files against scenario schema and semantic rules."""

    @classmethod
    def validate_dict(cls, data: Any, source_label: str = "inline") -> ScenarioModel:
        """Validate raw dictionary data against scenario schema and semantic rules."""
        if not isinstance(data, dict):
            raise ScenarioValidationError(
                f"Invalid scenario data in '{source_label}': expected dictionary/object, got {type(data).__name__}"
            )

        errors: list[str] = []
        try:
            scenario = ScenarioModel.model_validate(data)
        except ValidationError as e:
            for err in e.errors():
                loc = " -> ".join(str(x) for x in err["loc"])
                msg = err["msg"]
                if err["loc"] == ("schema_version",) and err["type"] == "missing":
                    msg = (
                        f"Field required. Missing required field 'schema_version'. "
                        f"Supported versions: {sorted(SUPPORTED_SCENARIO_SCHEMA_VERSIONS)}."
                    )
                errors.append(f"[{loc}]: {msg}")
            raise ScenarioValidationError(
                f"Schema validation failed for scenario '{source_label}'",
                errors=errors,
            ) from e

        # Semantic Checks
        if not scenario.scenario_id or " " in scenario.scenario_id:
            errors.append(f"scenario_id '{scenario.scenario_id}' must be non-empty without spaces.")

        for fault in scenario.faults:
            if fault.target_tool != scenario.target_tool:
                errors.append(
                    f"Fault target_tool '{fault.target_tool}' does not match scenario target_tool '{scenario.target_tool}'."
                )

        if (
            scenario.single_operation is True
            and scenario.assertions.expected_side_effects is not None
            and scenario.assertions.expected_side_effects > 1
        ):
            errors.append(
                f"Contradictory scenario configuration: 'single_operation: true' is incompatible with "
                f"'expected_side_effects: {scenario.assertions.expected_side_effects}' (> 1). "
                "A single logical business operation can commit at most 1 side-effect. Set 'single_operation: false' for multi-operation scenarios."
            )

        if errors:
            raise ScenarioValidationError(
                f"Semantic validation failed for scenario '{source_label}'",
                errors=errors,
            )

        return scenario

    @classmethod
    def validate_yaml_file(cls, path: str | Path) -> ScenarioModel:
        """Load and validate a YAML scenario file."""
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigurationError(f"Scenario file not found: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            raise ConfigurationError(f"Failed to read scenario file {file_path}: {e}") from e

        try:
            data = yaml.safe_load(content)
        except Exception as e:
            raise ScenarioValidationError(f"Failed to parse YAML in {file_path}: {e}") from e

        if not isinstance(data, dict):
            raise ScenarioValidationError(
                f"Invalid YAML root in {file_path}: expected dictionary/object, got {type(data).__name__}"
            )

        return cls.validate_dict(data, source_label=file_path.name)

    @classmethod
    def validate_json_file(cls, path: str | Path) -> ScenarioModel:
        """Load and validate a JSON scenario file."""
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigurationError(f"Scenario file not found: {file_path}")

        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception as e:
            raise ConfigurationError(f"Failed to read scenario file {file_path}: {e}") from e

        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise ScenarioValidationError(f"Failed to parse JSON in {file_path}: {e}") from e
        except Exception as e:
            raise ScenarioValidationError(f"Failed to parse JSON in {file_path}: {e}") from e

        if not isinstance(data, dict):
            raise ScenarioValidationError(
                f"Invalid JSON root in {file_path}: expected dictionary/object, got {type(data).__name__}"
            )

        return cls.validate_dict(data, source_label=file_path.name)

    @classmethod
    def validate_file(cls, path: str | Path) -> ScenarioModel:
        """Load and validate a scenario file based on its file extension (.yaml, .yml, .json)."""
        file_path = Path(path)
        if not file_path.exists():
            raise ConfigurationError(f"Scenario file not found: {file_path}")

        ext = file_path.suffix.lower()
        if ext in SUPPORTED_YAML_EXTENSIONS:
            return cls.validate_yaml_file(file_path)
        if ext in SUPPORTED_JSON_EXTENSIONS:
            return cls.validate_json_file(file_path)

        supported_str = ", ".join(sorted(SUPPORTED_SCENARIO_EXTENSIONS))
        raise ScenarioValidationError(
            f"Unsupported scenario file format '{file_path.suffix}' for {file_path}. "
            f"Supported extensions: {supported_str}"
        )

    @classmethod
    def validate_directory(cls, directory: str | Path) -> tuple[list[ScenarioModel], list[str]]:
        """Scan a directory for all .yaml, .yml, and .json scenario files and validate each one."""
        dir_path = Path(directory)
        if not dir_path.exists() or not dir_path.is_dir():
            raise ConfigurationError(f"Scenarios directory not found: {dir_path}")

        valid_scenarios: list[ScenarioModel] = []
        errors: list[str] = []

        scenario_files = sorted(set(
            list(dir_path.glob("*.yaml"))
            + list(dir_path.glob("*.yml"))
            + list(dir_path.glob("*.json"))
        ))
        if not scenario_files:
            errors.append(f"No .yaml, .yml, or .json scenario files found in '{dir_path}'.")
            return valid_scenarios, errors

        for file_path in scenario_files:
            try:
                scenario = cls.validate_file(file_path)
                valid_scenarios.append(scenario)
            except ScenarioValidationError as sve:
                errors.append(f"{file_path.name}: {sve.message}")
                for err in sve.errors:
                    errors.append(f"  - {err}")
            except ConfigurationError as ce:
                errors.append(f"{file_path.name}: {ce.message}")
            except Exception as exc:
                errors.append(f"{file_path.name}: {exc}")

        return valid_scenarios, errors


load_scenario = ScenarioValidator.validate_file
