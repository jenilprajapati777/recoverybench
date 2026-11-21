"""Execution engine executing scenarios against agent adapters."""

import asyncio
import contextlib
import inspect
import math
import time
import uuid
from collections.abc import Callable, Generator
from typing import Any

from recoverybench.adapters.callable import wrap_agent
from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    AwaitableDict,
    TaskContext,
    ToolClient,
)
from recoverybench.clock import VirtualClock
from recoverybench.config import get_settings
from recoverybench.engine.context import RunResult
from recoverybench.errors import (
    CircuitBreakerError,
    ConfigurationError,
    InfrastructureError,
    ProcessTerminatedError,
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
    HarnessAuditAuthority,
    HarnessOrchestratorAuthority,
    HarnessRuntimeAuthority,
    LedgerEvent,
    PrimaryInfrastructureStatus,
    ReconciliationInfrastructureStatus,
    RunSummary,
    ToolExecutionModel,
    compute_evaluation_digest,
    compute_request_fingerprint,
    hash_token,
)
from recoverybench.sandbox.backend import (
    ExecutionBackend,
    InProcessExecutionBackend,
    taint_process,
)
from recoverybench.sandbox.spec import AgentSpec
from recoverybench.scenarios.schema import ScenarioModel
from recoverybench.security import mask_string_secrets
from recoverybench.tools.base import (
    ToolRegistry,
    ToolResult,
    ToolService,
    demonstrate_runtime_capacity_enforcement,
)
from recoverybench.tools.process_runner import ToolProcessRunner


class BoundToolClient(ToolClient):
    """Execution engine implementation of the ToolClient interface.

    Allows agents to use either tool.call(...) or tool(...) interchangeably,
    and supports scoped operation boundaries via `with tool.operation(op_id):`.
    """

    def __init__(
        self,
        dispatcher: Callable[..., Any] | None = None,
        sync_dispatcher: Callable[..., dict[str, Any]] | None = None,
        async_dispatcher: Callable[..., Any] | None = None,
        default_operation_id: str | None = None,
        is_async: bool = False,
    ) -> None:
        self._sync_dispatcher = sync_dispatcher or (dispatcher if not is_async else None)
        self._async_dispatcher = async_dispatcher or (dispatcher if is_async else None)
        self._default_operation_id = default_operation_id
        self._scoped_operation_id: str | None = None
        self._is_async = is_async

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
    ) -> AwaitableDict:
        """Dispatch a tool call synchronously with operation and idempotency arguments."""
        if self._is_async:
            raise ConfigurationError(
                "Cannot call tool synchronously via call() in asynchronous execution. "
                "Use 'await tool.call_async(...)' or 'await tool(...)' instead."
            )
        if self._sync_dispatcher is None:
            raise ConfigurationError("No synchronous tool dispatcher configured.")

        effective_op_id = (
            operation_id.strip()
            if (operation_id and operation_id.strip())
            else self.current_operation_id
        )
        res = self._sync_dispatcher(tool_name, action, params, idempotency_key, effective_op_id)
        if isinstance(res, AwaitableDict):
            return res
        return AwaitableDict(res)

    async def call_async(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> AwaitableDict:
        """Asynchronously dispatch a tool call."""
        effective_op_id = (
            operation_id.strip()
            if (operation_id and operation_id.strip())
            else self.current_operation_id
        )
        if self._is_async and self._async_dispatcher is not None:
            res = await self._async_dispatcher(tool_name, action, params, idempotency_key, effective_op_id)
            if isinstance(res, AwaitableDict):
                return res
            return AwaitableDict(res)

        return self.call(tool_name, action, params, idempotency_key, effective_op_id)

    def __call__(
        self,
        tool_name: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
    ) -> AwaitableDict:
        """Callable dispatch protocol for backwards compatibility with tool_call(...)."""
        effective_op_id = (
            operation_id.strip()
            if (operation_id and operation_id.strip())
            else self.current_operation_id
        )
        if self._is_async and self._async_dispatcher is not None:
            coro = self._async_dispatcher(tool_name, action, params, idempotency_key, effective_op_id)
            return AwaitableDict(coro=coro)
        return self.call(tool_name, action, params, idempotency_key, effective_op_id)


class ExecutionEngine:
    """Coordinates deterministic fault injection, tool dispatch, and evaluation."""

    def __init__(
        self,
        ledger: Ledger,
        tool_registry: ToolRegistry,
        clock: VirtualClock | None = None,
        max_attempts: int | None = None,
        backend: ExecutionBackend | None = None,
        runtime_authority: HarnessRuntimeAuthority | None = None,
        audit_authority: HarnessAuditAuthority | None = None,
        orchestrator_authority: HarnessOrchestratorAuthority | None = None,
    ) -> None:
        self.ledger = ledger
        self.tool_registry = tool_registry
        self.clock = clock or VirtualClock()
        self.max_attempts = max_attempts or get_settings().max_attempts
        self.backend = backend or InProcessExecutionBackend()
        self.runtime_authority = runtime_authority or HarnessRuntimeAuthority()
        self.audit_authority = audit_authority or HarnessAuditAuthority()
        self.orchestrator_authority = orchestrator_authority or HarnessOrchestratorAuthority()
        self.evaluator = RecoveryEvaluator(
            tool_registry=self.tool_registry,
            runtime_authority=self.runtime_authority,
            audit_authority=self.audit_authority,
            orchestrator_authority=self.orchestrator_authority,
        )
        self.process_runner = ToolProcessRunner()

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
        if isinstance(agent, AgentAdapter | AgentSpec):
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
        current_primary_infra_status = PrimaryInfrastructureStatus.CLEAN.value

        # Reset tools state for clean run
        self.tool_registry.reset_all()
        self.tool_registry.bind_to_run(active_run_id)
        try:
            # Check for uncooperative tools in trusted mode
            if not active_backend.is_isolated:
                for tname, tserv in getattr(self.tool_registry, "_tools", {}).items():
                    exec_model = getattr(tserv, "execution_model", None)
                    if exec_model in (ToolExecutionModel.UNCOOPERATIVE_SYNC, "UNCOOPERATIVE"):
                        raise ConfigurationError(
                            f"Tool '{tname}' has execution model UNCOOPERATIVE which cannot be executed in trusted mode. "
                            "Uncooperative sync tools require isolated mode with process termination capability."
                        )

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
            ) -> Any:
                nonlocal total_invocations
                total_invocations += 1

                if total_invocations > self.max_attempts:
                    raise CircuitBreakerError(total_invocations, self.max_attempts)

                tool = self.tool_registry.get(tool_name)
                if not tool:
                    raise ConfigurationError(
                        f"Tool '{tool_name}' requested by agent is not registered in mock registry."
                    )
                if self.tool_registry.is_quarantined(tool_name):
                    raise ToolError(f"Tool '{tool_name}' is quarantined and cannot be invoked.")

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

                if hasattr(tool, "_handlers"):
                    handler = tool._handlers.get(action)
                    if not handler and hasattr(tool, "_primary_action") and tool._primary_action:
                        handler = tool._handlers.get(tool._primary_action)
                    if inspect.iscoroutinefunction(handler):
                        raise RuntimeError(
                            f"Tool '{tool_name}.{action}' is an asynchronous function ('async def'). "
                            "It cannot be executed in synchronous mode. Test this agent with 'await test_agent_async(...)'."
                        )

                tool_exec_model = getattr(tool, "execution_model", ToolExecutionModel.BOUNDED_SYNC)
                is_uncoop = tool_exec_model in (ToolExecutionModel.UNCOOPERATIVE_SYNC, "UNCOOPERATIVE")

                # 2. Tool Mutation in Mock Service
                self.tool_registry.worker_started()
                try:
                    if is_uncoop:
                        if not active_backend.is_isolated:
                            raise ConfigurationError(
                                f"Tool '{tool_name}' has execution model UNCOOPERATIVE which cannot be executed in trusted mode. "
                                "Uncooperative sync tools require isolated mode with process termination capability."
                            )
                        proc_res = self.process_runner.execute(
                            tool=tool,
                            action=action,
                            params=params,
                            idempotency_key=idempotency_key,
                            operation_id=effective_op_id,
                            attempt_id=attempt_id,
                            raise_on_error=False,
                        )
                        if proc_res.terminated:
                            nonlocal current_primary_infra_status
                            current_primary_infra_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value
                            term_event = LedgerEvent(
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
                                execution_status=ExecutionStatus.ERROR,
                                commit_status=CommitStatus.UNCERTAIN,
                                simulated_timestamp=sim_ts,
                                error_type="ProcessTerminatedError",
                                details={
                                    "process_terminated": True,
                                    "child_pid": proc_res.child_pid,
                                    "parent_pid": proc_res.parent_pid,
                                    "exit_code": proc_res.exit_code,
                                    "error": proc_res.error_message,
                                },
                            )
                            self.ledger.append(term_event)
                            raise ProcessTerminatedError(
                                f"Tool '{tool_name}.{action}' child process PID {proc_res.child_pid} exceeded deadline and was terminated by parent",
                                child_pid=proc_res.child_pid,
                            )
                        if not proc_res.success:
                            err_event = LedgerEvent(
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
                                execution_status=ExecutionStatus.ERROR,
                                commit_status=CommitStatus.ABORTED,
                                simulated_timestamp=sim_ts,
                                error_type=proc_res.error_type or "ToolError",
                                details={"error": mask_string_secrets(proc_res.error_message or "")},
                            )
                            self.ledger.append(err_event)
                            raise ToolError(proc_res.error_message or "Tool execution error")
                        tool_result = proc_res.tool_result
                    else:
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
                except (ProcessTerminatedError, ConfigurationError):
                    self.tool_registry.worker_finished()
                    raise
                except Exception as tool_exc:
                    self.tool_registry.worker_finished()
                    err_event = LedgerEvent(
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
                        execution_status=ExecutionStatus.ERROR,
                        commit_status=CommitStatus.ABORTED,
                        simulated_timestamp=sim_ts,
                        error_type=type(tool_exc).__name__,
                        details={"error": mask_string_secrets(str(tool_exc))},
                    )
                    self.ledger.append(err_event)
                    raise

                # 3. Duplicate Delivery Check (simulates at-least-once queue/network redelivery)
                try:
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
                    assert tool_result is not None
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
                finally:
                    self.tool_registry.worker_finished()

            # Client interface supporting callable, tool.call, and tool.operation protocols
            tool_client = BoundToolClient(
                sync_dispatcher=tool_dispatcher,
                default_operation_id=default_op_id,
                is_async=False,
            )

            timeout_seconds = float(
                scenario.task.get("timeout_seconds")
                or scenario.task.get("timeout")
                or getattr(scenario, "timeout_seconds", None)
                or get_settings().default_timeout_seconds
            )

            # Execute Agent via selected ExecutionBackend
            agent_result: AgentResult
            try:
                agent_result = active_backend.run(
                    agent=resolved_agent,
                    task_context=task_context,
                    tool_client=tool_client,
                    timeout_seconds=timeout_seconds,
                )
            except CircuitBreakerError as cbe:
                agent_result = AgentResult(
                    success=False,
                    error=str(cbe),
                    attempts=max(1, total_invocations),
                    status_message="Halted by harness circuit breaker",
                )
            except Exception as exc:
                # Check if it was an unhandled tool error or unexpected crash
                if isinstance(exc, ToolError):
                    agent_result = AgentResult(
                        success=False,
                        error=str(exc),
                        attempts=max(1, total_invocations),
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
                    infra_primary_status = current_primary_infra_status or PrimaryInfrastructureStatus.CLEAN.value
                    if isinstance(exc, ProcessTerminatedError):
                        infra_primary_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value
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
                        execution_finality="FINAL",
                        primary_infrastructure_status=infra_primary_status,
                        reconciliation_infrastructure_status="NOT_APPLICABLE",
                        overall_outcome="INFRASTRUCTURE_ERROR",
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

            # Drain Barrier (ADR-004 Section 8.3)
            drain_timeout = 0.5
            drain_start = time.perf_counter()
            while self.tool_registry.active_workers > 0 and (time.perf_counter() - drain_start) < drain_timeout:
                time.sleep(0.01)

            worker_leaked = (self.tool_registry.active_workers > 0)
            primary_infra_status = PrimaryInfrastructureStatus.CLEAN.value
            if worker_leaked:
                if not active_backend.is_isolated:
                    taint_process()
                    self.tool_registry.quarantine()
                    primary_infra_status = PrimaryInfrastructureStatus.THREAD_LEAKED.value
                else:
                    primary_infra_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value
            elif current_primary_infra_status == PrimaryInfrastructureStatus.PROCESS_TERMINATED.value:
                primary_infra_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value

            wall_clock_duration = (time.perf_counter() - wall_clock_start) * 1000

            # Retrieve all events for this run
            events = self.ledger.get_events(active_run_id)

            # Run recovery assertions
            harness_tool_metadata: dict[str, Any] = {}
            if self.tool_registry:
                for tname, tservice in getattr(self.tool_registry, "_tools", {}).items():
                    if demonstrate_runtime_capacity_enforcement(tservice):
                        cap = getattr(tservice, "max_side_effect_capacity", 1)
                        if (
                            hasattr(tservice, "_mutates")
                            and isinstance(tservice._mutates, dict)
                            and tservice._mutates
                            and not any(tservice._mutates.values())
                        ):
                            cap = 0
                        elif cap == math.inf or cap is None:
                            cap = 1
                        contract = getattr(tservice, "idempotency_contract", None)
                        dedup = getattr(contract, "verified_service_deduplication", False) if contract else False
                        harness_tool_metadata[tname] = {
                            "harness_attestation": self.runtime_authority.attest(
                                run_id=active_run_id,
                                tool_name=tname,
                                max_capacity=int(cap),
                                runtime_guarantee="RUNTIME_EXACTLY_ONCE_DEDUPLICATION",
                            ),
                            "verified_service_deduplication": dedup,
                        }

            evaluation_digest = compute_evaluation_digest(
                run_id=active_run_id,
                events=events,
                expected_side_effects=scenario.assertions.expected_side_effects,
                expect_agent_success=scenario.assertions.expect_agent_success,
                agent_status=agent_result.status.value,
                expected_effect_ids=getattr(scenario.assertions, "expected_effect_ids", None),
                infrastructure_status=primary_infra_status,
            )
            finality_proof = self.orchestrator_authority.issue_finality_proof(
                run_id=active_run_id,
                evaluation_digest=evaluation_digest,
            )
            evaluation = self.evaluator.evaluate(
                events=events,
                assertions=scenario.assertions,
                agent_result=agent_result,
                duration_ms=wall_clock_duration,
                tool_registry=self.tool_registry,
                tool_metadata=harness_tool_metadata,
                execution_finality=finality_proof,
                infrastructure_status=primary_infra_status,
                run_id=active_run_id,
                expected_effect_ids=getattr(scenario.assertions, "expected_effect_ids", None),
            )
            if worker_leaked or primary_infra_status == PrimaryInfrastructureStatus.PROCESS_TERMINATED.value:
                evaluation.verdict = EvaluationVerdict.INFRASTRUCTURE_ERROR
                evaluation.passed = False
                evaluation.execution_finality = "FINAL"
                evaluation.primary_infrastructure_status = primary_infra_status
                evaluation.reconciliation_infrastructure_status = "NOT_APPLICABLE"
                evaluation.overall_outcome = "INFRASTRUCTURE_ERROR"
                evaluation.behavioral_verdict = evaluation.behavioral_verdict or "INSUFFICIENT_EVIDENCE"
                evaluation.unresolved_workers = self.tool_registry.active_workers
                evaluation.failure_reasons.append(f"Worker containment failure: {primary_infra_status}")
            else:
                if evaluation.primary_infrastructure_status is None:
                    evaluation.primary_infrastructure_status = primary_infra_status
                if evaluation.reconciliation_infrastructure_status is None:
                    evaluation.reconciliation_infrastructure_status = ReconciliationInfrastructureStatus.CLEAN.value
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

        finally:
            self.tool_registry.unbind_run(active_run_id)

    async def run_async(
        self,
        scenario: ScenarioModel,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        run_id: str | None = None,
        backend: ExecutionBackend | None = None,
    ) -> RunResult:
        """Asynchronously execute a full scenario run against an agent adapter, spec, or callable."""
        active_backend = backend or self.backend
        active_isolation_mode = active_backend.mode.value

        resolved_agent: AgentAdapter | AgentSpec
        if isinstance(agent, AgentAdapter | AgentSpec):
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
        current_primary_infra_status = PrimaryInfrastructureStatus.CLEAN.value

        # Reset tools state for clean run
        self.tool_registry.reset_all()
        self.tool_registry.bind_to_run(active_run_id)
        try:
            # Check for uncooperative tools in trusted mode
            if not active_backend.is_isolated:
                for tname, tserv in getattr(self.tool_registry, "_tools", {}).items():
                    exec_model = getattr(tserv, "execution_model", None)
                    if exec_model in (ToolExecutionModel.UNCOOPERATIVE_SYNC, "UNCOOPERATIVE"):
                        raise ConfigurationError(
                            f"Tool '{tname}' has execution model UNCOOPERATIVE which cannot be executed in trusted mode. "
                            "Uncooperative sync tools require isolated mode with process termination capability."
                        )

            # Build Task Context with established operation_id boundary
            is_single_op = scenario.is_single_operation
            default_op_id: str | None = None

            if is_single_op:
                if "operation_id" in scenario.task:
                    default_op_id = scenario.task["operation_id"]
                else:
                    default_op_id = f"op_{scenario.scenario_id}"
            else:
                default_op_id = None

            task_context = TaskContext(
                scenario_id=scenario.scenario_id,
                task_params=scenario.task,
                clock=self.clock,
                operation_id=default_op_id,
            )

            # Asynchronous tool dispatch callback provided to agent
            async def async_tool_dispatcher(
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
                if self.tool_registry.is_quarantined(tool_name):
                    raise ToolError(f"Tool '{tool_name}' is quarantined and cannot be invoked.")

                # Establish unique attempt_id and effective operation_id
                attempt_id = f"att_{active_run_id}_{total_invocations}"
                clean_op_id = operation_id.strip() if (operation_id and operation_id.strip()) else None
                effective_op_id = clean_op_id if clean_op_id is not None else task_context.operation_id

                attempt = fault_injector.register_attempt(tool_name, action)
                request_fp = compute_request_fingerprint(tool_name, action, params)
                key_hash = hash_token(idempotency_key)
                sim_ts = self.clock.now_iso()

                # 1. Pre-Commit Fault Check
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
                    self.clock.advance(0.1)
                    raise pre_commit_fault

                is_async_tool = False
                if hasattr(tool, "_handlers"):
                    handler = tool._handlers.get(action)
                    if not handler and hasattr(tool, "_primary_action") and tool._primary_action:
                        handler = tool._handlers.get(tool._primary_action)
                    is_async_tool = inspect.iscoroutinefunction(handler)
                elif (
                    inspect.iscoroutinefunction(getattr(tool, "execute_async", None))
                    and tool.__class__.execute_async != ToolService.execute_async
                ):
                    is_async_tool = True

                tool_exec_model = getattr(tool, "execution_model", ToolExecutionModel.BOUNDED_SYNC)
                is_uncoop = tool_exec_model in (ToolExecutionModel.UNCOOPERATIVE_SYNC, "UNCOOPERATIVE")

                # 2. Tool Mutation in Mock Service
                self.tool_registry.worker_started()
                try:
                    if is_uncoop:
                        if not active_backend.is_isolated:
                            raise ConfigurationError(
                                f"Tool '{tool_name}' has execution model UNCOOPERATIVE which cannot be executed in trusted mode. "
                                "Uncooperative sync tools require isolated mode with process termination capability."
                            )
                        proc_res = await self.process_runner.execute_async(
                            tool=tool,
                            action=action,
                            params=params,
                            idempotency_key=idempotency_key,
                            operation_id=effective_op_id,
                            attempt_id=attempt_id,
                            raise_on_error=False,
                        )
                        if proc_res.terminated:
                            nonlocal current_primary_infra_status
                            current_primary_infra_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value
                            term_event = LedgerEvent(
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
                                execution_status=ExecutionStatus.ERROR,
                                commit_status=CommitStatus.UNCERTAIN,
                                simulated_timestamp=sim_ts,
                                error_type="ProcessTerminatedError",
                                details={
                                    "process_terminated": True,
                                    "child_pid": proc_res.child_pid,
                                    "parent_pid": proc_res.parent_pid,
                                    "exit_code": proc_res.exit_code,
                                    "error": proc_res.error_message,
                                },
                            )
                            self.ledger.append(term_event)
                            raise ProcessTerminatedError(
                                f"Tool '{tool_name}.{action}' child process PID {proc_res.child_pid} exceeded deadline and was terminated by parent",
                                child_pid=proc_res.child_pid,
                            )
                        if not proc_res.success:
                            err_event = LedgerEvent(
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
                                execution_status=ExecutionStatus.ERROR,
                                commit_status=CommitStatus.ABORTED,
                                simulated_timestamp=sim_ts,
                                error_type=proc_res.error_type or "ToolError",
                                details={"error": mask_string_secrets(proc_res.error_message or "")},
                            )
                            self.ledger.append(err_event)
                            raise ToolError(proc_res.error_message or "Tool execution error")
                        tool_result = proc_res.tool_result
                    elif is_async_tool:
                        tool_result = await tool.execute_async(
                            action,
                            params,
                            idempotency_key=idempotency_key,
                            operation_id=effective_op_id,
                            attempt_id=attempt_id,
                        )
                    else:
                        def _sync_worker() -> ToolResult:
                            try:
                                return tool.execute(
                                    action,
                                    params,
                                    idempotency_key=idempotency_key,
                                    operation_id=effective_op_id,
                                    attempt_id=attempt_id,
                                )
                            except TypeError:
                                return tool.execute(action, params, idempotency_key=idempotency_key)

                        tool_result = await asyncio.to_thread(_sync_worker)
                except (ProcessTerminatedError, ConfigurationError):
                    self.tool_registry.worker_finished()
                    raise
                except Exception as tool_exc:
                    self.tool_registry.worker_finished()
                    err_event = LedgerEvent(
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
                        execution_status=ExecutionStatus.ERROR,
                        commit_status=CommitStatus.ABORTED,
                        simulated_timestamp=sim_ts,
                        error_type=type(tool_exc).__name__,
                        details={"error": mask_string_secrets(str(tool_exc))},
                    )
                    self.ledger.append(err_event)
                    raise

                # 3. Duplicate Delivery Check
                try:
                    is_dup, dup_count = fault_injector.is_duplicate_delivery(tool_name, action, attempt)
                    if is_dup:
                        for dup_idx in range(1, dup_count + 1):
                            dup_attempt_id = f"{attempt_id}_dup{dup_idx}"
                            if is_async_tool:
                                dup_result = await tool.execute_async(
                                    action,
                                    params,
                                    idempotency_key=idempotency_key,
                                    operation_id=effective_op_id,
                                    attempt_id=dup_attempt_id,
                                )
                            else:
                                def _dup_worker(att_id: str = dup_attempt_id) -> ToolResult:
                                    try:
                                        return tool.execute(
                                            action,
                                            params,
                                            idempotency_key=idempotency_key,
                                            operation_id=effective_op_id,
                                            attempt_id=att_id,
                                        )
                                    except TypeError:
                                        return tool.execute(action, params, idempotency_key=idempotency_key)

                                dup_result = await asyncio.to_thread(_dup_worker)

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

                    # 4. Post-Commit Fault Check
                    assert tool_result is not None
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

                    # 5. Malformed Payload Check
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
                finally:
                    self.tool_registry.worker_finished()

            tool_client = BoundToolClient(
                async_dispatcher=async_tool_dispatcher,
                default_operation_id=default_op_id,
                is_async=True,
            )

            timeout = float(
                scenario.task.get("timeout_seconds")
                or scenario.task.get("timeout")
                or getattr(scenario, "timeout_seconds", None)
                or get_settings().default_timeout_seconds
            )
            agent_result: AgentResult
            try:
                try:
                    async with asyncio.timeout(timeout):
                        agent_result = await active_backend.run_async(
                            agent=resolved_agent,
                            task_context=task_context,
                            tool_client=tool_client,
                            timeout_seconds=timeout,
                        )
                except TimeoutError:
                    agent_result = AgentResult(
                        success=False,
                        error=f"TimeoutError: Execution timed out after {timeout} seconds",
                        attempts=max(1, total_invocations),
                        status_message=f"Agent timed out after {timeout}s",
                    )
            except asyncio.CancelledError:
                raise
            except CircuitBreakerError as cbe:
                agent_result = AgentResult(
                    success=False,
                    error=str(cbe),
                    attempts=max(1, total_invocations),
                    status_message="Halted by harness circuit breaker",
                )
            except Exception as exc:
                if isinstance(exc, ToolError):
                    agent_result = AgentResult(
                        success=False,
                        error=str(exc),
                        attempts=max(1, total_invocations),
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
                    infra_primary_status = current_primary_infra_status or PrimaryInfrastructureStatus.CLEAN.value
                    if isinstance(exc, ProcessTerminatedError):
                        infra_primary_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value
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
                        execution_finality="FINAL",
                        primary_infrastructure_status=infra_primary_status,
                        reconciliation_infrastructure_status="NOT_APPLICABLE",
                        overall_outcome="INFRASTRUCTURE_ERROR",
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

            # Drain Barrier (ADR-004 Section 8.3)
            drain_timeout = 0.5
            drain_start = time.perf_counter()
            while self.tool_registry.active_workers > 0 and (time.perf_counter() - drain_start) < drain_timeout:
                await asyncio.sleep(0.01)

            worker_leaked = (self.tool_registry.active_workers > 0)
            primary_infra_status = PrimaryInfrastructureStatus.CLEAN.value
            if worker_leaked:
                if not active_backend.is_isolated:
                    taint_process()
                    self.tool_registry.quarantine()
                    primary_infra_status = PrimaryInfrastructureStatus.THREAD_LEAKED.value
                else:
                    primary_infra_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value
            elif current_primary_infra_status == PrimaryInfrastructureStatus.PROCESS_TERMINATED.value:
                primary_infra_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value

            wall_clock_duration = (time.perf_counter() - wall_clock_start) * 1000
            events = self.ledger.get_events(active_run_id)

            harness_tool_metadata: dict[str, Any] = {}
            if self.tool_registry:
                for tname, tservice in getattr(self.tool_registry, "_tools", {}).items():
                    if demonstrate_runtime_capacity_enforcement(tservice):
                        cap = getattr(tservice, "max_side_effect_capacity", 1)
                        if (
                            hasattr(tservice, "_mutates")
                            and isinstance(tservice._mutates, dict)
                            and tservice._mutates
                            and not any(tservice._mutates.values())
                        ):
                            cap = 0
                        elif cap == math.inf or cap is None:
                            cap = 1
                        contract = getattr(tservice, "idempotency_contract", None)
                        dedup = getattr(contract, "verified_service_deduplication", False) if contract else False
                        harness_tool_metadata[tname] = {
                            "harness_attestation": self.runtime_authority.attest(
                                run_id=active_run_id,
                                tool_name=tname,
                                max_capacity=int(cap),
                                runtime_guarantee="RUNTIME_EXACTLY_ONCE_DEDUPLICATION",
                            ),
                            "verified_service_deduplication": dedup,
                        }

            evaluation_digest = compute_evaluation_digest(
                run_id=active_run_id,
                events=events,
                expected_side_effects=scenario.assertions.expected_side_effects,
                expect_agent_success=scenario.assertions.expect_agent_success,
                agent_status=agent_result.status.value,
                expected_effect_ids=getattr(scenario.assertions, "expected_effect_ids", None),
                infrastructure_status=primary_infra_status,
            )
            finality_proof = self.orchestrator_authority.issue_finality_proof(
                run_id=active_run_id,
                evaluation_digest=evaluation_digest,
            )
            evaluation = self.evaluator.evaluate(
                events=events,
                assertions=scenario.assertions,
                agent_result=agent_result,
                duration_ms=wall_clock_duration,
                tool_registry=self.tool_registry,
                tool_metadata=harness_tool_metadata,
                execution_finality=finality_proof,
                infrastructure_status=primary_infra_status,
                run_id=active_run_id,
                expected_effect_ids=getattr(scenario.assertions, "expected_effect_ids", None),
            )
            if worker_leaked or primary_infra_status == PrimaryInfrastructureStatus.PROCESS_TERMINATED.value:
                evaluation.verdict = EvaluationVerdict.INFRASTRUCTURE_ERROR
                evaluation.passed = False
                evaluation.execution_finality = "FINAL"
                evaluation.primary_infrastructure_status = primary_infra_status
                evaluation.reconciliation_infrastructure_status = "NOT_APPLICABLE"
                evaluation.overall_outcome = "INFRASTRUCTURE_ERROR"
                evaluation.behavioral_verdict = evaluation.behavioral_verdict or "INSUFFICIENT_EVIDENCE"
                evaluation.unresolved_workers = self.tool_registry.active_workers
                evaluation.failure_reasons.append(f"Worker containment failure: {primary_infra_status}")
            else:
                if evaluation.primary_infrastructure_status is None:
                    evaluation.primary_infrastructure_status = primary_infra_status
                if evaluation.reconciliation_infrastructure_status is None:
                    evaluation.reconciliation_infrastructure_status = ReconciliationInfrastructureStatus.CLEAN.value
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
        finally:
            self.tool_registry.unbind_run(active_run_id)
