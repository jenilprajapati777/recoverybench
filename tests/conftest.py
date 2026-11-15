"""Pytest configuration and shared fixtures for RecoveryBench."""

from pathlib import Path

import pytest

from recoverybench.clock import VirtualClock
from recoverybench.config import Settings, set_settings
from recoverybench.faults.types import FaultConfig, FaultType
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.ledger.sqlite import SQLiteLedger
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


@pytest.fixture
def temp_env(tmp_path: Path) -> Settings:
    """Configure temporary isolated data and reports directory."""
    settings = Settings(
        data_dir=tmp_path / ".recoverybench",
        scenarios_dir=tmp_path / "scenarios",
        db_path=tmp_path / ".recoverybench" / "ledger.db",
        reports_dir=tmp_path / ".recoverybench" / "reports",
        max_attempts=5,
        default_timeout_seconds=5.0,
    )
    set_settings(settings)
    settings.ensure_directories()
    return settings


@pytest.fixture
def virtual_clock() -> VirtualClock:
    """Deterministic virtual clock."""
    return VirtualClock(start_timestamp=1704067200.0)


@pytest.fixture
def memory_ledger() -> InMemoryLedger:
    """In-memory append-only ledger."""
    return InMemoryLedger()


@pytest.fixture
def sqlite_ledger(tmp_path: Path) -> SQLiteLedger:
    """Isolated SQLite ledger file."""
    return SQLiteLedger(tmp_path / "test_ledger.db")


@pytest.fixture
def tool_registry() -> ToolRegistry:
    """Tool registry containing fresh mock payment gateway."""
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    return registry


@pytest.fixture
def timeout_after_scenario() -> ScenarioModel:
    """Standard scenario testing timeout_after_commit."""
    return ScenarioModel(
        scenario_id="payment_timeout_after_commit",
        name="Payment Timeout After Commit Test",
        description="Drops response after commit to test idempotency recovery",
        target_tool="payment_gateway",
        target_action="charge",
        faults=[
            FaultConfig(
                type=FaultType.TIMEOUT_AFTER_COMMIT,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=1,
            )
        ],
        task={"amount": 5000, "currency": "USD", "customer_id": "cust_test_123"},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
            max_allowed_retries=3,
            expect_agent_success=True,
        ),
    )


@pytest.fixture
def timeout_before_scenario() -> ScenarioModel:
    """Standard scenario testing timeout_before_commit."""
    return ScenarioModel(
        scenario_id="payment_timeout_before_commit",
        name="Payment Timeout Before Commit Test",
        description="Fails socket connection before server receives request",
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
        task={"amount": 3500, "currency": "USD", "customer_id": "cust_test_456"},
        assertions=ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
            max_allowed_retries=3,
            expect_agent_success=True,
        ),
    )
