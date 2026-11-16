"""Automated tests for GitHub Actions CI/CD integration, batch suite execution, and security redaction."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from recoverybench.cli import app
from recoverybench.engine.context import RunResult
from recoverybench.errors import ConfigurationError
from recoverybench.reporting.github_summary import (
    append_github_step_summary,
    generate_markdown_summary,
)
from recoverybench.scenarios.validator import ScenarioValidator
from recoverybench.sdk.tester import test_agent as rb_test_agent
from recoverybench.security import (
    mask_string_secrets,
    redact_sensitive_data,
)

runner = CliRunner()


def test_scenario_validator_validate_directory() -> None:
    """Test batch directory validation on the scenarios folder."""
    scenarios_dir = Path("scenarios")
    valid_scenarios, errors = ScenarioValidator.validate_directory(scenarios_dir)

    assert len(errors) == 0
    assert len(valid_scenarios) == 6
    scenario_ids = [s.scenario_id for s in valid_scenarios]
    assert "payment_timeout_after_commit" in scenario_ids
    assert "payment_rate_limit_429" in scenario_ids


def test_scenario_validator_directory_not_found() -> None:
    """Test that a non-existent directory raises ConfigurationError."""
    with pytest.raises(ConfigurationError):
        ScenarioValidator.validate_directory(Path("non_existent_dir_12345"))


def test_cli_validate_directory_success() -> None:
    """Test CLI validate command on scenarios directory."""
    result = runner.invoke(app, ["validate", "scenarios"])
    assert result.exit_code == 0
    assert "All 6 scenario files in 'scenarios' are valid" in result.output


def test_cli_suite_execution_idempotent_pass(tmp_path: Path) -> None:
    """Test CLI suite command running against safe idempotent agent."""
    reports_dir = tmp_path / "reports"
    result = runner.invoke(
        app,
        [
            "suite",
            "scenarios",
            "--agent",
            "idempotent",
            "--output-dir",
            str(reports_dir),
            "--junit",
            "--isolation",
            "trusted",
        ],
    )
    assert result.exit_code == 0
    assert "PASS" in result.output

    # Check generated artifacts
    assert (reports_dir / "suite_summary.json").exists()
    assert (reports_dir / "payment_timeout_after_commit_report.json").exists()
    assert (reports_dir / "payment_timeout_after_commit_report.html").exists()
    assert (reports_dir / "payment_timeout_after_commit_report.xml").exists()

    # Check summary content
    summary_data = json.loads((reports_dir / "suite_summary.json").read_text(encoding="utf-8"))
    assert summary_data["total_scenarios"] == 6
    assert summary_data["passed"] == 6
    assert summary_data["failed"] == 0


def test_cli_suite_execution_naive_fails_ci(tmp_path: Path) -> None:
    """Test CLI suite command running against naive agent fails with exit code 1."""
    reports_dir = tmp_path / "reports_fail"
    result = runner.invoke(
        app,
        [
            "suite",
            "scenarios",
            "--agent",
            "naive",
            "--output-dir",
            str(reports_dir),
            "--isolation",
            "trusted",
        ],
    )
    assert result.exit_code == 1
    assert "FAIL" in result.output

    summary_data = json.loads((reports_dir / "suite_summary.json").read_text(encoding="utf-8"))
    assert summary_data["failed"] > 0


def test_security_redaction_sensitive_keys() -> None:
    """Test redaction of sensitive parameter keys."""
    raw_payload = {
        "user": "alice",
        "api_key": "sk-secret-live-1234567890",
        "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        "password": "SuperSecretPassword123!",
        "card_number": "4111-2222-3333-4444",
        "nested": {
            "token": "tok_stripe_live_abcdef",
            "safe_param": "100_usd",
        },
    }

    redacted = redact_sensitive_data(raw_payload)

    assert redacted["user"] == "alice"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["Authorization"] == "[REDACTED]"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["card_number"] == "[REDACTED]"
    assert redacted["nested"]["token"] == "[REDACTED]"
    assert redacted["nested"]["safe_param"] == "100_usd"


def test_security_mask_string_secrets() -> None:
    """Test masking embedded credit card numbers and bearer tokens in strings."""
    text_with_cc = "Payment failed for card 4111222233334444 on attempt 1"
    masked_cc = mask_string_secrets(text_with_cc)
    assert "4111222233334444" not in masked_cc
    assert "[REDACTED_CARD]" in masked_cc

    text_with_token = "Header Authorization: Bearer abc123def456_xyz.99"
    masked_token = mask_string_secrets(text_with_token)
    assert "abc123def456_xyz.99" not in masked_token
    assert "Bearer [REDACTED_TOKEN]" in masked_token


def test_github_step_summary_generation(tmp_path: Path) -> None:
    """Test generation and writing of GITHUB_STEP_SUMMARY markdown."""
    from recoverybench.agent.idempotent import IdempotentPaymentAgent

    agent = IdempotentPaymentAgent()
    run_res: RunResult = rb_test_agent("payment_timeout_after_commit", agent)

    md = generate_markdown_summary([run_res])
    assert "## 🛡️ RecoveryBench Recovery Test Summary" in md
    assert "**Total Scenarios Evaluated:** 1" in md
    assert "| ✅ PASS |" in md

    # Test appending to file
    summary_file = tmp_path / "step_summary.md"
    appended = append_github_step_summary([run_res], summary_file)
    assert appended is True
    assert summary_file.exists()
    content = summary_file.read_text(encoding="utf-8")
    assert "## 🛡️ RecoveryBench Recovery Test Summary" in content


def test_publish_workflow_configuration() -> None:
    """Validate that .github/workflows/publish.yml meets security and release constraints."""
    import yaml

    workflow_path = Path(".github/workflows/publish.yml")
    assert workflow_path.exists(), "publish.yml workflow file must exist"

    with open(workflow_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # 1. Triggers: ONLY release with types: [published]
    triggers = data.get("on") or data.get(True)
    assert "workflow_dispatch" not in triggers, "workflow_dispatch must be removed"
    assert "push" not in triggers, "push trigger must not be present in publish workflow"
    assert "pull_request" not in triggers, "pull_request trigger must not be present in publish workflow"
    assert "release" in triggers, "release trigger must be present"
    assert triggers["release"].get("types") == ["published"], "release trigger must only be ['published']"
    assert len(triggers) == 1, "The only trigger must be 'release'"

    # 2. Jobs
    jobs = data.get("jobs", {})
    assert "build" in jobs, "build job must exist"
    assert "publish-to-pypi" in jobs, "publish-to-pypi job must exist"

    # 3. Publish job defensive gating
    publish_job = jobs["publish-to-pypi"]
    assert "if" in publish_job, "publish-to-pypi job must have an 'if' condition"
    condition = publish_job["if"]
    assert "github.event_name == 'release'" in condition, "Condition must check github.event_name == 'release'"
    assert "github.event.action == 'published'" in condition, "Condition must check github.event.action == 'published'"
    assert publish_job.get("needs") == ["build"], "publish-to-pypi must depend on build job"

    # 4. OIDC permissions and environment
    perms = publish_job.get("permissions", {})
    assert perms.get("id-token") == "write", "id-token: write permission must be configured on publish-to-pypi"
    env = publish_job.get("environment", {})
    env_name = env.get("name") if isinstance(env, dict) else env
    assert env_name == "pypi", "environment must be 'pypi'"

    # 5. Tag/version validation step in build job
    build_steps = jobs["build"].get("steps", [])
    step_names = [s.get("name", "") for s in build_steps]
    tag_validation_steps = [
        s for s in build_steps
        if "tag" in s.get("name", "").lower() and "version" in s.get("name", "").lower()
    ]
    assert len(tag_validation_steps) == 1, f"Expected 1 tag/version validation step, found: {step_names}"
    tag_step = tag_validation_steps[0]
    assert "RELEASE_TAG" in tag_step.get("env", {}), "RELEASE_TAG env var must be passed to validation step"
    assert "github.event.release.tag_name" in tag_step["env"]["RELEASE_TAG"], (
        "RELEASE_TAG must reference github.event.release.tag_name"
    )

    # 6. Action and no static tokens
    publish_steps = publish_job.get("steps", [])
    pypi_steps = [s for s in publish_steps if "pypa/gh-action-pypi-publish" in s.get("uses", "")]
    assert len(pypi_steps) == 1, "Must use pypa/gh-action-pypi-publish"
    assert "pypa/gh-action-pypi-publish@release/v1" in pypi_steps[0]["uses"]
    workflow_text = workflow_path.read_text(encoding="utf-8")
    assert "PYPI_API_TOKEN" not in workflow_text, "Workflow must not reference static PYPI_API_TOKEN"
