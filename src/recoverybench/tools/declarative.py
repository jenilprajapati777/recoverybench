"""Declarative mock tool registration and execution framework."""

import asyncio
import inspect
import threading
from collections.abc import Callable
from typing import Any, overload

from recoverybench.models import (
    CapacityProvenanceTier,
    IdempotencyContract,
    IdempotencyGuarantee,
    ToolExecutionModel,
)
from recoverybench.tools.base import ToolResult, ToolService


class DeclarativeToolService(ToolService):
    """A developer-friendly, declarative mock tool service.

    Manages the effect lifecycle, evidence generation, parameter validation,
    and idempotency bookkeeping automatically for user-defined mock functions.
    """

    def __init__(
        self,
        name: str,
        *,
        default_action: str | None = None,
        default_handler: Callable[..., Any] | None = None,
        default_mutates: bool = True,
        max_side_effect_capacity: int | float = 1,
        capacity_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1,
        idempotency_contract: IdempotencyContract | None = None,
        enforce_capacity: bool = True,
        execution_model: ToolExecutionModel | None = None,
        timeout_seconds: float = 10.0,
        declared_timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Tool name must be a non-empty string.")

        self.name: str = name.strip()
        self.enforce_capacity: bool = enforce_capacity
        self.max_side_effect_capacity: int | float = max_side_effect_capacity
        self.timeout_seconds: float = timeout_seconds
        self.declared_timeout_seconds: float | None = declared_timeout_seconds
        if execution_model is not None:
            self.execution_model = execution_model
        elif default_handler is not None and inspect.iscoroutinefunction(default_handler):
            self.execution_model = ToolExecutionModel.COROUTINE
        else:
            self.execution_model = ToolExecutionModel.BOUNDED_SYNC
        self.declared_provenance: CapacityProvenanceTier = (
            capacity_provenance
            if capacity_provenance != CapacityProvenanceTier.TIER_C1
            else (CapacityProvenanceTier.TIER_C3 if enforce_capacity else CapacityProvenanceTier.TIER_C1)
        )
        self.verified_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1
        self.capacity_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1
        self.is_runtime_enforced: bool = bool(enforce_capacity)
        self.harness_attestation = None
        self.idempotency_contract: IdempotencyContract = idempotency_contract or IdempotencyContract(
            guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
            verified_service_deduplication=True,
        )
        self._handlers: dict[str, Callable[..., Any]] = {}
        self._mutates: dict[str, bool] = {}
        self._signatures: dict[str, inspect.Signature] = {}
        self._primary_action: str | None = default_action
        self._lock: threading.RLock = threading.RLock()

        # State tracking for effect lifecycle & idempotency
        # Cache key: (operation_id, action, idempotency_key)
        self._counter: int = 0
        self._history: list[dict[str, Any]] = []
        self._idempotency_cache: dict[tuple[str | None, str, str], dict[str, Any]] = {}

        if default_handler is not None:
            action_name = str(default_action or getattr(default_handler, "__name__", "execute"))
            self.register_action(action_name, default_handler, mutates=default_mutates)
            self._primary_action = action_name

    def register_action(
        self,
        action: str,
        handler: Callable[..., Any],
        *,
        mutates: bool = True,
    ) -> None:
        """Register a handler function for an action on this tool."""
        if not isinstance(action, str) or not action.strip():
            raise ValueError("Action name must be a non-empty string.")
        if not callable(handler):
            raise TypeError(f"Handler for action '{action}' must be callable, got {type(handler).__name__}.")

        clean_action = action.strip()
        self._handlers[clean_action] = handler
        self._mutates[clean_action] = mutates
        try:
            self._signatures[clean_action] = inspect.signature(handler)
        except (ValueError, TypeError):
            self._signatures[clean_action] = inspect.Signature()

        if self._primary_action is None:
            self._primary_action = clean_action

    def action(
        self,
        name: str | None = None,
        *,
        mutates: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator to add an action to this tool service."""
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            action_name = str(name or getattr(fn, "__name__", "execute"))
            self.register_action(action_name, fn, mutates=mutates)
            return fn

        return decorator

    def reset(self) -> None:
        """Reset internal mock history, idempotency cache, and counters."""
        with self._lock:
            self._counter = 0
            self._history.clear()
            self._idempotency_cache.clear()

    def _resolve_handler(self, action: str) -> tuple[str, Callable[..., Any], bool, inspect.Signature]:
        """Resolve handler and metadata for a given action string."""
        clean_action = action.strip() if action else ""

        if clean_action in self._handlers:
            return clean_action, self._handlers[clean_action], self._mutates[clean_action], self._signatures[clean_action]

        # If action is empty or generic, fall back to primary action if available
        if (not clean_action or clean_action in ("default", "execute", "run")) and self._primary_action:
            primary = self._primary_action
            return primary, self._handlers[primary], self._mutates[primary], self._signatures[primary]

        avail = sorted(self._handlers.keys())
        raise ValueError(
            f"Action '{action}' is not supported by tool '{self.name}'. "
            f"Available actions: {avail}"
        )

    def _validate_and_bind_params(
        self,
        sig: inspect.Signature,
        params: dict[str, Any],
        action: str,
    ) -> dict[str, Any]:
        """Validate input parameters against the handler signature."""
        if not sig.parameters:
            return params

        try:
            bound = sig.bind(**params)
            bound.apply_defaults()
            return bound.arguments
        except TypeError as exc:
            raise TypeError(
                f"Invalid arguments for tool '{self.name}.{action}': {exc}"
            ) from exc

    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        """Synchronously execute the mock tool action with automatic effect management."""
        resolved_action, handler, mutates, sig = self._resolve_handler(action)
        bound_args = self._validate_and_bind_params(sig, params, resolved_action)

        # 1. Idempotency Check for mutating operations: scoped to (operation_id, action, idempotency_key)
        if mutates and idempotency_key:
            clean_op_id = operation_id.strip() if (operation_id and operation_id.strip()) else None
            cache_key = (clean_op_id, resolved_action, idempotency_key)
            with self._lock:
                if cache_key in self._idempotency_cache:
                    cached = self._idempotency_cache[cache_key]
                    replay_data = dict(cached["data"])
                    replay_data["idempotent_replay"] = True
                    return ToolResult(
                        success=cached["success"],
                        data=replay_data,
                        committed=False,  # Replay of prior commit; zero duplicate side-effects
                        evidence_reference=cached["evidence_reference"],
                        effect_id=cached["effect_id"],
                    )

        # 2. Invoke Handler
        if inspect.iscoroutinefunction(handler):
            raise RuntimeError(
                f"Tool '{self.name}.{resolved_action}' is an asynchronous function ('async def'). "
                f"It cannot be executed in synchronous mode. Use test_agent_async() instead."
            )

        raw_result = handler(**bound_args)
        return self._build_tool_result(
            raw_result=raw_result,
            action=resolved_action,
            mutates=mutates,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
            attempt_id=attempt_id,
        )

    async def execute_async(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        """Asynchronously execute the mock tool action with automatic effect management."""
        resolved_action, handler, mutates, sig = self._resolve_handler(action)
        bound_args = self._validate_and_bind_params(sig, params, resolved_action)

        # 1. Idempotency Check: scoped to (operation_id, action, idempotency_key)
        if mutates and idempotency_key:
            clean_op_id = operation_id.strip() if (operation_id and operation_id.strip()) else None
            cache_key = (clean_op_id, resolved_action, idempotency_key)
            with self._lock:
                if cache_key in self._idempotency_cache:
                    cached = self._idempotency_cache[cache_key]
                    replay_data = dict(cached["data"])
                    replay_data["idempotent_replay"] = True
                    return ToolResult(
                        success=cached["success"],
                        data=replay_data,
                        committed=False,
                        evidence_reference=cached["evidence_reference"],
                        effect_id=cached["effect_id"],
                    )

        # 2. Invoke Handler (sync or async)
        if inspect.iscoroutinefunction(handler):
            raw_result = await handler(**bound_args)
        elif asyncio.iscoroutine(handler):
            raw_result = await handler
        else:
            raw_result = handler(**bound_args)

        return self._build_tool_result(
            raw_result=raw_result,
            action=resolved_action,
            mutates=mutates,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
            attempt_id=attempt_id,
        )

    def _build_tool_result(
        self,
        raw_result: Any,
        action: str,
        mutates: bool,
        idempotency_key: str | None,
        operation_id: str | None,
        attempt_id: str | None,
    ) -> ToolResult:
        """Normalize function return value and build ToolResult with effect bookkeeping."""
        data: dict[str, Any]
        if isinstance(raw_result, dict):
            data = dict(raw_result)
        elif raw_result is None:
            data = {"status": "ok"}
        else:
            data = {"result": raw_result}

        if not mutates:
            # Read-only action: no persistent side-effect committed
            return ToolResult(
                success=True,
                data=data,
                committed=False,
                evidence_reference=None,
                effect_id=None,
            )

        with self._lock:
            # Capacity Enforcement: if enabled, reject when limit is reached
            if getattr(self, "enforce_capacity", False):
                if attempt_id:
                    current_count = sum(1 for h in self._history if h.get("attempt_id") == attempt_id)
                elif operation_id and operation_id.startswith("__c3_probe_"):
                    current_count = sum(1 for h in self._history if h.get("operation_id") == operation_id)
                else:
                    current_count = 0

                if current_count >= self.max_side_effect_capacity:
                    return ToolResult(
                        success=False,
                        data={"error": f"Capacity limit {self.max_side_effect_capacity} reached"},
                        committed=False,
                        evidence_reference=None,
                        effect_id=None,
                    )

            # Mutating action: generate deterministic evidence reference and effect ID
            self._counter += 1
            evidence_reference = f"{self.name}_{action}_{self._counter:04d}"
            effect_id = f"eff_{evidence_reference}"

            record = {
                "success": True,
                "data": data,
                "evidence_reference": evidence_reference,
                "effect_id": effect_id,
                "idempotency_key": idempotency_key,
                "operation_id": operation_id,
                "attempt_id": attempt_id,
            }
            self._history.append(record)
            if idempotency_key:
                clean_op_id = operation_id.strip() if (operation_id and operation_id.strip()) else None
                cache_key = (clean_op_id, action, idempotency_key)
                self._idempotency_cache[cache_key] = record

            data_with_flag = dict(data)
            data_with_flag.setdefault("idempotent_replay", False)

            return ToolResult(
                success=True,
                data=data_with_flag,
                committed=True,
                evidence_reference=evidence_reference,
                effect_id=effect_id,
            )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Allow direct invocation of the primary action handler."""
        if not self._primary_action or self._primary_action not in self._handlers:
            raise TypeError(f"ToolService '{self.name}' has no registered actions to call directly.")
        handler = self._handlers[self._primary_action]
        return handler(*args, **kwargs)


@overload
def mock_tool(
    name: Callable[..., Any],
    *,
    action: str | None = None,
    mutates: bool = True,
    max_side_effect_capacity: int | float = 1,
    capacity_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1,
    idempotency_contract: IdempotencyContract | None = None,
    enforce_capacity: bool = True,
    execution_model: ToolExecutionModel | None = None,
) -> DeclarativeToolService: ...


@overload
def mock_tool(
    name: str | None = None,
    *,
    action: str | None = None,
    mutates: bool = True,
    max_side_effect_capacity: int | float = 1,
    capacity_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1,
    idempotency_contract: IdempotencyContract | None = None,
    enforce_capacity: bool = True,
    execution_model: ToolExecutionModel | None = None,
) -> Callable[[Callable[..., Any]], DeclarativeToolService]: ...


def mock_tool(
    name: str | Callable[..., Any] | None = None,
    *,
    action: str | None = None,
    mutates: bool = True,
    max_side_effect_capacity: int | float = 1,
    capacity_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1,
    idempotency_contract: IdempotencyContract | None = None,
    enforce_capacity: bool = True,
    execution_model: ToolExecutionModel | None = None,
) -> Any:
    """Decorator to declare a custom mock tool service.

    Usage:
        @rb.mock_tool(name="payment_gateway_charge")
        def charge(amount: float, customer_id: str):
            return {"status": "success", "amount": amount}

        @rb.mock_tool("inventory_service", action="reserve", mutates=True)
        def reserve_stock(sku: str, quantity: int = 1):
            return {"reserved": True, "sku": sku, "quantity": quantity}
    """
    def decorator(fn: Callable[..., Any]) -> DeclarativeToolService:
        tool_name = str(name if isinstance(name, str) else getattr(fn, "__name__", "mock_tool"))
        action_name = str(action if isinstance(action, str) else getattr(fn, "__name__", "execute"))
        return DeclarativeToolService(
            name=tool_name,
            default_action=action_name,
            default_handler=fn,
            default_mutates=mutates,
            max_side_effect_capacity=max_side_effect_capacity,
            capacity_provenance=capacity_provenance,
            idempotency_contract=idempotency_contract,
            enforce_capacity=enforce_capacity,
            execution_model=execution_model,
        )

    if callable(name):
        return decorator(name)

    return decorator
