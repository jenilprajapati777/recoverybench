"""Agent specification contract for container-isolated and external execution."""

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AgentSpec(BaseModel):
    """Specification of an agent under test for isolated execution."""

    name: str = Field(default="agent", description="Human-readable identifier for the agent.")
    module_path: str = Field(
        ...,
        description="Python module name (e.g. 'my_agent.core') or relative file path within workspace.",
    )
    entrypoint: str = Field(
        default="run_agent",
        description="Name of the callable function or AgentAdapter class within the module.",
    )
    workspace_dir: Path = Field(
        default_factory=lambda: Path("."),
        description="Host directory containing agent source code and assets to stage.",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Configuration parameters passed to the agent.",
    )
    dependencies: list[str] = Field(
        default_factory=list,
        description="Declared Python package dependencies required by the agent.",
    )
    lockfile: Path | None = Field(
        default=None,
        description="Optional path to dependency lockfile (e.g. requirements.txt or poetry.lock).",
    )
    runtime_image: str | None = Field(
        default=None,
        description="Optional custom prebuilt container image containing required agent dependencies.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Explicit, non-secret environment variables to pass to the worker process.",
    )
    files: list[str] | None = Field(
        default=None,
        description="Explicit list of files or directories required by the agent to stage from workspace.",
    )

    @field_validator("entrypoint")
    @classmethod
    def _validate_entrypoint(cls, v: str) -> str:
        clean = v.strip()
        if not clean.isidentifier():
            raise ValueError(f"Agent entrypoint must be a valid Python identifier, got {v!r}")
        return clean

    @field_validator("module_path")
    @classmethod
    def _validate_module_path(cls, v: str) -> str:
        clean = v.strip()
        if ".." in clean or clean.startswith("/") or clean.startswith("\\"):
            raise ValueError(f"Module path cannot contain directory traversal: {v!r}")
        return clean

    @field_validator("workspace_dir")
    @classmethod
    def _validate_workspace_dir(cls, v: Path) -> Path:
        resolved = Path(v).resolve()
        if not resolved.exists():
            raise ValueError(f"Workspace directory does not exist: {v}")
        if not resolved.is_dir():
            raise ValueError(f"Workspace path is not a directory: {v}")
        return resolved

    @field_validator("files")
    @classmethod
    def _validate_files(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        validated: list[str] = []
        for item in v:
            clean = item.strip()
            if not clean:
                continue
            if ".." in clean or clean.startswith("/") or clean.startswith("\\"):
                raise ValueError(
                    f"Declared file manifest item cannot contain directory traversal or absolute path: {item!r}"
                )
            validated.append(clean)
        return validated

    @classmethod
    def from_file(
        cls,
        file_path: str | Path,
        entrypoint: str = "run_agent",
        name: str | None = None,
        config: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        dependencies: list[str] | None = None,
        runtime_image: str | None = None,
        lockfile: str | Path | None = None,
        files: list[str] | None = None,
    ) -> "AgentSpec":
        """Construct an AgentSpec from a specific Python file."""
        path = Path(file_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"Agent source file not found: {file_path}")
        workspace = path.parent
        module_name = path.stem

        # Auto-detect requirements.txt in workspace if lockfile not explicitly specified
        resolved_lock = Path(lockfile).resolve() if lockfile else (workspace / "requirements.txt" if (workspace / "requirements.txt").exists() else None)

        return cls(
            name=name or module_name,
            module_path=module_name,
            entrypoint=entrypoint,
            workspace_dir=workspace,
            config=config or {},
            env=env or {},
            dependencies=dependencies or [],
            runtime_image=runtime_image,
            lockfile=resolved_lock,
            files=files,
        )

    @classmethod
    def from_callable(
        cls,
        func: Callable[..., Any],
        name: str | None = None,
        config: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        dependencies: list[str] | None = None,
        runtime_image: str | None = None,
        lockfile: str | Path | None = None,
        files: list[str] | None = None,
    ) -> "AgentSpec":
        """Construct an AgentSpec from an inspectable Python function or class."""
        try:
            source_file = inspect.getsourcefile(func)
        except (TypeError, OSError):
            source_file = None

        if not source_file or source_file.startswith("<"):
            raise ValueError(
                f"Cannot construct AgentSpec from in-memory or dynamic callable {func!r}. "
                "Isolated Docker execution requires an on-disk Python source file. "
                "Use AgentSpec.from_file(...) or run with --isolation trusted."
            )

        path = Path(source_file).resolve()
        workspace = path.parent
        module_name = path.stem
        entrypoint = str(getattr(func, "__name__", "run_agent"))
        agent_name = str(name or getattr(func, "agent_id", entrypoint))
        resolved_lock = Path(lockfile).resolve() if lockfile else (workspace / "requirements.txt" if (workspace / "requirements.txt").exists() else None)

        return cls(
            name=agent_name,
            module_path=module_name,
            entrypoint=entrypoint,
            workspace_dir=workspace,
            config=config or {},
            env=env or {},
            dependencies=dependencies or [],
            runtime_image=runtime_image,
            lockfile=resolved_lock,
            files=files,
        )
