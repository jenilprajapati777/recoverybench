"""Integration tests for the Execution Engine lifecycle."""

from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.clock import VirtualClock
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.faults.types import FaultConfig, FaultType
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.models import CommitStatus, ExecutionStatus
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


def test_execution_engine_timeout_before_commit() -> None:
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    clock = VirtualClock()

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="scen_pre_timeout",
        name="Pre-Commit Timeout",
        description="Fails before commit",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.TIMEOUT_BEFORE_COMMIT,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=1,
            )
        ],
        task={"amount": 4200, "currency": "USD"},
        assertions=ScenarioAssertions(expected_side_effects=1),
    )

    engine = ExecutionEngine(ledger=ledger, tool_registry=tools, clock=clock)
    agent = IdempotentPaymentAgent()
    result = engine.run(scenario, agent)

    assert result.evaluation.passed is True
    assert len(result.events) == 2
    # First attempt: fault injected, NOT committed
    assert result.events[0].execution_status == ExecutionStatus.FAULT_INJECTED
    assert result.events[0].commit_status == CommitStatus.NOT_APPLICABLE
    # Second attempt: success, committed
    assert result.events[1].execution_status == ExecutionStatus.SUCCESS
    assert result.events[1].commit_status == CommitStatus.COMMITTED
    assert result.evaluation.actual_side_effects == 1


def test_circuit_breaker_trips_on_excessive_retries() -> None:
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())

    # Scenario that never succeeds (failure count 10)
    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="scen_infinite_fail",
        name="Infinite Failure",
        description="Always fails to test breaker",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.TIMEOUT_BEFORE_COMMIT,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=10,
            )
        ],
        task={"amount": 1000},
        assertions=ScenarioAssertions(expected_side_effects=1),
    )

    # Engine limits max_attempts to 3
    engine = ExecutionEngine(ledger=ledger, tool_registry=tools, max_attempts=3)
    agent = IdempotentPaymentAgent(max_retries=10)
    result = engine.run(scenario, agent)

    assert result.evaluation.passed is False
    error_msg = str(result.agent_raw_result.get("error", "") if result.agent_raw_result else "")
    assert "Circuit breaker tripped" in error_msg
    assert len(result.events) == 3


def test_virtual_clock_zero_wall_sleep() -> None:
    import time
    ledger = InMemoryLedger()
    tools = ToolRegistry()
    tools.register(MockPaymentService())
    clock = VirtualClock()

    scenario = ScenarioModel(
        schema_version=1,
        scenario_id="scen_sleep_test",
        name="Sleep Test",
        description="Verify virtual sleep doesn't block real time",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.HTTP_429_THEN_SUCCESS,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=2,
                retry_after_seconds=5.0,  # 5 seconds virtual sleep per retry
            )
        ],
        task={"amount": 2000},
        assertions=ScenarioAssertions(expected_side_effects=1),
    )

    engine = ExecutionEngine(ledger=ledger, tool_registry=tools, clock=clock)
    agent = IdempotentPaymentAgent()

    start_real = time.perf_counter()
    result = engine.run(scenario, agent)
    elapsed_real = time.perf_counter() - start_real

    # Virtual clock should have advanced by at least 10 simulated seconds
    assert clock.elapsed_seconds() >= 10.0
    # But real wall-clock elapsed time should be well under 0.25 seconds!
    assert elapsed_real < 0.25
    assert result.evaluation.passed is True
