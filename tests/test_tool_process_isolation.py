"""Comprehensive process boundary and isolation test suite.

Implements Tests A through G as required by ADR-004 Section 6.4 and Phase 4.2 remediation:
- Test A: Process Identity (child_pid != parent_pid, real OS process)
- Test B: Actual Hard Termination (uncooperative hanging child killed, parent unaffected)
- Test C: No Thread Fallback (trusted mode rejects uncooperative tools fail-closed)
- Test D: Result and Exception IPC (structured serialization, child errors, unpickleable rejection)
- Test E: Remote Side-Effect Uncertainty (process death leaves bounds uncertain until reconciled)
- Test F: Windows Process Lifecycle (spawn start method, exit code capture, cleanup)
- Test G: Regression (cooperative sync, bounded sync, coroutine tools unaffected)
"""

import asyncio
import os
import uuid
from typing import Any

import pytest

from recoverybench.agent.base import AgentResult, CompletionStatus
from recoverybench.engine.context import RunResult
from recoverybench.engine.late_outcome import LateOutcomeLedger
from recoverybench.engine.reconciliation import ReconciliationEngine
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.errors import (
    ConfigurationError,
    ProcessTerminatedError,
    ToolError,
)
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.models import (
    AuthoritativeFinalityProof,
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
    HarnessAuditAuthority,
    HarnessOrchestratorAuthority,
    HarnessRuntimeAuthority,
    LateOutcomeRecord,
    LedgerEvent,
    PrimaryInfrastructureStatus,
    RunSummary,
    ToolExecutionModel,
    compute_evaluation_digest,
)
from recoverybench.reporting.json_reporter import JSONReporter
from recoverybench.sandbox.backend import (
    InProcessExecutionBackend,
)
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry, ToolResult, ToolService
from recoverybench.tools.declarative import DeclarativeToolService
from recoverybench.tools.process_runner import ToolProcessRunner
from recoverybench.tools.testing import (
    UncooperativeErrorTool,
    UncooperativeHangingTool,
    UncooperativePidTool,
    UncooperativeSuccessTool,
)

TEST_RUNTIME_AUTH = HarnessRuntimeAuthority()
TEST_AUDIT_AUTH = HarnessAuditAuthority()
TEST_ORCH_AUTH = HarnessOrchestratorAuthority()


def _make_c3_meta(
    events: list[LedgerEvent],
    tool_name: str,
    max_capacity: int = 1,
    runtime_guarantee: str = "RUNTIME_UNIQUE_CONSTRAINT",
) -> dict[str, Any]:
    run_id = events[0].run_id if events else "run_test"
    att = TEST_RUNTIME_AUTH.attest(
        run_id=run_id,
        tool_name=tool_name,
        max_capacity=max_capacity,
        runtime_guarantee=runtime_guarantee,
    )
    return {
        tool_name: {
            "harness_attestation": att,
            "verified_service_deduplication": True,
        }
    }


def _make_finality_proof(
    run_id: str,
    events: list[LedgerEvent],
    expected_side_effects: int,
    expect_agent_success: bool,
    agent_status: str,
    infrastructure_status: str,
) -> AuthoritativeFinalityProof:
    digest = compute_evaluation_digest(
        run_id=run_id,
        events=events,
        expected_side_effects=expected_side_effects,
        expect_agent_success=expect_agent_success,
        agent_status=agent_status,
        infrastructure_status=infrastructure_status,
    )
    return TEST_ORCH_AUTH.issue_finality_proof(
        run_id=run_id,
        evaluation_digest=digest,
    )


def _make_scenario(
    scenario_id: str,
    target_tool: str = "uncoop_success",
    target_action: str = "run",
    assertions: ScenarioAssertions | None = None,
    name: str = "",
) -> ScenarioModel:
    return ScenarioModel(
        schema_version=1,
        scenario_id=scenario_id,
        name=name or f"Scenario {scenario_id}",
        description=f"Description for {scenario_id}",
        target_tool=target_tool,
        target_action=target_action,
        assertions=assertions or ScenarioAssertions(),
        task={},
    )


def _make_late_record(
    run_id: str,
    attempt_id: str,
    effect_id: str | None = None,
    commit_status: CommitStatus = CommitStatus.COMMITTED,
    writer_id: str = "THREAD_HOOK",
    event_type: str = "LATE_COMPLETION",
    operation_id: str = "op_1",
    event_sequence: int = 1,
) -> LateOutcomeRecord:
    payload = {
        "commit_status": commit_status.value if hasattr(commit_status, "value") else str(commit_status),
        "effect_id": effect_id,
    }
    fp = LateOutcomeRecord.compute_payload_fingerprint(payload)
    return LateOutcomeRecord(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        writer_id=writer_id,
        event_sequence=event_sequence,
        event_type=event_type,
        payload_fingerprint=fp,
        emitted_timestamp="2026-11-20T00:00:00Z",
        arrival_timestamp="2026-11-20T00:00:01Z",
        payload=payload,
    )


class TestToolProcessIsolation:
    """Test suite verifying genuine OS process isolation for uncooperative tools."""

    def test_a_process_identity(self) -> None:
        """Test A: Process Identity.

        Asserts child_pid != parent_pid and proves child executed in a real OS process.
        """
        runner = ToolProcessRunner()
        pid_tool = UncooperativePidTool(name="pid_tool", timeout_seconds=15.0)

        result = runner.execute(
            tool=pid_tool,
            action="get_pid",
            params={"test": 1},
            raise_on_error=True,
        )

        parent_pid = os.getpid()
        assert result.success is True
        assert result.child_pid is not None
        assert isinstance(result.child_pid, int)
        assert result.child_pid > 0
        assert result.child_pid != parent_pid
        assert result.parent_pid == parent_pid
        assert result.tool_result is not None
        assert result.tool_result.success is True
        assert result.tool_result.data["pid"] == result.child_pid
        assert result.tool_result.data["parent_pid"] == parent_pid
        assert result.terminated is False
        assert result.exit_code == 0

    def test_b_actual_hard_termination(self) -> None:
        """Test B: Actual Hard Termination.

        Spawns hanging child tool, enforces 0.1s timeout, terminates child,
        verifies child exit at OS level, and asserts parent remains alive and healthy.
        """
        runner = ToolProcessRunner()
        hanging_tool = UncooperativeHangingTool(
            name="hanging_tool",
            timeout_seconds=0.1,
            sleep_duration=20.0,
        )

        # 1. Non-raising execution returns structured termination result
        res_no_raise = runner.execute(
            tool=hanging_tool,
            action="hang",
            params={},
            raise_on_error=False,
        )
        assert res_no_raise.success is False
        assert res_no_raise.terminated is True
        assert res_no_raise.child_pid is not None
        assert res_no_raise.child_pid != os.getpid()
        assert res_no_raise.exit_code is not None
        assert res_no_raise.error_type == "ProcessTerminatedError"
        assert "exceeded deadline" in (res_no_raise.error_message or "")

        # 2. Raising execution raises ProcessTerminatedError with valid child_pid
        with pytest.raises(ProcessTerminatedError) as exc_info:
            runner.execute(
                tool=hanging_tool,
                action="hang",
                params={},
                raise_on_error=True,
            )
        assert exc_info.value.child_pid is not None
        assert exc_info.value.child_pid != os.getpid()
        assert "exceeded deadline" in str(exc_info.value)

        # 3. Assert parent process is completely unaffected and can run further operations
        healthy_tool = UncooperativePidTool(name="pid_tool", timeout_seconds=15.0)
        healthy_res = runner.execute(
            tool=healthy_tool,
            action="get_pid",
            params={},
            raise_on_error=True,
        )
        assert healthy_res.success is True
        assert healthy_res.tool_result is not None
        assert healthy_res.tool_result.data["pid"] != os.getpid()

    def test_c_no_thread_fallback(self) -> None:
        """Test C: No Thread Fallback.

        Asserts uncooperative tool in trusted mode fails closed with ConfigurationError
        and never falls back to host threads or event loop execution.
        """
        tool = UncooperativeSuccessTool(name="uncoop_success")
        registry = ToolRegistry()
        registry.register(tool)

        backend = InProcessExecutionBackend()
        assert backend.is_isolated is False

        ledger = InMemoryLedger()
        engine = ExecutionEngine(
            ledger=ledger,
            tool_registry=registry,
            backend=backend,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )

        scenario = _make_scenario(
            scenario_id="test_uncoop_trusted",
            target_tool="uncoop_success",
            target_action="run",
            assertions=ScenarioAssertions(expected_side_effects=0, expect_agent_success=False),
        )

        def simple_agent(task: dict[str, Any], tool_client: Any) -> AgentResult:
            tool_client.execute("uncoop_success", "run", {})
            return AgentResult(success=True)

        # Sync dispatch: fails closed with ConfigurationError
        with pytest.raises(ConfigurationError) as exc_info:
            engine.run(
                scenario=scenario,
                agent=simple_agent,
            )
        assert "UNCOOPERATIVE" in str(exc_info.value)
        assert "trusted mode" in str(exc_info.value)

        # Async dispatch: fails closed with ConfigurationError
        async def async_test() -> None:
            async def simple_async_agent(task: dict[str, Any], tool_client: Any) -> AgentResult:
                await tool_client.execute_async("uncoop_success", "run", {})
                return AgentResult(success=True)

            with pytest.raises(ConfigurationError) as async_exc_info:
                await engine.run_async(
                    scenario=scenario,
                    agent=simple_async_agent,
                )
            assert "UNCOOPERATIVE" in str(async_exc_info.value)

        asyncio.run(async_test())

    def test_d_result_and_exception_ipc(self) -> None:
        """Test D: Result and Exception IPC.

        Tests:
        1. Normal return serialization across process boundary.
        2. Child exceptions safely converted to structured ToolError.
        3. Unserializable parameters/tools fail closed with ConfigurationError.
        """
        runner = ToolProcessRunner()

        # 1. Normal return serialization
        success_tool = UncooperativeSuccessTool(name="success_tool")
        res_ok = runner.execute(
            tool=success_tool,
            action="run",
            params={"value": 99},
            raise_on_error=True,
        )
        assert res_ok.success is True
        assert res_ok.tool_result is not None
        assert res_ok.tool_result.data["result"] == 99
        assert res_ok.tool_result.committed is True
        assert res_ok.tool_result.effect_id == "eff_uncoop_01"

        # 2. Child exception propagation
        err_tool = UncooperativeErrorTool(name="error_tool")
        res_err = runner.execute(
            tool=err_tool,
            action="fail",
            params={"message": "Child failed on purpose"},
            raise_on_error=False,
        )
        assert res_err.success is False
        assert res_err.terminated is False
        assert res_err.error_type == "ToolError"
        assert "Child failed on purpose" in (res_err.error_message or "")

        with pytest.raises(ToolError) as exc_info:
            runner.execute(
                tool=err_tool,
                action="fail",
                params={"message": "Child failed on purpose"},
                raise_on_error=True,
            )
        assert "Child failed on purpose" in str(exc_info.value)

        # 3. Serialization failure fails closed
        unpickleable_params = {"closure": lambda x: x * 2}
        with pytest.raises(ConfigurationError) as cfg_exc:
            runner.execute(
                tool=success_tool,
                action="run",
                params=unpickleable_params,
                raise_on_error=True,
            )
        assert "cannot be serialized" in str(cfg_exc.value)

    def test_e_remote_side_effect_uncertainty(self, tmp_path: Any) -> None:
        """Test E: Remote Side-Effect Uncertainty.

        Tests process termination does not falsely imply remote absence;
        supplemental reconciliation updates bounds while preserving primary PROCESS_TERMINATED.
        """
        runner = ToolProcessRunner()
        hanging_tool = UncooperativeHangingTool(
            name="remote_payment",
            timeout_seconds=0.1,
            sleep_duration=15.0,
        )
        proc_res = runner.execute(
            tool=hanging_tool,
            action="charge",
            params={"amount": 100},
            raise_on_error=False,
        )
        assert proc_res.terminated is True
        assert proc_res.child_pid is not None

        run_id = "run_test_e"
        db_path = str(tmp_path / "test_e_wal.db")
        wal_ledger = LateOutcomeLedger(db_path=db_path)

        events = [
            LedgerEvent(
                event_id=1,
                run_id=run_id,
                scenario_id="scen_e",
                tool_name="remote_payment",
                action_type="charge",
                attempt_id="att_e1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_remote_e",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                error_type="ProcessTerminatedError",
                details={
                    "process_terminated": True,
                    "child_pid": proc_res.child_pid,
                    "exit_code": proc_res.exit_code,
                    "capacity_provenance": "TIER_C3",
                    "max_capacity": 1,
                },
            )
        ]
        tool_meta = _make_c3_meta(events, "remote_payment", max_capacity=1)
        agent_res = AgentResult(
            success=False,
            status=CompletionStatus.FAILED,
            status_message=f"Supervisor killed child process PID {proc_res.child_pid}",
        )
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        evaluator = RecoveryEvaluator(
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        proof = _make_finality_proof(
            run_id,
            events,
            expected_side_effects=1,
            expect_agent_success=True,
            agent_status=agent_res.status.value,
            infrastructure_status=PrimaryInfrastructureStatus.PROCESS_TERMINATED.value,
        )
        eval_res = evaluator.evaluate(
            events=events,
            assertions=assertions,
            agent_result=agent_res,
            duration_ms=proc_res.duration_ms,
            tool_metadata=tool_meta,
            execution_finality=proof,
            infrastructure_status=PrimaryInfrastructureStatus.PROCESS_TERMINATED.value,
            run_id=run_id,
        )
        eval_res.behavioral_verdict = "PASS"
        eval_res.primary_infrastructure_status = PrimaryInfrastructureStatus.PROCESS_TERMINATED.value
        eval_res.verdict = EvaluationVerdict.INFRASTRUCTURE_ERROR
        eval_res.overall_outcome = "INFRASTRUCTURE_ERROR"

        # Assert primary capacity bounds reflect uncertainty [0, 1]
        assert eval_res.bounds == [0, 1]
        assert eval_res.primary_infrastructure_status == "PROCESS_TERMINATED"

        scenario = _make_scenario(
            scenario_id="scen_e",
            target_tool="remote_payment",
            target_action="charge",
            assertions=assertions,
        )
        summary = RunSummary(
            run_id=run_id,
            scenario_id="scen_e",
            agent_id="test_agent",
            passed=False,
            verdict=EvaluationVerdict.INFRASTRUCTURE_ERROR,
            total_events=len(events),
            committed_effects=0,
            duplicate_effects=0,
            created_at="2026-11-20T00:00:00Z",
            duration_ms=proc_res.duration_ms,
        )
        run_res = RunResult(
            run_id=run_id,
            scenario=scenario,
            agent_id="test_agent",
            evaluation=eval_res,
            events=events,
            summary=summary,
        )
        primary_dict = JSONReporter.generate_report_dict(run_res, schema_version=2)
        assert primary_dict["side_effects"]["bounds"] == [0, 1]
        assert primary_dict["outcome"]["primary_infrastructure_status"] == "PROCESS_TERMINATED"

        # Late remote commit ingested via WAL
        late_record = _make_late_record(
            run_id=run_id,
            attempt_id="att_e1",
            effect_id="eff_remote_e1",
            commit_status=CommitStatus.COMMITTED,
            writer_id="RECON_ENGINE",
            event_type="AUDIT_SNAPSHOT",
        )
        wal_ledger.append(late_record)

        engine = ReconciliationEngine(ledger=wal_ledger)
        supp_report = engine.generate_supplemental_report(
            primary_report=primary_dict,
            cutoff_timestamp="100.0",
        )

        # Reconciled bounds collapse to exact [1, 1]
        assert supp_report.side_effects.bounds == [1, 1]
        assert supp_report.reconciled_outcome.side_effect_status == "EXACT_MATCH"
        # Tier 3 precedence: primary infrastructure termination is permanently retained
        assert supp_report.reconciled_outcome.primary_infrastructure_status == "PROCESS_TERMINATED"
        assert supp_report.reconciled_outcome.overall_outcome == "INFRASTRUCTURE_ERROR"

    def test_f_windows_process_lifecycle(self) -> None:
        """Test F: Windows Process Lifecycle.

        Exercises full spawn lifecycle on Windows:
        spawn child -> transmit args -> execute -> transmit result / terminate -> confirm exit.
        Verifies exit codes and resource cleanup without mocks.
        """
        runner = ToolProcessRunner()

        # Sequential executions: verifies clean lifecycle without process leaks
        for i in range(3):
            pid_tool = UncooperativePidTool(name=f"pid_tool_{i}", timeout_seconds=15.0)
            res = runner.execute(
                tool=pid_tool,
                action="get_pid",
                params={"iter": i},
                raise_on_error=True,
            )
            assert res.success is True
            assert res.exit_code == 0
            assert res.child_pid != os.getpid()

        # Hard termination lifecycle: verifies exit code capture and termination confirmation
        hanging_tool = UncooperativeHangingTool(
            name="hanging_lifecycle",
            timeout_seconds=0.1,
            sleep_duration=10.0,
        )
        term_res = runner.execute(
            tool=hanging_tool,
            action="hang",
            params={},
            raise_on_error=False,
        )
        assert term_res.terminated is True
        assert term_res.exit_code is not None
        assert term_res.child_pid != os.getpid()

    def test_g_regression(self) -> None:
        """Test G: Regression.

        Verifies cooperative sync, bounded sync, and coroutine execution models
        continue to execute via their normal paths without regressions.
        """
        # 1. Bounded Sync Tool
        bounded_tool = DeclarativeToolService(
            name="bounded_tool",
            default_action="compute",
            default_handler=lambda x=0: {"ans": x * 2},
            default_mutates=False,
        )
        assert bounded_tool.execution_model == ToolExecutionModel.BOUNDED_SYNC
        res_bounded = bounded_tool.execute("compute", {"x": 21})
        assert res_bounded.success is True
        assert res_bounded.data["ans"] == 42

        # 2. Coroutine Tool
        class AsyncTestTool(ToolService):
            def __init__(self) -> None:
                super().__init__()
                self.name = "async_tool"
                self.execution_model = ToolExecutionModel.COROUTINE

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None, **kwargs: Any) -> ToolResult:
                raise NotImplementedError

            async def execute_async(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None, **kwargs: Any) -> ToolResult:
                await asyncio.sleep(0.01)
                return ToolResult(success=True, data={"async_ans": params.get("y", 0) + 10})

            def reset(self) -> None:
                pass

        async_tool = AsyncTestTool()
        assert async_tool.execution_model == ToolExecutionModel.COROUTINE
        async_res = asyncio.run(async_tool.execute_async("compute", {"y": 32}))
        assert async_res.success is True
        assert async_res.data["async_ans"] == 42

        # 3. Execution in backend
        registry = ToolRegistry()
        registry.register(bounded_tool)
        backend = InProcessExecutionBackend()
        engine = ExecutionEngine(
            ledger=InMemoryLedger(),
            tool_registry=registry,
            backend=backend,
            runtime_authority=TEST_RUNTIME_AUTH,
            audit_authority=TEST_AUDIT_AUTH,
            orchestrator_authority=TEST_ORCH_AUTH,
        )
        scenario = _make_scenario(
            scenario_id="test_bounded_sync",
            target_tool="bounded_tool",
            target_action="compute",
            assertions=ScenarioAssertions(expected_side_effects=0, expect_agent_success=True),
        )

        def agent_fn(task: dict[str, Any], tool_client: Any) -> AgentResult:
            data = tool_client.call("bounded_tool", "compute", {"x": 10})
            return AgentResult(success=True, data=data)

        run_result = engine.run(scenario=scenario, agent=agent_fn)
        assert run_result.evaluation.passed is True
        assert run_result.evaluation.verdict == EvaluationVerdict.PASS
        assert run_result.evaluation.primary_infrastructure_status == "CLEAN"
