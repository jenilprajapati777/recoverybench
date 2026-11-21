"""Tests for public tool extension contracts and exports."""

from typing import Any

import pytest

import recoverybench as rb
from recoverybench import (
    DeclarativeToolService,
    IdempotencyContract,
    IdempotencyGuarantee,
    MockPaymentService,
    ToolInvocation,
    ToolRegistry,
    ToolResult,
    ToolService,
    mock_tool,
)


def test_public_tool_imports_from_root() -> None:
    """Verify that all tool extension contracts are importable from the package root."""
    assert rb.ToolRegistry is ToolRegistry
    assert rb.ToolService is ToolService
    assert rb.ToolInvocation is ToolInvocation
    assert rb.ToolResult is ToolResult
    assert rb.MockPaymentService is MockPaymentService
    assert rb.DeclarativeToolService is DeclarativeToolService
    assert rb.mock_tool is mock_tool


def test_tool_registry_input_validation() -> None:
    """Verify ToolRegistry validates inputs strictly."""
    registry = ToolRegistry()

    # Reject non-ToolService
    with pytest.raises(TypeError) as exc_info:
        registry.register("not_a_service")  # type: ignore[arg-type]
    assert "Expected ToolService instance" in str(exc_info.value)

    # Reject ToolService with empty name
    class EmptyNameTool(ToolService):
        name = ""

        def execute(
            self,
            action: str,
            params: dict[str, Any],
            idempotency_key: str | None = None,
            operation_id: str | None = None,
            attempt_id: str | None = None,
        ) -> ToolResult:
            return ToolResult(success=True)

        def reset(self) -> None:
            pass

    with pytest.raises(ValueError) as val_exc:
        registry.register(EmptyNameTool())
    assert "non-empty string 'name'" in str(val_exc.value)


def test_tool_registry_container_methods() -> None:
    """Verify ToolRegistry container ergonomics (__contains__, __len__, __iter__)."""
    registry = ToolRegistry()
    assert len(registry) == 0
    assert "payment_gateway" not in registry

    payment = MockPaymentService()
    registry.register(payment)

    assert len(registry) == 1
    assert "payment_gateway" in registry
    assert "  payment_gateway  " in registry  # whitespace tolerant lookup
    assert registry.get("payment_gateway") is payment

    # Iteration
    tools = list(registry)
    assert len(tools) == 1
    assert tools[0] is payment


def test_custom_tool_service_subclass() -> None:
    """Verify that a custom ToolService subclass works with RecoveryBench engine."""
    class CustomEchoService(ToolService):
        name = "echo_service"
        max_side_effect_capacity = 1
        is_runtime_enforced = True
        idempotency_contract = IdempotencyContract(
            guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
            verified_service_deduplication=True,
        )

        def __init__(self) -> None:
            self.invocations: list[dict[str, Any]] = []

        def verify_runtime_capacity_enforcement(self) -> bool:
            return True

        def execute(
            self,
            action: str,
            params: dict[str, Any],
            idempotency_key: str | None = None,
            operation_id: str | None = None,
            attempt_id: str | None = None,
        ) -> ToolResult:
            self.invocations.append({"action": action, "params": params})
            return ToolResult(
                success=True,
                data={"echo": params.get("msg", "")},
                committed=True,
                evidence_reference="echo_ref_01",
                effect_id="eff_echo_01",
            )

        def reset(self) -> None:
            self.invocations.clear()

    service = CustomEchoService()
    registry = ToolRegistry()
    registry.register(service)

    @rb.agent
    def echo_agent(task: rb.TaskContext, tool_call: rb.ToolCaller) -> rb.AgentResult:
        res = tool_call("echo_service", "echo", {"msg": "hello_world"})
        return rb.AgentResult(success=True, data=res)

    result = rb.test_agent(
        "payment_timeout_before_commit",  # built-in scenario targeting payment_gateway
        echo_agent,
        custom_tools=registry,
    )
    # The fault was for payment_gateway, so echo_service should execute cleanly
    assert result.passed
    assert len(service.invocations) == 1
    assert service.invocations[0]["params"] == {"msg": "hello_world"}
