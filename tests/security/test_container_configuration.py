"""Tests for container configuration, security flags, resource limits, and command generation."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from recoverybench.errors import ConfigurationError
from recoverybench.sandbox.backend import DockerExecutionBackend
from recoverybench.sandbox.config import SandboxConfig, is_reserved_or_blocked_env
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


def test_agent_spec_env_cannot_override_path(tmp_path: Path) -> None:
    """Verify AgentSpec.env cannot override container system PATH in the generated Docker environment."""
    backend = DockerExecutionBackend()
    spec = AgentSpec(
        module_path="agent.py",
        entrypoint="run",
        env={"PATH": "/usr/local/malicious/bin:/bin", "SAFE_PARAM": "preserved_value"},
    )

    safe_env = backend._build_safe_env(spec)

    # 1. Verification in sanitized env dictionary
    assert "PATH" not in safe_env
    assert safe_env["SAFE_PARAM"] == "preserved_value"

    # 2. Verification in the actual generated Docker run command
    cmd = backend.docker_client.build_run_command(
        container_name="test_sandbox_path",
        staged_dir=tmp_path,
        env_vars=safe_env,
    )
    assert not any(arg.startswith("PATH=") or arg == "PATH" or arg == "-e PATH=" for arg in cmd)
    assert any("SAFE_PARAM=preserved_value" in arg for arg in cmd)

    # 3. Diagnostic advisory recorded
    assert any("PATH" in d for d in backend.last_env_diagnostics)


def test_agent_spec_env_cannot_inject_ld_preload(tmp_path: Path) -> None:
    """Verify AgentSpec.env cannot inject LD_PRELOAD into the generated Docker environment."""
    backend = DockerExecutionBackend()
    spec = AgentSpec(
        module_path="agent.py",
        entrypoint="run",
        env={"LD_PRELOAD": "/workspace/malicious_shim.so", "APP_MODE": "eval"},
    )

    safe_env = backend._build_safe_env(spec)

    assert "LD_PRELOAD" not in safe_env
    assert safe_env["APP_MODE"] == "eval"

    cmd = backend.docker_client.build_run_command(
        container_name="test_sandbox_ld_preload",
        staged_dir=tmp_path,
        env_vars=safe_env,
    )
    assert not any("LD_PRELOAD" in arg for arg in cmd)
    assert any("APP_MODE=eval" in arg for arg in cmd)
    assert any("LD_PRELOAD" in d for d in backend.last_env_diagnostics)


def test_agent_spec_env_cannot_inject_ld_library_path(tmp_path: Path) -> None:
    """Verify AgentSpec.env cannot inject LD_LIBRARY_PATH into the generated Docker environment."""
    backend = DockerExecutionBackend()
    spec = AgentSpec(
        module_path="agent.py",
        entrypoint="run",
        env={"LD_LIBRARY_PATH": "/workspace/lib_hijack", "CUSTOM_VAR": "valid"},
    )

    safe_env = backend._build_safe_env(spec)

    assert "LD_LIBRARY_PATH" not in safe_env
    assert safe_env["CUSTOM_VAR"] == "valid"

    cmd = backend.docker_client.build_run_command(
        container_name="test_sandbox_ld_lib",
        staged_dir=tmp_path,
        env_vars=safe_env,
    )
    assert not any("LD_LIBRARY_PATH" in arg for arg in cmd)
    assert any("CUSTOM_VAR=valid" in arg for arg in cmd)
    assert any("LD_LIBRARY_PATH" in d for d in backend.last_env_diagnostics)


def test_agent_spec_env_cannot_inject_ld_audit(tmp_path: Path) -> None:
    """Verify AgentSpec.env cannot inject LD_AUDIT into the generated Docker environment."""
    backend = DockerExecutionBackend()
    spec = AgentSpec(
        module_path="agent.py",
        entrypoint="run",
        env={"LD_AUDIT": "/workspace/audit_exploit.so"},
    )

    safe_env = backend._build_safe_env(spec)

    assert "LD_AUDIT" not in safe_env

    cmd = backend.docker_client.build_run_command(
        container_name="test_sandbox_ld_audit",
        staged_dir=tmp_path,
        env_vars=safe_env,
    )
    assert not any("LD_AUDIT" in arg for arg in cmd)
    assert any("LD_AUDIT" in d for d in backend.last_env_diagnostics)


def test_agent_spec_env_cannot_inject_loader_and_shell_hooks(tmp_path: Path) -> None:
    """Verify AgentSpec.env cannot inject runtime loader or shell execution hooks."""
    backend = DockerExecutionBackend()
    spec = AgentSpec(
        module_path="agent.py",
        entrypoint="run",
        env={
            "LD_DEBUG": "all",
            "PYTHONHOME": "/workspace/fake_stdlib",
            "PYTHONPATH": "/workspace/fake_site_packages",
            "BASH_ENV": "/workspace/evil_profile.sh",
            "ENV": "/workspace/evil_sh.sh",
            "PROMPT_COMMAND": "malicious_cmd",
            "SHELL": "/workspace/fake_shell",
            "IFS": ":",
            "RECOVERYBENCH_ISOLATED": "0",  # Cannot defeat harness status
            "VALID_AGENT_FLAG": "true",
        },
    )

    safe_env = backend._build_safe_env(spec)

    # Invariants in sanitized environment dictionary
    for blocked_key in (
        "LD_DEBUG",
        "PYTHONHOME",
        "PYTHONPATH",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "SHELL",
        "IFS",
    ):
        assert blocked_key not in safe_env

    # Mandatory harness variables cannot be overwritten
    assert safe_env["RECOVERYBENCH_ISOLATED"] == "1"
    assert safe_env["VALID_AGENT_FLAG"] == "true"

    cmd = backend.docker_client.build_run_command(
        container_name="test_sandbox_loader_hooks",
        staged_dir=tmp_path,
        env_vars=safe_env,
    )

    for blocked_key in (
        "LD_DEBUG",
        "PYTHONHOME",
        "PYTHONPATH",
        "BASH_ENV",
        "ENV",
        "PROMPT_COMMAND",
        "SHELL",
        "IFS",
    ):
        assert not any(f"{blocked_key}=" in arg for arg in cmd)

    assert any("VALID_AGENT_FLAG=true" in arg for arg in cmd)


def test_docker_rejects_reserved_env_when_configured_fail_closed() -> None:
    """Verify DockerExecutionBackend raises ConfigurationError when reject_reserved_env is True."""
    cfg = SandboxConfig(reject_reserved_env=True)
    backend = DockerExecutionBackend(sandbox_config=cfg)

    spec = AgentSpec(
        module_path="agent.py",
        entrypoint="run",
        env={"LD_PRELOAD": "/workspace/lib.so", "PATH": "/bad/path"},
    )

    with pytest.raises(ConfigurationError) as exc_info:
        backend._build_safe_env(spec)

    err_text = str(exc_info.value)
    assert "Security violation" in err_text
    assert "LD_PRELOAD" in err_text
    assert "PATH" in err_text
    assert "reserved/blocked" in err_text


def test_safe_custom_application_variables_work(tmp_path: Path) -> None:
    """Verify approved custom application variables are faithfully passed to Docker."""
    backend = DockerExecutionBackend()
    spec = AgentSpec(
        module_path="agent.py",
        entrypoint="run",
        env={
            "OPENAI_API_BASE": "http://127.0.0.1:8080/v1",
            "MODEL_NAME": "custom-recovery-v1",
            "AGENT_TIMEOUT_SECONDS": "15",
            "RETRY_BUDGET": "3",
        },
    )

    safe_env = backend._build_safe_env(spec)

    assert safe_env["OPENAI_API_BASE"] == "http://127.0.0.1:8080/v1"
    assert safe_env["MODEL_NAME"] == "custom-recovery-v1"
    assert safe_env["AGENT_TIMEOUT_SECONDS"] == "15"
    assert safe_env["RETRY_BUDGET"] == "3"

    cmd = backend.docker_client.build_run_command(
        container_name="test_sandbox_custom_vars",
        staged_dir=tmp_path,
        env_vars=safe_env,
    )

    assert any("OPENAI_API_BASE=http://127.0.0.1:8080/v1" in arg for arg in cmd)
    assert any("MODEL_NAME=custom-recovery-v1" in arg for arg in cmd)
    assert any("AGENT_TIMEOUT_SECONDS=15" in arg for arg in cmd)
    assert any("RETRY_BUDGET=3" in arg for arg in cmd)


def test_host_path_is_never_propagated(tmp_path: Path) -> None:
    """Verify host PATH is never inherited or passed into the container command."""
    backend = DockerExecutionBackend()
    spec = AgentSpec(module_path="agent.py", entrypoint="run")

    with patch.dict(os.environ, {"PATH": "C:\\Windows\\System32;C:\\Python311;/usr/bin"}):
        safe_env = backend._build_safe_env(spec)
        assert "PATH" not in safe_env

        cmd = backend.docker_client.build_run_command(
            container_name="test_sandbox_no_host_path",
            staged_dir=tmp_path,
            env_vars=safe_env,
        )
        assert not any(arg.startswith("PATH=") or arg == "PATH" or arg == "-e PATH=" for arg in cmd)


def test_centralized_reserved_env_policy_evaluation() -> None:
    """Verify is_reserved_or_blocked_env accurately identifies dangerous variables."""
    # Direct exact matches (case-insensitive)
    assert is_reserved_or_blocked_env("PATH") is True
    assert is_reserved_or_blocked_env("path") is True
    assert is_reserved_or_blocked_env("Path") is True
    assert is_reserved_or_blocked_env("LD_PRELOAD") is True
    assert is_reserved_or_blocked_env("ld_preload") is True
    assert is_reserved_or_blocked_env("LD_LIBRARY_PATH") is True
    assert is_reserved_or_blocked_env("LD_AUDIT") is True
    assert is_reserved_or_blocked_env("PYTHONHOME") is True
    assert is_reserved_or_blocked_env("PYTHONPATH") is True
    assert is_reserved_or_blocked_env("BASH_ENV") is True
    assert is_reserved_or_blocked_env("ENV") is True
    assert is_reserved_or_blocked_env("GCONV_PATH") is True
    assert is_reserved_or_blocked_env("GLIBC_TUNABLES") is True

    # Prefix matches
    assert is_reserved_or_blocked_env("LD_UNKNOWN_CUSTOM") is True
    assert is_reserved_or_blocked_env("DYLD_INSERT_LIBRARIES") is True
    assert is_reserved_or_blocked_env("RECOVERYBENCH_CUSTOM") is True

    # Safe application variables
    assert is_reserved_or_blocked_env("OPENAI_API_KEY") is False
    assert is_reserved_or_blocked_env("API_BASE_URL") is False
    assert is_reserved_or_blocked_env("AGENT_NAME") is False
    assert is_reserved_or_blocked_env("TIMEOUT_SECONDS") is False
    assert is_reserved_or_blocked_env("CUSTOM_PARAM") is False
