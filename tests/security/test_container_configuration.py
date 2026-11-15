"""Tests for container configuration, security flags, resource limits, and command generation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from recoverybench.sandbox.config import SandboxConfig
from recoverybench.sandbox.docker_client import DockerClient
from recoverybench.sandbox.spec import AgentSpec


def test_sandbox_config_defaults() -> None:
    """Verify default sandbox security configuration matches strict isolation requirements."""
    cfg = SandboxConfig()
    assert cfg.memory_limit == "512m"
    assert cfg.cpu_limit == "1.0"
    assert cfg.pids_limit == 64
    assert cfg.timeout_seconds == 30.0
    assert cfg.tmpfs_size == "64m"
    assert cfg.user == "nobody:nogroup"
    assert cfg.cap_drop == ["ALL"]
    assert "@sha256:" in cfg.runtime_image
    assert cfg.runtime_image.startswith("python:3.11-slim@sha256:")
    assert cfg.allow_mutable_image_tags is False
    assert cfg.read_only_root is True
    assert cfg.no_new_privileges is True


def test_sandbox_config_rejects_mutable_image_tags_by_default() -> None:
    """Verify mutable image tags without SHA-256 digest are rejected by default."""
    with pytest.raises(ValidationError) as exc_info:
        SandboxConfig(runtime_image="python:3.11-slim")
    assert "Mutable runtime image tag" in str(exc_info.value)

    with pytest.raises(ValidationError):
        SandboxConfig(runtime_image="ubuntu:latest")


def test_sandbox_config_accepts_mutable_image_tag_with_explicit_override() -> None:
    """Verify mutable image tags are accepted when allow_mutable_image_tags is explicitly enabled."""
    cfg = SandboxConfig(runtime_image="python:3.11-slim", allow_mutable_image_tags=True)
    assert cfg.runtime_image == "python:3.11-slim"
    assert cfg.allow_mutable_image_tags is True


def test_sandbox_config_accepts_valid_sha256_digest() -> None:
    """Verify custom image pinned by an exact 64-hex SHA-256 digest is accepted."""
    custom = f"my-registry.io/agent-runtime@sha256:{'f' * 64}"
    cfg = SandboxConfig(runtime_image=custom)
    assert cfg.runtime_image == custom


def test_sandbox_config_validation_invalid_memory() -> None:
    """Verify invalid memory limit formats or dangerously low limits are rejected."""
    with pytest.raises(ValidationError):
        SandboxConfig(memory_limit="invalid_format")

    with pytest.raises(ValidationError):
        SandboxConfig(memory_limit="16m")  # Below minimum 32m requirement


def test_sandbox_config_validation_invalid_cpu() -> None:
    """Verify invalid CPU limits are rejected."""
    with pytest.raises(ValidationError):
        SandboxConfig(cpu_limit="0")

    with pytest.raises(ValidationError):
        SandboxConfig(cpu_limit="-0.5")

    with pytest.raises(ValidationError):
        SandboxConfig(cpu_limit="not_a_number")


def test_sandbox_config_validation_invalid_pids() -> None:
    """Verify invalid PID limits are rejected."""
    with pytest.raises(ValidationError):
        SandboxConfig(pids_limit=0)

    with pytest.raises(ValidationError):
        SandboxConfig(pids_limit=-10)


def test_sandbox_config_validation_invalid_timeout() -> None:
    """Verify invalid execution timeouts are rejected."""
    with pytest.raises(ValidationError):
        SandboxConfig(timeout_seconds=0.0)

    with pytest.raises(ValidationError):
        SandboxConfig(timeout_seconds=-5.0)


def test_build_run_command_contains_all_security_flags(tmp_path: Path) -> None:
    """Verify generated Docker run arguments include all required isolation flags."""
    cfg = SandboxConfig(
        memory_limit="256m",
        cpu_limit="0.5",
        pids_limit=32,
        tmpfs_size="32m",
        user="nobody:nogroup",
    )
    client = DockerClient(cfg)

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("# worker", encoding="utf-8")

    cmd = client.build_run_command(
        container_name="test_secure_container",
        staged_dir=workspace_dir,
        worker_script_path=worker_script,
        env_vars={"TEST_AGENT_VAR": "val123"},
    )

    # 1. Base command structure
    exe_name = Path(cmd[0]).name.lower()
    assert exe_name in ("docker", "docker.exe")
    assert "run" in cmd
    assert "-i" in cmd
    assert "--name" in cmd
    assert "test_secure_container" in cmd

    # 2. Network isolation: MUST be --network=none
    assert "--network=none" in cmd or ("--network" in cmd and cmd[cmd.index("--network") + 1] == "none")

    # 3. Read-only root filesystem
    assert "--read-only" in cmd

    # 4. Capability dropping: MUST be ALL
    assert "--cap-drop=ALL" in cmd or ("--cap-drop" in cmd and cmd[cmd.index("--cap-drop") + 1] == "ALL")

    # 5. Privilege escalation prevention
    assert any("no-new-privileges" in arg for arg in cmd)

    # 6. Resource constraints
    assert "--pids-limit=32" in cmd or ("--pids-limit" in cmd and cmd[cmd.index("--pids-limit") + 1] == "32")
    assert "--memory=256m" in cmd or ("--memory" in cmd and cmd[cmd.index("--memory") + 1] == "256m")
    assert "--cpus=0.5" in cmd or ("--cpus" in cmd and cmd[cmd.index("--cpus") + 1] == "0.5")

    # 7. Non-root user
    assert "--user=nobody:nogroup" in cmd or ("--user" in cmd and cmd[cmd.index("--user") + 1] == "nobody:nogroup")

    # 8. Tmpfs mount: rw,noexec,nosuid
    tmpfs_args = [arg for arg in cmd if "tmpfs" in arg or "/tmp" in arg]
    assert any("noexec" in arg and "nosuid" in arg for arg in tmpfs_args)

    # 9. Read-only workspace and worker mounts
    workspace_mounts = [arg for arg in cmd if "/workspace" in arg]
    assert any(":ro" in arg or "ro" in arg for arg in workspace_mounts)

    worker_mounts = [arg for arg in cmd if "worker.py" in arg]
    assert any(":ro" in arg or "ro" in arg for arg in worker_mounts)

    # 10. Environment variables
    assert any("TEST_AGENT_VAR=val123" in arg for arg in cmd)

    # 11. Command entrypoint at the end
    assert cmd[-3:] == ["python", "-u", "/worker.py"]


def test_build_run_command_forbidden_flags_never_present(tmp_path: Path) -> None:
    """Verify dangerous Docker flags are strictly excluded from execution command."""
    client = DockerClient()

    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    worker_script = tmp_path / "worker.py"
    worker_script.write_text("# worker", encoding="utf-8")

    cmd = client.build_run_command(
        container_name="test_secure_container",
        staged_dir=workspace_dir,
        worker_script_path=worker_script,
    )

    cmd_str = " ".join(cmd)

    # Must NEVER run with root privileges or disable security
    assert "--privileged" not in cmd
    assert "--privileged=true" not in cmd_str

    # Must NEVER share host network
    assert "--net=host" not in cmd_str
    assert "--network=host" not in cmd_str

    # Must NEVER share host PID namespace
    assert "--pid=host" not in cmd_str

    # Must NEVER grant direct access to host devices
    assert "--device" not in cmd_str

    # Must NEVER mount the Docker socket
    assert "docker.sock" not in cmd_str


def test_agent_spec_environment_isolation() -> None:
    """Verify AgentSpec environment isolation and allowlist behavior."""
    spec = AgentSpec(
        module_path="my_agent.py",
        entrypoint="run_agent",
        env={
            "AGENT_SETTING": "enabled",
            "CUSTOM_PARAM": "42",
        },
    )

    assert spec.env["AGENT_SETTING"] == "enabled"
    assert spec.env["CUSTOM_PARAM"] == "42"
    # Ensure system host secrets are not automatically included in spec.env
    assert "AWS_SECRET_ACCESS_KEY" not in spec.env
    assert "OPENAI_API_KEY" not in spec.env
