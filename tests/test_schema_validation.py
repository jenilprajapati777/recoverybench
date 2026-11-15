"""Unit tests for scenario schema validation and registry."""

from pathlib import Path

import pytest

from recoverybench.errors import ScenarioValidationError
from recoverybench.scenarios.registry import ScenarioRegistry
from recoverybench.scenarios.validator import ScenarioValidator


def test_valid_scenario_dict() -> None:
    data = {
        "scenario_id": "test_payment_recovery",
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
    scenario = ScenarioValidator.validate_dict(data)
    assert scenario.scenario_id == "test_payment_recovery"
    assert len(scenario.faults) == 1
    assert scenario.faults[0].type.value == "timeout_after_commit"
    assert scenario.assertions.expected_side_effects == 1


def test_invalid_fault_type() -> None:
    data = {
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
        "scenario_id": "mismatch_test",
        "name": "Mismatch Tool",
        "description": "Fault targets email_service while scenario targets payment_gateway",
        "target_tool": "payment_gateway",
        "faults": [{"type": "timeout_after_commit", "target_tool": "email_service"}],
    }
    with pytest.raises(ScenarioValidationError) as exc_info:
        ScenarioValidator.validate_dict(data)
    assert "target_tool" in str(exc_info.value)


def test_validate_yaml_file(tmp_path: Path) -> None:
    yaml_content = """
scenario_id: yaml_test_scenario
name: "YAML Test"
description: "A scenario from YAML"
target_tool: payment_gateway
target_action: charge
faults:
  - type: timeout_before_commit
    target_tool: payment_gateway
    target_action: charge
task:
  amount: 1000
"""
    file_path = tmp_path / "scenario.yaml"
    file_path.write_text(yaml_content, encoding="utf-8")

    scenario = ScenarioValidator.validate_yaml_file(file_path)
    assert scenario.scenario_id == "yaml_test_scenario"
    assert scenario.target_action == "charge"


def test_registry_builtins() -> None:
    registry = ScenarioRegistry()
    scenarios = registry.list_all()
    assert len(scenarios) >= 6
    ids = [s.scenario_id for s in scenarios]
    assert "payment_timeout_after_commit" in ids
    assert "payment_timeout_before_commit" in ids
    assert "payment_rate_limit_429" in ids
    assert "payment_duplicate_delivery" in ids
    assert "payment_http_500_transient" in ids
    assert "payment_malformed_response" in ids
