"""Subprocess wrapper for Docker Engine commands with strict security validation."""

import os
import shutil
import subprocess
from pathlib import Path

from recoverybench.sandbox.config import SandboxConfig


class DockerClient:
    """Safely executes Docker CLI operations without shell=True."""

    def __init__(self, config: SandboxConfig | None = None) -> None:
        self.config = config or SandboxConfig()

    def find_docker_executable(self) -> str | None:
        """Find the Docker executable on PATH or via configured path."""
        binary = self.config.docker_binary
        if os.path.isabs(binary):
            return binary if os.path.isfile(binary) and os.access(binary, os.X_OK) else None
        return shutil.which(binary)

    def is_docker_available(self) -> tuple[bool, str]:
        """Check if Docker CLI is installed and daemon is responsive."""
        exe = self.find_docker_executable()
        if not exe:
            return False, "Docker CLI executable not found on PATH"

        try:
            res = subprocess.run(
                [exe, "info", "--format", "{{.OSType}}"],
                capture_output=True,
                text=True,
                timeout=10.0,
                check=False,
            )
            if res.returncode != 0:
                err_detail = res.stderr.strip() or res.stdout.strip()
                return False, f"Docker daemon unreachable: {err_detail}"
            os_type = res.stdout.strip()
            return True, f"Docker available (Daemon OS: {os_type})"
        except subprocess.TimeoutExpired:
            return False, "Docker daemon connection timed out after 10 seconds"
        except Exception as exc:
            return False, f"Failed to execute docker info: {exc}"

    def check_image_exists(self, image_name: str) -> bool:
        """Verify whether the requested container image is cached locally."""
        exe = self.find_docker_executable()
        if not exe:
            return False

        # Try exact reference first
        candidates = [image_name]
        # If reference contains tag and digest (e.g. python:3.11-slim@sha256:...),
        # also check digest-only (python@sha256:...) and repo:tag candidates
        if "@" in image_name and ":" in image_name.split("@")[0]:
            repo_with_tag, digest = image_name.split("@", 1)
            repo_base = repo_with_tag.split(":", 1)[0]
            candidates.append(f"{repo_base}@{digest}")

        for candidate in candidates:
            try:
                res = subprocess.run(
                    [exe, "image", "inspect", candidate],
                    capture_output=True,
                    text=True,
                    timeout=10.0,
                    check=False,
                )
                if res.returncode == 0:
                    return True
            except Exception:
                continue
        return False

    def build_run_command(
        self,
        container_name: str,
        staged_dir: Path | None = None,
        env_vars: dict[str, str] | None = None,
        custom_config: SandboxConfig | None = None,
        *,
        staged_workspace: Path | None = None,
        env: dict[str, str] | None = None,
        worker_script_path: Path | None = None,
    ) -> list[str]:
        """Construct the hardened docker run command argument list.

        Security flags:
        - --network=none
        - --read-only
        - --cap-drop=ALL
        - --security-opt=no-new-privileges
        - --pids-limit
        - --memory
        - --cpus
        - --user
        - --tmpfs /tmp:rw,noexec,nosuid
        - Read-only workspace volume mount
        """
        cfg = custom_config or self.config
        exe = self.find_docker_executable() or cfg.docker_binary

        target_dir = staged_dir or staged_workspace
        if target_dir is None:
            raise ValueError("staged_dir or staged_workspace must be provided")

        effective_env = dict(env_vars or {})
        if env:
            effective_env.update(env)

        # Format staged_dir for Docker volume mount (handling Windows slashes)
        mount_src = str(target_dir.resolve()).replace("\\", "/")

        cmd = [
            exe,
            "run",
            "-i",  # Interactive mode to keep stdin open for IPC
            "--name",
            container_name,
            "--network=none",  # Strict network isolation: loopback only
            "--read-only",  # Read-only root filesystem
            "--cap-drop=ALL",  # Drop all Linux capabilities
            "--security-opt=no-new-privileges",  # Prevent privilege escalation
            f"--pids-limit={cfg.pids_limit}",
            f"--memory={cfg.memory_limit}",
            f"--cpus={cfg.cpu_limit}",
            f"--user={cfg.user}",
            f"--tmpfs=/tmp:rw,noexec,nosuid,size={cfg.tmpfs_size}",
            "-v",
            f"{mount_src}:/workspace:ro",  # Mount staged workspace read-only
            "-w",
            "/workspace",
        ]

        if worker_script_path:
            worker_src = str(worker_script_path.resolve()).replace("\\", "/")
            cmd.extend(["-v", f"{worker_src}:/worker.py:ro"])

        # Pass approved environment variables
        for k, v in effective_env.items():
            cmd.extend(["-e", f"{k}={v}"])

        # Container image and worker entrypoint
        worker_target = "/worker.py" if worker_script_path else "/workspace/_recoverybench_worker.py"
        cmd.extend([
            cfg.runtime_image,
            "python",
            "-u",
            worker_target,
        ])

        return cmd

    def kill_and_remove(self, container_name: str, timeout: float = 5.0) -> bool:
        """Deterministically force-kill and remove a container."""
        exe = self.find_docker_executable()
        if not exe:
            return False

        try:
            res = subprocess.run(
                [exe, "rm", "-f", container_name],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            return res.returncode == 0
        except Exception:
            return False
