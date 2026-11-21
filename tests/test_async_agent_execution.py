"""Comprehensive tests for asynchronous agent execution (Workstream C)."""

import asyncio
import time
import warnings
from typing import Any

import pytest

import recoverybench as rb
from recoverybench.errors import ConfigurationError
from recoverybench.models import CommitStatus, ExecutionStatus
from recoverybench.sdk.tester import resolve_scenario


def test_basic_async_agent_execution() -> None:
    """1. Test basic async agent execution using await test_agent_async()."""
    async def _test() -> None:
        @rb.agent(id="async_basic_agent")
        async def my_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            await asyncio.sleep(0.01)
            for attempt in range(1, 4):
                try:
                    res = await tool_call(
                        "payment_gateway",
                        "charge",
                        task.task_params,
                        idempotency_key="async_key_1",
                    )
                    return rb.AgentResult(success=True, data=res, attempts=attempt)
                except Exception:
                    continue
            return rb.AgentResult(success=False)

        result = await rb.test_agent_async("payment_timeout_before_commit", my_agent)
        assert result.passed is True
        assert result.verdict == "PASS"

    asyncio.run(_test())


def test_async_agent_various_return_types() -> None:
    """2. Test that async agents returning bool, dict, and AgentResult normalize correctly."""
    async def _test() -> None:
        # bool True
        async def bool_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> bool:
            await asyncio.sleep(0.001)
            for _ in range(3):
                try:
                    await tool_call("payment_gateway", "charge", task.task_params, idempotency_key="k_bool")
                    return True
                except Exception:
                    pass
            return False

        res_bool = await rb.test_agent_async("payment_timeout_before_commit", bool_agent)
        assert res_bool.passed is True

        # dict
        async def dict_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> dict[str, Any]:
            await asyncio.sleep(0.001)
            for _ in range(3):
                try:
                    res = await tool_call("payment_gateway", "charge", task.task_params, idempotency_key="k_dict")
                    return {"success": True, "charge": res, "attempts": 2}
                except Exception:
                    pass
            return {"success": False}

        res_dict = await rb.test_agent_async("payment_timeout_before_commit", dict_agent)
        assert res_dict.passed is True
        assert res_dict.evaluation.passed is True

    asyncio.run(_test())


def test_async_agent_with_await_tool_call_and_retry() -> None:
    """3. Test async agent awaiting tool calls with idempotent retry under fault injection."""
    async def _test() -> None:
        @rb.agent(id="async_resilient_agent")
        async def resilient_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            stable_key = "async_stable_key_999"
            for attempt in range(1, 4):
                try:
                    # Test 'await tool_call(...)'
                    res = await tool_call(
                        "payment_gateway",
                        "charge",
                        task.task_params,
                        idempotency_key=stable_key,
                    )
                    return rb.AgentResult(success=True, data=res, attempts=attempt)
                except Exception:
                    await asyncio.sleep(0.005)
                    continue
            return rb.AgentResult(success=False, error="Max retries reached")

        result = await rb.test_agent_async("payment_timeout_after_commit", resilient_agent)
        assert result.passed is True
        assert result.verdict == "PASS"
        assert result.evaluation.actual_side_effects == 1
        assert result.evaluation.duplicate_effect_count == 0

    asyncio.run(_test())


def test_async_agent_naive_retry_triggers_duplicate_detection() -> None:
    """4. Test that an async agent that fails to preserve idempotency keys is flagged for duplicates."""
    async def _test() -> None:
        @rb.agent(id="async_naive_agent")
        async def naive_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            for attempt in range(1, 4):
                try:
                    # Unsafe: uses different key on each attempt
                    fresh_key = f"unstable_async_key_{attempt}"
                    res = await tool_call(
                        "payment_gateway",
                        "charge",
                        task.task_params,
                        idempotency_key=fresh_key,
                    )
                    return rb.AgentResult(success=True, data=res, attempts=attempt)
                except Exception:
                    await asyncio.sleep(0.005)
                    continue
            return rb.AgentResult(success=False, error="Max retries reached")

        result = await rb.test_agent_async("payment_timeout_after_commit", naive_agent)
        assert result.passed is False
        assert result.verdict == "GENUINE_FAILURE"
        assert result.evaluation.actual_side_effects == 2
        assert result.evaluation.duplicate_effect_count == 1

    asyncio.run(_test())


def test_async_agent_raising_exception() -> None:
    """5. Test that unhandled exceptions inside async agent code are safely quarantined."""
    async def _test() -> None:
        @rb.agent
        async def failing_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            await asyncio.sleep(0.001)
            raise ValueError("Simulated async runtime failure inside agent")

        result = await rb.test_agent_async("payment_timeout_before_commit", failing_agent)
        assert result.passed is False
        assert "ValueError" in str(result.summary.verdict) or not result.passed

    asyncio.run(_test())


def test_async_tool_with_declarative_mock() -> None:
    """6. Test async declarative mock tool invoked by async agent."""
    async def _test() -> None:
        @rb.mock_tool(name="async_database", action="write", mutates=True)
        async def async_write(record_id: str, payload: dict[str, Any]) -> dict[str, Any]:
            await asyncio.sleep(0.005)
            return {"written": True, "record_id": record_id}

        @rb.agent
        async def db_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            res = await tool_call("async_database", "write", {"record_id": "rec_001", "payload": {}})
            return rb.AgentResult(success=True, data=res)

        result = await rb.test_agent_async(
            "payment_timeout_before_commit",
            db_agent,
            custom_tools=async_write,
        )
        assert result.passed is True

    asyncio.run(_test())


def test_async_tester_context_manager() -> None:
    """7. Test async context manager with RecoveryBenchTester.test_async()."""
    async def _test() -> None:
        async with rb.RecoveryBenchTester() as tester:
            @rb.agent
            async def agent1(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
                for _ in range(3):
                    try:
                        res = await tool_call("payment_gateway", "charge", task.task_params, idempotency_key="idem_k1")
                        return rb.AgentResult(success=True, data=res)
                    except Exception:
                        pass
                return rb.AgentResult(success=False)

            res = await tester.test_async("payment_timeout_before_commit", agent1)
            assert res.passed is True

    asyncio.run(_test())


def test_async_execution_with_preexisting_event_loop() -> None:
    """8. Test async execution while caller already has a running event loop."""
    async def caller_with_active_loop() -> None:
        # Caller has an active running event loop
        assert asyncio.get_running_loop() is not None

        @rb.agent
        async def loop_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            await asyncio.sleep(0.001)
            for _ in range(3):
                try:
                    res = await tool_call("payment_gateway", "charge", task.task_params, idempotency_key="loop_k1")
                    return rb.AgentResult(success=True, data=res)
                except Exception:
                    pass
            return rb.AgentResult(success=False)

        # Call test_agent_async while the loop is running without nested event loop errors
        res = await rb.test_agent_async("payment_timeout_before_commit", loop_agent)
        assert res.passed is True

    asyncio.run(caller_with_active_loop())


def test_sync_test_agent_rejects_async_callable() -> None:
    """9. Test that calling synchronous test_agent() on an async def function raises ConfigurationError."""
    async def my_async_fn(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
        return rb.AgentResult(success=True)

    with pytest.raises(ConfigurationError) as exc_info:
        rb.test_agent("payment_timeout_before_commit", my_async_fn)
    assert "asynchronous function ('async def')" in str(exc_info.value)
    assert "test_agent_async" in str(exc_info.value)


def test_no_unawaited_coroutine_warnings() -> None:
    """10. Test that running async tests emits zero unawaited coroutine warnings."""
    async def _test() -> None:
        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")

            @rb.agent
            async def clean_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
                await asyncio.sleep(0.001)
                for _ in range(3):
                    try:
                        res = await tool_call(
                            "payment_gateway", "charge", task.task_params, idempotency_key="clean_k1"
                        )
                        return rb.AgentResult(success=True, data=res)
                    except Exception:
                        pass
                return rb.AgentResult(success=False)

            res = await rb.test_agent_async("payment_timeout_before_commit", clean_agent)
            assert res.passed is True

            coroutine_warnings = [
                w for w in recorded_warnings
                if "coroutine" in str(w.message).lower() and "was never awaited" in str(w.message).lower()
            ]
            assert len(coroutine_warnings) == 0

    asyncio.run(_test())


def test_isolated_docker_execution_fails_closed_for_async_agents() -> None:
    """11. Test that requesting isolated execution with test_agent_async fails closed with helpful guidance."""
    async def _test() -> None:
        async def sample_async(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            return rb.AgentResult(success=True)

        with pytest.raises(ConfigurationError) as exc_info:
            await rb.test_agent_async("payment_timeout_before_commit", sample_async, isolation="isolated")
        assert "Docker container execution currently only supports synchronous agents" in str(exc_info.value)

    asyncio.run(_test())


def test_async_timeout_and_scenario_timeout() -> None:
    """12. Test that scenario-specific timeout triggers timeout error and records failure verdict."""
    async def _test() -> None:
        base_scenario = resolve_scenario("payment_timeout_before_commit")
        fast_timeout_scenario = base_scenario.model_copy(
            update={"task": {**base_scenario.task, "timeout_seconds": 0.05}}
        )

        @rb.agent
        async def sleeping_agent(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            await asyncio.sleep(0.5)
            return rb.AgentResult(success=True)

        start_time = time.perf_counter()
        result = await rb.test_agent_async(fast_timeout_scenario, sleeping_agent)
        duration = time.perf_counter() - start_time

        assert duration < 0.35, f"Execution took {duration}s, should have timed out around 0.05s"
        assert result.passed is False
        assert result.verdict in ("GENUINE_FAILURE", "INFRASTRUCTURE_ERROR")
        err_msg = str(result.agent_raw_result.get("error") if result.agent_raw_result else "")
        assert "TimeoutError" in err_msg or "timed out" in err_msg

    asyncio.run(_test())


def test_ledger_consistency_after_timeout() -> None:
    """13. Test that committed effects prior to timeout are safely preserved in the ledger."""
    async def _test() -> None:
        base_scenario = resolve_scenario("payment_timeout_after_commit")
        custom_scenario = base_scenario.model_copy(
            update={"task": {**base_scenario.task, "timeout_seconds": 0.08}}
        )

        @rb.agent
        async def committing_then_sleeping_agent(
            task: rb.TaskContext, tool_call: rb.ToolClient
        ) -> rb.AgentResult:
            try:
                await tool_call(
                    "payment_gateway",
                    "charge",
                    task.task_params,
                    idempotency_key="key_pre_timeout",
                )
            except Exception:
                pass
            # Agent hangs after the tool call has committed
            await asyncio.sleep(0.5)
            return rb.AgentResult(success=True)

        result = await rb.test_agent_async(custom_scenario, committing_then_sleeping_agent)
        assert result.passed is False
        committed_events = [e for e in result.events if e.commit_status == CommitStatus.COMMITTED]
        assert len(committed_events) >= 1
        assert committed_events[0].tool_name == "payment_gateway"

    asyncio.run(_test())


def test_async_agent_cancellation_and_ledger_consistency() -> None:
    """14. Test that cancelling an async agent does not wipe committed tool history or ledger evidence."""
    async def _test() -> None:
        tool_called_event = asyncio.Event()

        @rb.agent
        async def agent_to_cancel(task: rb.TaskContext, tool_call: rb.ToolClient) -> rb.AgentResult:
            try:
                await tool_call(
                    "payment_gateway",
                    "charge",
                    task.task_params,
                    idempotency_key="cancel_test_key",
                )
            except Exception:
                pass
            tool_called_event.set()
            await asyncio.sleep(10.0)
            return rb.AgentResult(success=True)

        tester = rb.RecoveryBenchTester()
        task = asyncio.create_task(
            tester.test_async("payment_timeout_after_commit", agent_to_cancel, run_id="cancel_run_001")
        )
        await tool_called_event.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Verify tool history was NOT wiped on cancellation
        payment_service = tester.tools.get("payment_gateway")
        assert isinstance(payment_service, rb.MockPaymentService)
        assert len(payment_service.charges) >= 1
        assert "cancel_test_key" in payment_service.idempotency_map

        # Verify ledger events recorded before cancellation remain intact
        events = tester.ledger.get_events("cancel_run_001")
        assert len(events) >= 1
        assert any(e.tool_name == "payment_gateway" for e in events)

    asyncio.run(_test())


def test_synchronous_blocking_tool_offloaded_in_async_execution() -> None:
    """15. Test that synchronous tool handlers are offloaded to worker threads and do not block the event loop."""
    async def _test() -> None:
        @rb.mock_tool(name="blocking_sync_service", action="compute", mutates=False)
        def sync_compute_handler(duration_ms: int = 50) -> dict[str, Any]:
            time.sleep(duration_ms / 1000.0)
            return {"computed": True}

        ticks = 0
        stop_ticker = asyncio.Event()

        async def loop_ticker() -> None:
            nonlocal ticks
            while not stop_ticker.is_set():
                ticks += 1
                await asyncio.sleep(0.005)

        @rb.agent
        async def async_agent_with_sync_tool(
            task: rb.TaskContext, tool_call: rb.ToolClient
        ) -> rb.AgentResult:
            ticker_task = asyncio.create_task(loop_ticker())
            await tool_call(
                "blocking_sync_service",
                "compute",
                {"duration_ms": 60},
                operation_id="compute_op",
            )
            stop_ticker.set()
            await ticker_task

            # Satisfy scenario payment
            for attempt in range(1, 4):
                try:
                    res = await tool_call(
                        "payment_gateway",
                        "charge",
                        task.task_params,
                        idempotency_key="sync_offload_k1",
                    )
                    return rb.AgentResult(success=True, data=res, attempts=attempt)
                except Exception:
                    continue
            return rb.AgentResult(success=False)

        result = await rb.test_agent_async(
            "payment_timeout_before_commit",
            async_agent_with_sync_tool,
            custom_tools=[sync_compute_handler, rb.MockPaymentService()],
        )
        assert result.passed is True
        # If the sync tool blocked the event loop, ticks would be 0 or 1.
        # With asyncio.to_thread, loop_ticker runs concurrently and ticks multiple times.
        assert ticks >= 3, f"Expected event loop ticks >= 3, got {ticks}"

    asyncio.run(_test())


def test_async_tool_exception_recorded_in_ledger() -> None:
    """16. Test that tool handler exceptions record an ERROR ledger entry before bubbling."""
    async def _test() -> None:
        @rb.mock_tool(name="sync_failing_disk", action="flush", mutates=True)
        def failing_sync_handler(file_id: str) -> dict[str, Any]:
            raise RuntimeError("Disk write failed: out of space")

        @rb.mock_tool(name="async_failing_stream", action="transmit", mutates=True)
        async def failing_async_handler(packet_id: str) -> dict[str, Any]:
            await asyncio.sleep(0.002)
            raise ConnectionResetError("Stream connection reset by peer")

        @rb.agent
        async def agent_handling_tool_errors(
            task: rb.TaskContext, tool_call: rb.ToolClient
        ) -> rb.AgentResult:
            sync_caught = False
            async_caught = False

            try:
                await tool_call("sync_failing_disk", "flush", {"file_id": "f_001"})
            except RuntimeError:
                sync_caught = True

            try:
                await tool_call("async_failing_stream", "transmit", {"packet_id": "p_001"})
            except ConnectionResetError:
                async_caught = True

            return rb.AgentResult(success=(sync_caught and async_caught))

        tester = rb.RecoveryBenchTester(
            custom_tools=[failing_sync_handler, failing_async_handler]
        )
        result = await tester.test_async(
            "payment_timeout_before_commit", agent_handling_tool_errors
        )
        assert result.agent_raw_result is not None
        assert result.agent_raw_result.get("success") is True

        events = tester.get_ledger_events(result.run_id)
        error_events = [e for e in events if e.execution_status == ExecutionStatus.ERROR]
        assert len(error_events) == 2

        # Verify sync tool failure details
        sync_err = next(e for e in error_events if e.tool_name == "sync_failing_disk")
        assert sync_err.commit_status == CommitStatus.ABORTED
        assert sync_err.error_type == "RuntimeError"
        assert "Disk write failed" in str(sync_err.details.get("error", ""))

        # Verify async tool failure details
        async_err = next(e for e in error_events if e.tool_name == "async_failing_stream")
        assert async_err.commit_status == CommitStatus.ABORTED
        assert async_err.error_type == "ConnectionResetError"
        assert "Stream connection reset" in str(async_err.details.get("error", ""))

    asyncio.run(_test())
