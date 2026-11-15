"""Base interfaces for mock tools and tool registry."""

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ToolInvocation(BaseModel):
    """Details of a tool call request."""

    tool_name: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None


class ToolResult(BaseModel):
    """Result of a tool execution."""

    success: bool
    data: dict[str, Any] = Field(default_factory=dict)
    committed: bool = False
    evidence_reference: str | None = None
    effect_id: str | None = None


class ToolService(ABC):
    """Abstract base class for all mock tool services."""

    name: str

    @abstractmethod
    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        """Execute the requested action against mock state."""
        raise NotImplementedError

    @abstractmethod
    def reset(self) -> None:
        """Reset internal mock state between runs."""
        raise NotImplementedError


class ToolRegistry:
    """Registry maintaining active mock tool services."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolService] = {}

    def register(self, service: ToolService) -> None:
        """Register a mock tool service."""
        self._tools[service.name] = service

    def get(self, name: str) -> ToolService | None:
        """Retrieve tool service by name."""
        return self._tools.get(name)

    def reset_all(self) -> None:
        """Reset all registered services."""
        for tool in self._tools.values():
            tool.reset()

    def list_names(self) -> list[str]:
        """List registered tool names."""
        return list(self._tools.keys())
