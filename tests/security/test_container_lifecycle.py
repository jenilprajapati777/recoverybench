"""Tests for container lifecycle, Docker availability checks, fail-closed isolation, and cleanup guarantees."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from recoverybench.agent.base import AgentResult, CompletionStatus, TaskContext
from recoverybench.cli import app
from recoverybench.clock import VirtualClock
from recoverybench.errors import ConfigurationError, InfrastructureError
from recoverybench.sandbox.backend import (
    DockerExecutionBackend,
    InProcessExecutionBackend,
    IsolationMode,
)
from recoverybench.sandbox.docker_client import DockerClient
from recoverybench.sandbox.spec import AgentSpec

runner = CliRunner()


def test_docker_client_executable_detection() -> None:
    """Verify DockerClient executable detection and availability checks."""
    client = DockerClient()
    is_avail, reason = client.is_docker_available()
    assert isinstance(is_avail, bool)
    assert isinstance(reason, str)


def test_docker_backend_fails_closed_when_docker_unavailable() -> None:
    """Verify DockerExecutionBackend raises InfrastructureError when Docker is unavailable."""
    backend = DockerExecutionBackend()

    with patch.object(backend.docker_client, "is_docker_available", return_value=(False, "Docker daemon not running")):
        task_ctx = TaskContext(scenario_id="test_scen", task_params={}, clock=VirtualClock())
        mock_tool = MagicMock()

        spec = AgentSpec(module_path="agent", entrypoint="run")

        with pytest.raises(InfrastructureError) as exc_info:
            backend.run(spec, task_ctx, mock_tool)

        err_msg = exc_info.value.message
        assert "Docker sandbox execution requested, but Docker is unavailable" in err_msg
        assert "Docker daemon not running" in err_msg
        assert "--isolation trusted" in err_msg


def test_docker_backend_fails_closed_when_image_missing() -> None:
    """Verify DockerExecutionBackend raises ConfigurationError when runtime image is missing."""
    backend = DockerExecutionBackend()

    with patch.object(backend.docker_client, "is_docker_available", return_value=(True, "Docker is ready")):
        with patch.object(backend.docker_client, "check_image_exists", return_value=False):
            task_ctx = TaskContext(scenario_id="test_scen", task_params={}, clock=VirtualClock())
            mock_tool = MagicMock()
            spec = AgentSpec(module_path="agent", entrypoint="run")

            with pytest.raises(ConfigurationError) as exc_info:
                backend.run(spec, task_ctx, mock_tool)

            err_msg = exc_info.value.message
            assert "is not available locally" in err_msg
            assert "docker pull" in err_msg


def test_docker_backend_cleanup_guarantee_on_error() -> None:
    """Verify container is aggressively killed and removed even if execution raises an unexpected error."""
    backend = DockerExecutionBackend()

    with patch.object(backend.docker_client, "is_docker_available", return_value=(True, "Ready")):
        with patch.object(backend.docker_client, "check_image_exists", return_value=True):
            with patch("recoverybench.sandbox.backend.WorkspaceStaging.stage_workspace") as mock_stage:
                mock_stage.return_value = Path("/tmp/mock_staged_workspace")
                with patch.object(backend.docker_client, "kill_and_remove") as mock_kill:
                    with patch("subprocess.Popen", side_effect=RuntimeError("Subprocess launch failed")):
                        task_ctx = TaskContext(scenario_id="test_scen", task_params={}, clock=VirtualClock())
                        mock_tool = MagicMock()
                        spec = AgentSpec(module_path="agent", entrypoint="run")

                        with pytest.raises(InfrastructureError):
                            backend.run(spec, task_ctx, mock_tool)

                        # Container cleanup must be called in finally block
                        assert mock_kill.called


def test_in_process_execution_backend_lifecycle() -> None:
    """Verify InProcessExecutionBackend runs correctly in trusted mode and marks results as TRUSTED."""
    backend = InProcessExecutionBackend()
    assert backend.mode == IsolationMode.TRUSTED
    assert backend.is_isolated is False

    def sample_agent(task: TaskContext, tool: MagicMock) -> AgentResult:
        return AgentResult(
            status=CompletionStatus.SUCCEEDED,
            success=True,
            data={"result": "ok"},
        )

    task_ctx = TaskContext(scenario_id="test_scen", task_params={}, clock=VirtualClock())
    mock_tool = MagicMock()

    result = backend.run(sample_agent, task_ctx, mock_tool)
    assert result.status == CompletionStatus.SUCCEEDED
    assert result.success is True
    assert result.data["result"] == "ok"


def test_cli_doctor_reports_isolation_status() -> None:
    """Verify recoverybench doctor reports Docker isolation readiness."""
    result = runner.invoke(app, ["doctor"])
    assert "RecoveryBench Doctor - Environment Diagnostics" in result.stdout
    assert "Sandbox Resource Limits" in result.stdout
    assert "Isolation Readiness" in result.stdout


def test_cli_doctor_require_isolation_fails_closed_when_docker_missing() -> None:
    """Verify recoverybench doctor --require-isolation exits with code 3 if Docker is unavailable."""
    client = DockerClient()
    is_docker, _ = client.is_docker_available()
    if is_docker:
        pytest.skip("Docker is available on this host; testing require-isolation failure requires Docker to be absent")

    result = runner.invoke(app, ["doctor", "--require-isolation"])
    assert result.exit_code == 3
