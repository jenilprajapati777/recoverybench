"""Base interfaces for mock tools and tool registry."""

import asyncio
import copy
import inspect
import math
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, cast

from pydantic import BaseModel, Field

from recoverybench.errors import ConcurrencyViolationError
from recoverybench.models import (
    CapacityProvenanceTier,
    HarnessRuntimeAttestation,
    IdempotencyContract,
    IdempotencyGuarantee,
    ToolExecutionModel,
)


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
    max_side_effect_capacity: int | float = math.inf
    capacity_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1
    declared_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1
    verified_provenance: CapacityProvenanceTier = CapacityProvenanceTier.TIER_C1
    is_runtime_enforced: bool = False
    idempotency_contract: IdempotencyContract = IdempotencyContract(
        guarantee=IdempotencyGuarantee.NONE,
        verified_service_deduplication=False,
    )
    harness_attestation: HarnessRuntimeAttestation | None = None
    execution_model: ToolExecutionModel = ToolExecutionModel.BOUNDED_SYNC
    timeout_seconds: float = 10.0
    declared_timeout_seconds: float | None = None
    is_quarantined: bool = False

    def quarantine(self) -> None:
        """Mark this tool quarantined to prevent future dispatches."""
        self.is_quarantined = True

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

    async def execute_async(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        """Asynchronously execute the requested action against mock state. Defaults to synchronous execute."""
        return self.execute(
            action=action,
            params=params,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
            attempt_id=attempt_id,
        )


class ToolRegistry:
    """Registry maintaining active mock tool services for scenario execution."""

    _run_bindings: dict[str, "ToolRegistry"] = {}
    _lock: threading.RLock = threading.RLock()

    def __init__(self) -> None:
        self._tools: dict[str, ToolService] = {}
        self._active_workers: int = 0
        self._quarantined: bool = False
        self._worker_lock: threading.Lock = threading.Lock()

    def worker_started(self) -> None:
        """Mark a tool invocation worker as active."""
        with self._worker_lock:
            self._active_workers += 1

    def worker_finished(self) -> None:
        """Mark a tool invocation worker as completed."""
        with self._worker_lock:
            self._active_workers = max(0, self._active_workers - 1)

    @property
    def active_workers(self) -> int:
        """Return the number of currently active tool invocation workers."""
        with self._worker_lock:
            return self._active_workers

    def quarantine(self, name: str | None = None) -> None:
        """Quarantine a specific tool or all registered tools."""
        if name is not None:
            tool = self.get(name)
            if tool:
                tool.quarantine()
        else:
            self._quarantined = True
            for tool in self._tools.values():
                tool.quarantine()

    def is_quarantined(self, name: str | None = None) -> bool:
        """Check if registry or a specific tool is quarantined."""
        if self._quarantined:
            return True
        if name is not None:
            tool = self.get(name)
            if tool and getattr(tool, "is_quarantined", False):
                return True
        return False

    def bind_to_run(self, run_id: str) -> None:
        """Explicitly bind this tool registry to a specific execution run ID.

        If run_id is already bound to this exact registry instance, the call is an
        idempotent no-op. If bound to a different registry instance, raises ValueError
        to prevent accidental replacement of an active run binding.
        """
        if not run_id or not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string.")
        key = run_id.strip()
        with ToolRegistry._lock:
            existing = ToolRegistry._run_bindings.get(key)
            if existing is not None and existing is not self:
                raise ValueError(
                    f"Run ID '{key}' is already bound to another ToolRegistry instance."
                )
            ToolRegistry._run_bindings[key] = self

    def unbind_run(self, run_id: str) -> None:
        """Unbind this registry from a specific execution run ID.

        Only removes the binding if it is currently bound to this registry instance.
        """
        if not run_id or not isinstance(run_id, str) or not run_id.strip():
            return
        key = run_id.strip()
        with ToolRegistry._lock:
            if ToolRegistry._run_bindings.get(key) is self:
                ToolRegistry._run_bindings.pop(key, None)

    @classmethod
    def get_for_run(cls, run_id: str | None) -> "ToolRegistry | None":
        """Retrieve the explicitly bound registry for a run ID."""
        if not run_id or not isinstance(run_id, str):
            return None
        with cls._lock:
            return cls._run_bindings.get(run_id.strip())

    @classmethod
    def clear_bindings(cls) -> None:
        """Clear all active run bindings (test cleanup)."""
        with cls._lock:
            cls._run_bindings.clear()

    def register(self, service: ToolService) -> None:
        """Register a mock tool service.

        Args:
            service: An instance of ToolService with a non-empty name attribute.

        Raises:
            TypeError: If service is not an instance of ToolService.
            ValueError: If service.name is empty or not a string.
        """
        if not isinstance(service, ToolService):
            raise TypeError(
                f"Expected ToolService instance, got {type(service).__name__}."
            )
        name = getattr(service, "name", None)
        if not name or not isinstance(name, str) or not name.strip():
            raise ValueError("ToolService must define a non-empty string 'name' attribute.")
        self._tools[name.strip()] = service

    def get(self, name: str) -> ToolService | None:
        """Retrieve tool service by name."""
        if not isinstance(name, str):
            return None
        return self._tools.get(name.strip())

    def reset_all(self) -> None:
        """Reset all registered services."""
        with self._worker_lock:
            if self._active_workers > 0:
                raise ConcurrencyViolationError(
                    f"Cannot reset tool registry: {self._active_workers} worker(s) still active."
                )
        for tool in self._tools.values():
            tool.reset()

    def list_names(self) -> list[str]:
        """List registered tool names."""
        return list(self._tools.keys())

    list_tools = list_names

    def __contains__(self, name: object) -> bool:
        if isinstance(name, str):
            return name.strip() in self._tools
        return False

    def __len__(self) -> int:
        return len(self._tools)

    def __iter__(self) -> Iterator[ToolService]:
        return iter(self._tools.values())


def _run_coroutine_sync(coro: Any) -> Any:
    """Safely run a coroutine synchronously regardless of whether an event loop is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    else:
        return asyncio.run(coro)


def _invoke_service_probe(
    service: ToolService,
    action: str,
    params: dict[str, Any],
    idempotency_key: str | None = None,
    operation_id: str | None = None,
    attempt_id: str | None = None,
) -> ToolResult:
    """Invoke tool probe handling both synchronous and asynchronous handlers safely."""
    is_async_handler = False
    if hasattr(service, "_handlers") and isinstance(service._handlers, dict):
        h = service._handlers.get(action)
        if inspect.iscoroutinefunction(h):
            is_async_handler = True

    if is_async_handler and hasattr(service, "execute_async"):
        coro = service.execute_async(
            action=action,
            params=params,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
            attempt_id=attempt_id,
        )
        return cast(ToolResult, _run_coroutine_sync(coro))

    try:
        res = service.execute(
            action=action,
            params=params,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
            attempt_id=attempt_id,
        )
        if inspect.iscoroutine(res):
            return cast(ToolResult, _run_coroutine_sync(res))
        return res
    except RuntimeError as re:
        if "asynchronous function" in str(re) and hasattr(service, "execute_async"):
            coro = service.execute_async(
                action=action,
                params=params,
                idempotency_key=idempotency_key,
                operation_id=operation_id,
                attempt_id=attempt_id,
            )
            return cast(ToolResult, _run_coroutine_sync(coro))
        raise


def demonstrate_runtime_capacity_enforcement(service: Any) -> bool:
    """Verifies whether a tool service genuinely demonstrates runtime capacity enforcement.

    To qualify for Tier C3 attestation, the service must provide verifiable runtime-enforced
    capacity and deduplication guarantees under actual execution. It fails closed to C1/C2 if
    it relies on inherited defaults, self-declared metadata, mere attribute presence, or
    self-authored verification methods.
    """
    if not isinstance(service, ToolService):
        return False

    # 1. Never grant based on unverified inherited defaults:
    cls = service.__class__
    if (
        getattr(cls, "declared_provenance", None) == CapacityProvenanceTier.TIER_C1
        and not getattr(cls, "is_runtime_enforced", False)
        and not getattr(service, "enforce_capacity", False)
    ):
        return False

    # Check whether the service is entirely read-only (all actions explicitly declare mutates=False)
    is_read_only = False
    if hasattr(service, "_handlers") and isinstance(service._handlers, dict):
        mutates_map = getattr(service, "_mutates", {})
        if mutates_map and not any(mutates_map.values()):
            is_read_only = True

    # 2. Strict Bounded Capacity Requirement
    cap = getattr(service, "max_side_effect_capacity", None)
    if is_read_only:
        cap = 0
    else:
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1 or math.isinf(cap):
            return False

    # 3. Idempotency Contract Requirement: STRICT_EXACTLY_ONCE and verified_service_deduplication
    contract = getattr(service, "idempotency_contract", None)
    if not isinstance(contract, IdempotencyContract):
        return False
    if contract.guarantee != IdempotencyGuarantee.STRICT_EXACTLY_ONCE:
        return False
    if not contract.verified_service_deduplication:
        return False

    # 4. Check executable capability
    executor = getattr(service, "execute", None)
    if not callable(executor) and not callable(getattr(service, "execute_async", None)):
        return False

    # 5. Isolated Runtime Execution Probe:
    # Snapshot all internal mutable attributes (excluding threading locks)
    snapshot: dict[str, Any] = {}
    for attr, val in list(service.__dict__.items()):
        if attr in ("_lock", "lock"):
            continue
        try:
            snapshot[attr] = copy.deepcopy(val)
        except Exception:
            try:
                snapshot[attr] = copy.copy(val)
            except Exception:
                snapshot[attr] = val

    try:
        probe_op_id = f"__c3_probe_op_{uuid.uuid4().hex}__"
        probe_key_1 = f"__c3_probe_k1_{uuid.uuid4().hex}__"

        # Determine probe action and parameters
        action = None
        params: dict[str, Any] = {}

        if hasattr(service, "_handlers") and isinstance(service._handlers, dict):
            mutates_map = getattr(service, "_mutates", {})
            sig_map = getattr(service, "_signatures", {})
            if is_read_only:
                action = next(iter(service._handlers.keys()))
                sig = sig_map.get(action)
                if sig and hasattr(sig, "parameters"):
                    for p_name, param in sig.parameters.items():
                        if param.default is inspect.Parameter.empty and param.kind not in (
                            inspect.Parameter.VAR_POSITIONAL,
                            inspect.Parameter.VAR_KEYWORD,
                        ):
                            params[p_name] = 1
            else:
                for act_name, is_mut in mutates_map.items():
                    if is_mut:
                        action = act_name
                        sig = sig_map.get(act_name)
                        if sig and hasattr(sig, "parameters"):
                            for p_name, param in sig.parameters.items():
                                if param.default is inspect.Parameter.empty and param.kind not in (
                                    inspect.Parameter.VAR_POSITIONAL,
                                    inspect.Parameter.VAR_KEYWORD,
                                ):
                                    params[p_name] = 1
                        break
                if action is None and service._handlers:
                    action = next(iter(service._handlers.keys()))
        elif hasattr(service, "charges") or getattr(service, "name", "") == "payment_gateway":
            action = "charge"
            params = {"amount": 1}
        else:
            action = "execute"

        if action is None:
            return False

        # Read-only service runtime enforcement probe: verifies zero persistent mutations
        if is_read_only:
            try:
                res1 = _invoke_service_probe(
                    service=service,
                    action=action,
                    params=params,
                    idempotency_key=probe_key_1,
                    operation_id=probe_op_id,
                )
            except Exception:
                return False
            if not getattr(res1, "success", False) or getattr(res1, "committed", True):
                return False

            try:
                res2 = _invoke_service_probe(
                    service=service,
                    action=action,
                    params=params,
                    idempotency_key=probe_key_1,
                    operation_id=probe_op_id,
                )
            except Exception:
                return False
            if not getattr(res2, "success", False) or getattr(res2, "committed", True):
                return False
            return True

        # --- Sub-test A: Duplicate Replay Deduplication Guarantee ---
        # First call: must commit exactly one effect
        try:
            res1 = _invoke_service_probe(
                service=service,
                action=action,
                params=params,
                idempotency_key=probe_key_1,
                operation_id=probe_op_id,
            )
        except Exception:
            return False

        if not getattr(res1, "success", False):
            return False
        if not getattr(res1, "committed", False):
            return False
        eff1 = getattr(res1, "effect_id", None)
        if not eff1:
            return False

        committed_effects: set[str] = {eff1}

        # Second call with SAME idempotency key: must NOT commit a new distinct effect
        try:
            res2 = _invoke_service_probe(
                service=service,
                action=action,
                params=params,
                idempotency_key=probe_key_1,
                operation_id=probe_op_id,
            )
        except Exception:
            return False

        if not getattr(res2, "success", False):
            return False

        eff2 = getattr(res2, "effect_id", None)
        # If res2 committed, it MUST have the identical effect identity (eff2 == eff1).
        # Producing multiple distinct effects for the same key is a deduplication failure!
        if getattr(res2, "committed", False):
            if eff2 != eff1:
                return False
        else:
            if eff2 and eff2 != eff1:
                return False

        # --- Sub-test B: Capacity Enforcement under Distinct Keys ---
        # Commit up to capacity M using distinct idempotency keys (1 commit already completed)
        for i in range(2, cap + 1):
            key_i = f"__c3_probe_k{i}_{uuid.uuid4().hex}__"
            try:
                res_i = _invoke_service_probe(
                    service=service,
                    action=action,
                    params=params,
                    idempotency_key=key_i,
                    operation_id=probe_op_id,
                )
                if not getattr(res_i, "success", False):
                    return False
                if getattr(res_i, "committed", False):
                    eff_i = getattr(res_i, "effect_id", None)
                    if eff_i:
                        committed_effects.add(eff_i)
                if len(committed_effects) > cap:
                    return False
            except Exception:
                return False

        # Now capacity M has been reached.
        # An attempt to commit with a DISTINCT key (M + 1) MUST NOT commit a new distinct effect!
        key_overflow = f"__c3_probe_overflow_{uuid.uuid4().hex}__"
        try:
            res_overflow = _invoke_service_probe(
                service=service,
                action=action,
                params=params,
                idempotency_key=key_overflow,
                operation_id=probe_op_id,
            )
            # If the service reports committed=True with a new distinct effect ID, capacity was breached!
            if getattr(res_overflow, "committed", False):
                eff_overflow = getattr(res_overflow, "effect_id", None)
                if eff_overflow and eff_overflow not in committed_effects:
                    return False
                if not eff_overflow:
                    return False
        except Exception:
            # Raising an error/exception on overflow is a valid form of capacity enforcement
            pass

        return True

    finally:
        # Restore snapshot to ensure probes are completely isolated and leave no side-effects
        for attr, val in snapshot.items():
            service.__dict__[attr] = val
