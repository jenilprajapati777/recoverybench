"""Execution engine executing scenarios against agent adapters."""

import contextlib
import time
import uuid
from collections.abc import Callable, Generator
from typing import Any

from recoverybench.adapters.callable import wrap_agent
from recoverybench.agent.base import AgentAdapter, AgentResult, TaskContext, ToolClient
from recoverybench.clock import VirtualClock
from recoverybench.config import get_settings
from recoverybench.engine.context import RunResult
from recoverybench.errors import (
    CircuitBreakerError,
    ConfigurationError,
    InfrastructureError,
    ToolError,
)
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.faults.injector import FaultInjector
from recoverybench.ledger.base import Ledger
from recoverybench.models import (
    CommitStatus,
    EvaluationAssertion,
    EvaluationResult,
    EvaluationVerdict,
    ExecutionStatus,
    LedgerEvent,
    RunSummary,
    compute_request_fingerprint,
    hash_token,
)
from recoverybench.sandbox.backend import (
    ExecutionBackend,
    InProcessExecutionBackend,
)
from recoverybench.sandbox.spec import AgentSpec
from recoverybench.scenarios.schema import ScenarioModel
from recoverybench.security import mask_string_secrets
from recoverybench.tools.base import ToolRegistry


class BoundToolClient(ToolClient):
    """Execution engine implementation of the ToolClient interface.

    Allows agents to use either tool.call(...) or tool(...) interchangeably,
    and supports scoped operation boundaries via `with tool.operation(op_id):`.
    """

    def __init__(
        self,
        dispatcher: Callable[..., dict[str, Any]],
        default_operation_id: str | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._default_operation_id = default_operation_id
        self._scoped_operation_id: str | None = None

    @contextlib.contextmanager
    def operation(self, operation_id: str) -> Generator[str, None, None]:
        """Establish an active operation_id context scope for tool calls.

        Ensures all invocations and retries within this block share the same
        operation_id, while distinct blocks receive distinct operation_ids.
        """
        if not operation_id or not operation_id.strip():
            raise ValueError("operation_id must be a non-empty string.")
        prev = self._scoped_operation_id
        self._scoped_operation_id = operation_id.strip()
        try:
            yield self._scoped_operation_id
        finally:
            self._scoped_operation_id = prev

    @property
    def current_operation_id(self) -> str | None:
        """Return the currently active operation_id in scope, if any."""
        return self._scoped_operation_id or self._default_operation_id

    def call(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch a tool call with operation and idempotency arguments."""
        effective_op_id = (
            operation_id.strip()
            if (operation_id and operation_id.strip())
            else self.current_operation_id
        )
        return self._dispatcher(tool_name, action, params, idempotency_key, effective_op_id)

    def __call__(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Callable dispatch protocol for backwards compatibility with tool_call(...)."""
        effective_op_id = (
            operation_id.strip()
            if (operation_id and operation_id.strip())
            else self.current_operation_id
        )
        return self._dispatcher(tool_name, action, params, idempotency_key, effective_op_id)


class ExecutionEngine:
    """Coordinates deterministic fault injection, tool dispatch, and evaluation."""

    def __init__(
        self,
        ledger: Ledger,
        tool_registry: ToolRegistry,
        clock: VirtualClock | None = None,
        max_attempts: int | None = None,
        backend: ExecutionBackend | None = None,
    ) -> None:
        self.ledger = ledger
        self.tool_registry = tool_registry
        self.clock = clock or VirtualClock()
        self.max_attempts = max_attempts or get_settings().max_attempts
        self.backend = backend or InProcessExecutionBackend()
        self.evaluator = RecoveryEvaluator()

    def run(
        self,
        scenario: ScenarioModel,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        run_id: str | None = None,
        backend: ExecutionBackend | None = None,
    ) -> RunResult:
        """Execute a full scenario run against an agent adapter, spec, or callable."""
        active_backend = backend or self.backend
        active_isolation_mode = active_backend.mode.value

        resolved_agent: AgentAdapter | AgentSpec
        if isinstance(agent, (AgentAdapter, AgentSpec)):
            resolved_agent = agent
        else:
            resolved_agent = wrap_agent(agent)

        agent_id = getattr(resolved_agent, "agent_id", getattr(resolved_agent, "name", "agent"))
        active_run_id = run_id or f"run_{uuid.uuid4().hex[:12]}"
        # Ensure fresh state for this run_id
        self.ledger.clear_run(active_run_id)
        fault_injector = FaultInjector(faults=scenario.faults, seed=scenario.seed)
        total_invocations = 0
        wall_clock_start = time.perf_counter()

        # Reset tools state for clean run
        self.tool_registry.reset_all()

        # Build Task Context with established operation_id boundary
        is_single_op = scenario.is_single_operation
        default_op_id: str | None = None

        if is_single_op:
            if "operation_id" in scenario.task:
                default_op_id = scenario.task["operation_id"]
            else:
                default_op_id = f"op_{scenario.scenario_id}"
        else:
            # Multi-operation scenario: No scenario-level default allowed!
            # Every logical business operation must establish its own distinct operation_id.
            default_op_id = None

        task_context = TaskContext(
            scenario_id=scenario.scenario_id,
            task_params=scenario.task,
            clock=self.clock,
            operation_id=default_op_id,
        )

        # Tool dispatch callback provided to agent
        def tool_dispatcher(
            tool_name: str,
            action: str,
            params: dict[str, Any],
            idempotency_key: str | None = None,
            operation_id: str | None = None,
        ) -> dict[str, Any]:
            nonlocal total_invocations
            total_invocations += 1

            if total_invocations > self.max_attempts:
                raise CircuitBreakerError(total_invocations, self.max_attempts)

            tool = self.tool_registry.get(tool_name)
            if not tool:
                raise ConfigurationError(
                    f"Tool '{tool_name}' requested by agent is not registered in mock registry."
                )

            # Establish unique attempt_id and effective operation_id
            attempt_id = f"att_{active_run_id}_{total_invocations}"
            clean_op_id = operation_id.strip() if (operation_id and operation_id.strip()) else None
            effective_op_id = clean_op_id if clean_op_id is not None else task_context.operation_id

            attempt = fault_injector.register_attempt(tool_name, action)
            request_fp = compute_request_fingerprint(tool_name, action, params)
            key_hash = hash_token(idempotency_key)
            sim_ts = self.clock.now_iso()

            # 1. Pre-Commit Fault Check (timeout before commit, 429, 500, etc.)
            pre_commit_fault = fault_injector.evaluate_pre_commit(tool_name, action, attempt)
            if pre_commit_fault:
                event = LedgerEvent(
                    run_id=active_run_id,
                    scenario_id=scenario.scenario_id,
                    tool_name=tool_name,
                    action_type=action,
                    operation_id=effective_op_id,
                    attempt_id=attempt_id,
                    effect_id=None,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=request_fp,
                    attempt_number=attempt,
                    execution_status=ExecutionStatus.FAULT_INJECTED,
                    commit_status=CommitStatus.NOT_APPLICABLE,
                    simulated_timestamp=sim_ts,
                    error_type=type(pre_commit_fault).__name__,
                    details={"params_keys": list(params.keys())},
                )
                self.ledger.append(event)
                self.clock.advance(0.1)  # Minimal latency advance
                raise pre_commit_fault

            # 2. Tool Mutation in Mock Service
            try:
                tool_result = tool.execute(
                    action,
                    params,
                    idempotency_key=idempotency_key,
                    operation_id=effective_op_id,
                    attempt_id=attempt_id,
                )
            except TypeError:
                tool_result = tool.execute(action, params, idempotency_key=idempotency_key)

            # 3. Duplicate Delivery Check (simulates at-least-once queue/network redelivery)
            is_dup, dup_count = fault_injector.is_duplicate_delivery(tool_name, action, attempt)
            if is_dup:
                for dup_idx in range(1, dup_count + 1):
                    dup_attempt_id = f"{attempt_id}_dup{dup_idx}"
                    try:
                        dup_result = tool.execute(
                            action,
                            params,
                            idempotency_key=idempotency_key,
                            operation_id=effective_op_id,
                            attempt_id=dup_attempt_id,
                        )
                    except TypeError:
                        dup_result = tool.execute(action, params, idempotency_key=idempotency_key)
                    dup_event = LedgerEvent(
                        run_id=active_run_id,
                        scenario_id=scenario.scenario_id,
                        tool_name=tool_name,
                        action_type=action,
                        operation_id=effective_op_id,
                        attempt_id=dup_attempt_id,
                        effect_id=dup_result.effect_id,
                        idempotency_key_hash=key_hash,
                        request_fingerprint=request_fp,
                        attempt_number=attempt,
                        execution_status=ExecutionStatus.SUCCESS,
                        commit_status=CommitStatus.COMMITTED if dup_result.committed else CommitStatus.NOT_APPLICABLE,
                        simulated_timestamp=self.clock.now_iso(),
                        evidence_reference=dup_result.evidence_reference,
                        details={"duplicate_delivery": True, "duplicate_index": dup_idx},
                    )
                    self.ledger.append(dup_event)

            # 4. Post-Commit Fault Check (e.g. timeout after commit)
            post_commit_fault = fault_injector.evaluate_post_commit(tool_name, action, attempt)
            if post_commit_fault:
                event = LedgerEvent(
                    run_id=active_run_id,
                    scenario_id=scenario.scenario_id,
                    tool_name=tool_name,
                    action_type=action,
                    operation_id=effective_op_id,
                    attempt_id=attempt_id,
                    effect_id=tool_result.effect_id,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=request_fp,
                    attempt_number=attempt,
                    execution_status=ExecutionStatus.FAULT_INJECTED,
                    commit_status=CommitStatus.COMMITTED if tool_result.committed else CommitStatus.NOT_APPLICABLE,
                    simulated_timestamp=sim_ts,
                    error_type=type(post_commit_fault).__name__,
                    evidence_reference=tool_result.evidence_reference,
                    details={"params_keys": list(params.keys())},
                )
                self.ledger.append(event)
                self.clock.advance(0.2)
                raise post_commit_fault

            # 5. Malformed Payload Check (returns corrupted dictionary instead of expected schema)
            corrupted_data = fault_injector.evaluate_malformed_payload(tool_name, action, attempt, tool_result.data)
            if corrupted_data is not None:
                event = LedgerEvent(
                    run_id=active_run_id,
                    scenario_id=scenario.scenario_id,
                    tool_name=tool_name,
                    action_type=action,
                    operation_id=effective_op_id,
                    attempt_id=attempt_id,
                    effect_id=tool_result.effect_id,
                    idempotency_key_hash=key_hash,
                    request_fingerprint=request_fp,
                    attempt_number=attempt,
                    execution_status=ExecutionStatus.FAULT_INJECTED,
                    commit_status=CommitStatus.COMMITTED if tool_result.committed else CommitStatus.NOT_APPLICABLE,
                    simulated_timestamp=sim_ts,
                    error_type="MalformedResponseError",
                    evidence_reference=tool_result.evidence_reference,
                    details={"malformed_payload": True},
                )
                self.ledger.append(event)
                self.clock.advance(0.1)
                return corrupted_data

            # 6. Successful Execution
            event = LedgerEvent(
                run_id=active_run_id,
                scenario_id=scenario.scenario_id,
                tool_name=tool_name,
                action_type=action,
                operation_id=effective_op_id,
                attempt_id=attempt_id,
                effect_id=tool_result.effect_id,
                idempotency_key_hash=key_hash,
                request_fingerprint=request_fp,
                attempt_number=attempt,
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED if tool_result.committed else CommitStatus.NOT_APPLICABLE,
                simulated_timestamp=sim_ts,
                evidence_reference=tool_result.evidence_reference,
                details={"params_keys": list(params.keys())},
            )
            self.ledger.append(event)
            self.clock.advance(0.1)
            return tool_result.data

        # Client interface supporting callable, tool.call, and tool.operation protocols
        tool_client = BoundToolClient(tool_dispatcher, default_operation_id=default_op_id)

        # Execute Agent via selected ExecutionBackend
        agent_result: AgentResult
        try:
            agent_result = active_backend.run(
                agent=resolved_agent,
                task_context=task_context,
                tool_client=tool_client,
                timeout_seconds=get_settings().default_timeout_seconds,
            )
        except CircuitBreakerError as cbe:
            agent_result = AgentResult(
                success=False,
                error=str(cbe),
                attempts=total_invocations,
                status_message="Halted by harness circuit breaker",
            )
        except Exception as exc:
            # Check if it was an unhandled tool error or unexpected crash
            if isinstance(exc, ToolError):
                agent_result = AgentResult(
                    success=False,
                    error=str(exc),
                    attempts=total_invocations,
                    status_message=f"Agent crashed on tool error: {exc}",
                )
            else:
                wall_clock_duration = (time.perf_counter() - wall_clock_start) * 1000
                events = self.ledger.get_events(active_run_id)
                clean_exc_msg = mask_string_secrets(str(exc))
                rule_name = (
                    "configuration_integrity"
                    if isinstance(exc, ConfigurationError)
                    else (
                        "infrastructure_integrity"
                        if isinstance(exc, InfrastructureError)
                        else "runtime_integrity"
                    )
                )
                infra_eval = EvaluationResult(
                    verdict=EvaluationVerdict.INFRASTRUCTURE_ERROR,
                    passed=False,
                    assertions=[
                        EvaluationAssertion(
                            name=rule_name,
                            passed=False,
                            expected="operational",
                            actual="error",
                            message=f"{type(exc).__name__}: {clean_exc_msg}",
                        )
                    ],
                    expected_side_effects=scenario.assertions.expected_side_effects,
                    actual_side_effects=len([e for e in events if e.commit_status == CommitStatus.COMMITTED]),
                    duration_ms=wall_clock_duration,
                    failure_reasons=[f"{type(exc).__name__}: {clean_exc_msg}"],
                    isolation_mode=active_isolation_mode,
                )
                summary = RunSummary(
                    run_id=active_run_id,
                    scenario_id=scenario.scenario_id,
                    agent_id=agent_id,
                    passed=False,
                    verdict=EvaluationVerdict.INFRASTRUCTURE_ERROR,
                    total_events=len(events),
                    committed_effects=infra_eval.actual_side_effects,
                    duplicate_effects=0,
                    created_at=self.clock.now_iso(),
                    duration_ms=wall_clock_duration,
                    isolation_mode=active_isolation_mode,
                )
                self.ledger.record_run(summary)
                return RunResult(
                    run_id=active_run_id,
                    scenario=scenario,
                    agent_id=agent_id,
                    evaluation=infra_eval,
                    events=events,
                    summary=summary,
                    agent_raw_result={"error": str(exc)},
                )

        wall_clock_duration = (time.perf_counter() - wall_clock_start) * 1000

        # Retrieve all events for this run
        events = self.ledger.get_events(active_run_id)

        # Run recovery assertions
        evaluation = self.evaluator.evaluate(
            events=events,
            assertions=scenario.assertions,
            agent_result=agent_result,
            duration_ms=wall_clock_duration,
        )
        evaluation.isolation_mode = active_isolation_mode

        summary = RunSummary(
            run_id=active_run_id,
            scenario_id=scenario.scenario_id,
            agent_id=agent_id,
            passed=evaluation.passed,
            verdict=evaluation.verdict,
            total_events=len(events),
            committed_effects=evaluation.actual_side_effects,
            duplicate_effects=evaluation.duplicate_effect_count,
            created_at=self.clock.now_iso(),
            duration_ms=wall_clock_duration,
            isolation_mode=active_isolation_mode,
        )
        self.ledger.record_run(summary)

        return RunResult(
            run_id=active_run_id,
            scenario=scenario,
            agent_id=agent_id,
            evaluation=evaluation,
            events=events,
            summary=summary,
            agent_raw_result=agent_result.model_dump(),
        )
