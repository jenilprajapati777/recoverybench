"""Unit tests for the Replay Engine."""

from pathlib import Path

from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.ledger.sqlite import SQLiteLedger
from recoverybench.replay.engine import ReplayEngine
from recoverybench.scenarios.schema import ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


def test_replay_historical_run(tmp_path: Path, timeout_after_scenario: ScenarioModel) -> None:
    db_path = tmp_path / "replay.db"
    ledger = SQLiteLedger(db_path)
    tools = ToolRegistry()
    tools.register(MockPaymentService())

    engine = ExecutionEngine(ledger=ledger, tool_registry=tools)
    agent = IdempotentPaymentAgent()
    run_result = engine.run(timeout_after_scenario, agent, run_id="run_replay_test_01")

    # Replay
    replay_engine = ReplayEngine(ledger)
    trace = replay_engine.replay_run("run_replay_test_01")

    assert trace is not None
    assert trace.run_id == "run_replay_test_01"
    assert trace.summary.passed is True
    assert len(trace.events) == len(run_result.events)
    assert len(trace.reconstructed_timeline) == len(run_result.events)

    # Check first step in reconstructed trace
    step1 = trace.reconstructed_timeline[0]
    assert step1["attempt"] == 1
    assert step1["tool"] == "payment_gateway"
    assert step1["action"] == "charge"
    assert step1["execution_status"] == "FAULT_INJECTED"
    assert step1["commit_status"] == "COMMITTED"


def test_replay_nonexistent_run(tmp_path: Path) -> None:
    ledger = SQLiteLedger(tmp_path / "replay_empty.db")
    replay_engine = ReplayEngine(ledger)
    assert replay_engine.replay_run("nonexistent_run_id") is None
