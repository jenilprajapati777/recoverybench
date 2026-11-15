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
)
from recoverybench.sandbox.config import SandboxConfig
from recoverybench.sandbox.spec import AgentSpec

__all__ = [
    "AgentSpec",
    "DockerExecutionBackend",
    "ExecutionBackend",
    "InProcessExecutionBackend",
    "IsolationMode",
    "SandboxConfig",
]
