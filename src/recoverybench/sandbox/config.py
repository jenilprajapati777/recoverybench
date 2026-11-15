"""Configuration models for containerized sandbox execution."""

import re

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_RUNTIME_IMAGE_DIGEST = (
    "python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535"
)

# ==============================================================================
# Centralized Environment Security Policy
# ==============================================================================

# Variables that must NEVER be overridden or injected by AgentSpec.env or host
BLOCKED_ENV_VARS: frozenset[str] = frozenset({
    # System execution path (container image must preserve native system PATH)
    "PATH",
    # Dynamic loader & linker hijacking (glibc / musl / bionic)
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "LD_DEBUG",
    "LD_PROFILE",
    "LD_ORIGIN_PATH",
    "LD_DYNAMIC_WEAK",
    "LD_SHOW_AUXV",
    "LD_BIND_NOW",
    "LD_AOUT_LIBRARY_PATH",
    "LD_AOUT_PRELOAD",
    "GCONV_PATH",
    "GLIBC_TUNABLES",
    "HOSTALIASES",
    "RESOLV_HOST_CONF",
    # Python runtime control
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONEXECUTABLE",
    "PYTHONBREAKPOINT",
    # Shell execution hooks
    "BASH_ENV",
    "ENV",
    "PROMPT_COMMAND",
    "SHELLOPTS",
    "BASHOPTS",
    "SHELL",
    "IFS",
    # User / session identity
    "USER",
    "USERNAME",
    "LOGNAME",
    "HOME",
    # Internal harness indicators
    "RECOVERYBENCH_ISOLATED",
})

# Variable prefixes that are strictly prohibited in container environments
BLOCKED_ENV_PREFIXES: tuple[str, ...] = (
    "LD_",
    "DYLD_",
    "RECOVERYBENCH_",
)

# Mandatory runtime environment variables set by RecoveryBench
MANDATORY_CONTAINER_ENV: dict[str, str] = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "RECOVERYBENCH_ISOLATED": "1",
}

# Safe host environment variables permitted if present in host process
DEFAULT_HOST_ENV_ALLOWLIST: list[str] = [
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
]


def is_reserved_or_blocked_env(name: str) -> bool:
    """Check whether an environment variable name is reserved, prohibited, or dangerous.

    Evaluates exact match against BLOCKED_ENV_VARS and prefix matches against
    BLOCKED_ENV_PREFIXES case-insensitively.
    """
    clean = name.strip()
    upper = clean.upper()
    if upper in BLOCKED_ENV_VARS:
        return True
    return any(upper.startswith(prefix) for prefix in BLOCKED_ENV_PREFIXES)


class SandboxConfig(BaseModel):
    """Configuration for container execution security and resource limits."""

    runtime_image: str = Field(
        default=DEFAULT_RUNTIME_IMAGE_DIGEST,
        description="Docker container image with immutable SHA-256 digest used for agent execution.",
    )
    allow_mutable_image_tags: bool = Field(
        default=False,
        description="Permit mutable image tags (without @sha256: digest) for local development.",
    )
    memory_limit: str = Field(
        default="512m",
        description="Memory limit for the container (e.g. 256m, 512m, 1g).",
    )
    cpu_limit: str = Field(
        default="1.0",
        description="CPU limit for the container (e.g. 0.5, 1.0, 2.0).",
    )
    pids_limit: int = Field(
        default=64,
        ge=8,
        le=512,
        description="Maximum number of simultaneous processes in container to prevent fork-bombs.",
    )
    timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        le=300.0,
        description="Maximum execution timeout for container in wall-clock seconds.",
    )
    tmpfs_size: str = Field(
        default="64m",
        description="Size of the restricted temporary writable filesystem (/tmp).",
    )
    user: str = Field(
        default="nobody:nogroup",
        description="Non-root user (uid:gid or name) for container process execution.",
    )
    read_only_root: bool = Field(
        default=True,
        description="Enforce read-only root filesystem (--read-only).",
    )
    cap_drop: list[str] = Field(
        default_factory=lambda: ["ALL"],
        description="Linux capabilities to drop (--cap-drop=ALL).",
    )
    no_new_privileges: bool = Field(
        default=True,
        description="Prevent privilege escalation (--security-opt=no-new-privileges).",
    )
    env_allowlist: list[str] = Field(
        default_factory=lambda: list(DEFAULT_HOST_ENV_ALLOWLIST),
        description="Allowlist of host environment variables permitted to pass into container.",
    )
    reject_reserved_env: bool = Field(
        default=False,
        description="Raise ConfigurationError if AgentSpec.env contains reserved variables, instead of filtering with diagnostics.",
    )
    max_message_bytes: int = Field(
        default=1_048_576,  # 1 MB
        ge=4096,
        le=10_485_760,
        description="Maximum allowed size in bytes for a single IPC message.",
    )
    docker_binary: str = Field(
        default="docker",
        description="Executable name or absolute path for Docker CLI.",
    )

    @field_validator("runtime_image")
    @classmethod
    def _validate_image(cls, v: str) -> str:
        clean = v.strip()
        if not clean or any(c in clean for c in ";|&$`'\""):
            raise ValueError(f"Invalid characters in runtime_image: {v!r}")
        return clean

    @model_validator(mode="after")
    def _validate_digest_pinning(self) -> "SandboxConfig":
        if not self.allow_mutable_image_tags:
            match = re.search(r"@sha256:[a-fA-F0-9]{64}$", self.runtime_image)
            if not match:
                raise ValueError(
                    f"Security violation: Mutable runtime image tag '{self.runtime_image}' rejected. "
                    "Isolated container execution requires an immutable SHA-256 digest (e.g. 'image@sha256:<64-hex>'). "
                    "To allow mutable tags during development, set allow_mutable_image_tags=True."
                )
        return self

    @field_validator("memory_limit")
    @classmethod
    def _validate_memory(cls, v: str) -> str:
        clean = v.strip().lower()
        if not re.match(r"^\d+[kmg]$", clean):
            raise ValueError(f"Invalid memory limit format: {v!r}")
        num = int(clean[:-1])
        unit = clean[-1]
        mb = num if unit == "m" else (num * 1024 if unit == "g" else num / 1024)
        if mb < 32:
            raise ValueError(f"Memory limit {v!r} must be at least 32m")
        return clean

    @field_validator("cpu_limit")
    @classmethod
    def _validate_cpu(cls, v: str) -> str:
        clean = v.strip()
        try:
            val = float(clean)
            if val <= 0:
                raise ValueError
        except ValueError:
            raise ValueError(f"Invalid cpu limit: {v!r}") from None
        return clean

    @field_validator("docker_binary")
    @classmethod
    def _validate_binary(cls, v: str) -> str:
        clean = v.strip()
        if not clean or any(c in clean for c in ";|&$`'\""):
            raise ValueError(f"Invalid characters in docker_binary: {v!r}")
        return clean
