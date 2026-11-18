"""End-to-end CLI command tests and exit code verification."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.cli import app
from recoverybench.errors import ConfigurationError, InfrastructureError

runner = CliRunner()


def test_cli_version() -> None:
    from recoverybench import __version__

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "RecoveryBench" in result.stdout
    assert __version__ in result.stdout


def test_cli_version_short() -> None:
    from recoverybench import __version__

    result = runner.invoke(app, ["-v"])
    assert result.exit_code == 0
    assert "RecoveryBench" in result.stdout
    assert __version__ in result.stdout


def test_cli_doctor() -> None:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "Environment Diagnostics" in result.stdout
    assert "Python Runtime" in result.stdout


def test_cli_init(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0
    assert (tmp_path / "recoverybench.yaml").exists()
    assert (tmp_path / "scenarios" / "payment_timeout_after_commit.yaml").exists()
    assert (tmp_path / "scenarios" / "payment_timeout_before_commit.yaml").exists()


def test_cli_list_scenarios() -> None:
    result = runner.invoke(app, ["list-scenarios"])
    assert result.exit_code == 0
    assert "payment_timeout" in result.stdout


def test_cli_list_scenarios_json() -> None:
    result = runner.invoke(app, ["list-scenarios", "--json"])
    assert result.exit_code == 0
    assert "payment_timeout_after_commit" in result.stdout


def test_cli_validate_success(tmp_path: Path) -> None:
    valid_file = tmp_path / "valid.yaml"
    valid_file.write_text(
        "schema_version: 1\n"
        "scenario_id: valid_test\n"
        "name: Valid Test\n"
        "description: A valid test scenario\n"
        "target_tool: payment_gateway\n"
        "target_action: charge\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(valid_file)])
    assert result.exit_code == 0
    assert "Valid scenario" in result.stdout


def test_cli_validate_error(tmp_path: Path) -> None:
    invalid_file = tmp_path / "invalid.yaml"
    invalid_file.write_text(
        """
scenario_id: bad_id_with spaces
name: Invalid Test
target_tool: payment_gateway
faults:
  - type: bad_fault_type
""",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["validate", str(invalid_file)])
    assert result.exit_code == 2


def test_cli_validate_json_success(tmp_path: Path) -> None:
    json_file = tmp_path / "valid.json"
    json_data = {
        "schema_version": 1,
        "scenario_id": "valid_json_cli_test",
        "name": "Valid JSON CLI Test",
        "description": "A valid test scenario defined in JSON",
        "target_tool": "payment_gateway",
        "target_action": "charge",
    }
    json_file.write_text(json.dumps(json_data), encoding="utf-8")
    result = runner.invoke(app, ["validate", str(json_file)])
    assert result.exit_code == 0
    assert "Valid scenario" in result.stdout
    assert "valid_json_cli_test" in result.stdout


def test_cli_run_json_scenario_success(tmp_path: Path) -> None:
    json_file = tmp_path / "payment_test.json"
    json_data = {
        "schema_version": 1,
        "scenario_id": "json_run_scenario",
        "name": "JSON Run Scenario",
        "description": "Tests executing a scenario loaded from a JSON file",
        "target_tool": "payment_gateway",
        "target_action": "charge",
        "faults": [
            {
                "type": "timeout_after_commit",
                "target_tool": "payment_gateway",
                "target_action": "charge",
                "failure_count": 1,
                "error_message": "Network dropped connection after commit",
            }
        ],
        "task": {"amount": 5000, "currency": "USD", "customer_id": "cust_json_1"},
        "assertions": {
            "expected_side_effects": 1,
            "max_duplicate_effects": 0,
            "require_stable_idempotency_key": True,
            "max_allowed_retries": 3,
            "expect_agent_success": True,
        },
    }
    json_file.write_text(json.dumps(json_data), encoding="utf-8")
    result = runner.invoke(app, ["run", str(json_file), "--agent", "idempotent", "--isolation", "trusted"])
    assert result.exit_code == 0
    assert "PASS" in result.stdout


def test_cli_validate_json_malformed_error(tmp_path: Path) -> None:
    malformed_file = tmp_path / "malformed.json"
    malformed_file.write_text('{"schema_version": 1, "unclosed: }', encoding="utf-8")
    result = runner.invoke(app, ["validate", str(malformed_file)])
    assert result.exit_code != 0
    combined_output = result.output or result.stderr
    assert "Failed to parse JSON" in combined_output or "Error" in combined_output


def test_cli_validate_json_unsupported_version_error(tmp_path: Path) -> None:
    bad_ver_file = tmp_path / "bad_version.json"
    bad_data = {
        "schema_version": 99,
        "scenario_id": "bad_ver_test",
        "name": "Bad Version Test",
        "description": "Tests error output for unsupported schema version",
        "target_tool": "payment_gateway",
    }
    bad_ver_file.write_text(json.dumps(bad_data), encoding="utf-8")
    result = runner.invoke(app, ["validate", str(bad_ver_file)])
    assert result.exit_code != 0
    combined_output = result.output or result.stderr
    assert "Unsupported scenario schema_version: 99" in combined_output
    assert "Supported versions: [1]" in combined_output


def test_cli_demo() -> None:
    result = runner.invoke(app, ["demo"])
    assert result.exit_code == 0
    assert "RECOVERYBENCH COMPARATIVE DEMONSTRATION" in result.stdout
    assert "Unsafe Agent (Naive Payment Agent)" in result.stdout
    assert "Resilient Agent (Idempotent Payment Agent)" in result.stdout
    assert "DOUBLE CHARGE" in result.stdout


def test_cli_run_idempotent_pass() -> None:
    result = runner.invoke(app, ["run", "payment_timeout_after_commit", "--agent", "idempotent", "--isolation", "trusted"])
    assert result.exit_code == 0
    assert "PASS" in result.stdout


def test_cli_run_naive_fail() -> None:
    result = runner.invoke(app, ["run", "payment_timeout_after_commit", "--agent", "naive", "--isolation", "trusted"])
    assert result.exit_code == 1
    assert "GENUINE_FAILURE" in result.stdout


def test_cli_run_unknown_scenario() -> None:
    result = runner.invoke(app, ["run", "nonexistent_scenario_123", "--isolation", "trusted"])
    assert result.exit_code == 2


def test_cli_run_infrastructure_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_infra(self: NaivePaymentAgent, task: object, tool: object) -> object:
        raise InfrastructureError("Simulated ledger disk write failure")

    monkeypatch.setattr(NaivePaymentAgent, "run", raise_infra)
    result = runner.invoke(app, ["run", "payment_timeout_after_commit", "--agent", "naive", "--isolation", "trusted"])
    assert result.exit_code == 3
    assert "INFRASTRUCTURE_ERROR" in result.stdout


def test_cli_run_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_config(self: NaivePaymentAgent, task: object, tool: object) -> object:
        raise ConfigurationError("Simulated invalid configuration parameter")

    monkeypatch.setattr(NaivePaymentAgent, "run", raise_config)
    result = runner.invoke(app, ["run", "payment_timeout_after_commit", "--agent", "naive", "--isolation", "trusted"])
    assert result.exit_code == 2
    assert "INFRASTRUCTURE_ERROR" in result.stdout


def test_cli_replay_and_report(tmp_path: Path) -> None:
    # First, run a scenario with demo to generate runs in DB
    demo_res = runner.invoke(app, ["demo"])
    assert demo_res.exit_code == 0

    # Replay naive run
    replay_res = runner.invoke(app, ["replay", "demo_naive_agent_run"])
    assert replay_res.exit_code == 0
    assert "demo_naive_agent_run" in replay_res.stdout
    assert "Historical Event Sequence" in replay_res.stdout

    # Replay as JSON
    replay_json_res = runner.invoke(app, ["replay", "demo_safe_agent_run", "--json"])
    assert replay_json_res.exit_code == 0
    assert "demo_safe_agent_run" in replay_json_res.stdout

    # Replay missing run
    replay_bad_res = runner.invoke(app, ["replay", "nonexistent_run_404"])
    assert replay_bad_res.exit_code == 2

    # Export Report in all formats
    report_res = runner.invoke(app, ["report", "demo_safe_agent_run", "--format", "all", "--output-dir", str(tmp_path)])
    assert report_res.exit_code == 0
    assert (tmp_path / "demo_safe_agent_run_report.json").exists()
    assert (tmp_path / "demo_safe_agent_run_report.html").exists()
    assert (tmp_path / "demo_safe_agent_run_report.xml").exists()


def test_cli_run_with_junit_flag(tmp_path: Path) -> None:
    junit_file = tmp_path / "custom_junit.xml"
    result = runner.invoke(app, ["run", "payment_timeout_after_commit", "--agent", "idempotent", "--junit", str(junit_file), "--isolation", "trusted"])
    assert result.exit_code == 0
    assert junit_file.exists()
    assert "<testsuites" in junit_file.read_text(encoding="utf-8")


def test_cli_run_default_isolated_fails_closed_when_docker_unavailable() -> None:
    """When Docker is unavailable, running with default or explicit --isolation isolated must fail closed (exit code 3)."""
    from recoverybench.sandbox.docker_client import DockerClient
    is_docker, _ = DockerClient().is_docker_available()
    if is_docker:
        pytest.skip("Docker is available on this host; testing fail-closed requires Docker to be unavailable")
    result = runner.invoke(app, ["run", "payment_timeout_after_commit", "--agent", "idempotent"])
    assert result.exit_code == 3
    assert "INFRASTRUCTURE_ERROR" in result.stdout
