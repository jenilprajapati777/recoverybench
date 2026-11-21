"""Sandbox and execution backend package for RecoveryBench.

Provides modular execution backends for running agents under test:
- InProcessExecutionBackend: Trusted in-process execution with explicit security warnings.
- DockerExecutionBackend: Operating-system-level containerized isolation with --network=none.
"""

from recoverybench.sandbox.backend import (
    DockerExecutionBackend,
    ExecutionBackend,
    InProcessExecutionBackend,
    IsolationMode,
    ProcessIsolatedExecutionBackend,
)
from recoverybench.sandbox.config import (
    BLOCKED_ENV_PREFIXES,
    BLOCKED_ENV_VARS,
    MANDATORY_CONTAINER_ENV,
    SandboxConfig,
    is_reserved_or_blocked_env,
)
from recoverybench.sandbox.spec import AgentSpec

__all__ = [
    "AgentSpec",
    "BLOCKED_ENV_PREFIXES",
    "BLOCKED_ENV_VARS",
    "DockerExecutionBackend",
    "ExecutionBackend",
    "InProcessExecutionBackend",
    "ProcessIsolatedExecutionBackend",
    "IsolationMode",
    "MANDATORY_CONTAINER_ENV",
    "SandboxConfig",
    "is_reserved_or_blocked_env",
]
