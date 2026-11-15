"""High-level SDK test runner and testing harness."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from recoverybench.adapters.callable import wrap_agent
from recoverybench.agent.base import AgentAdapter
from recoverybench.clock import VirtualClock
from recoverybench.engine.context import RunResult
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.errors import ConfigurationError
from recoverybench.ledger.base import Ledger
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.ledger.sqlite import SQLiteLedger
from recoverybench.models import LedgerEvent
from recoverybench.sandbox.backend import (
    DockerExecutionBackend,
    ExecutionBackend,
    InProcessExecutionBackend,
)
from recoverybench.sandbox.spec import AgentSpec
from recoverybench.scenarios.registry import ScenarioRegistry
from recoverybench.scenarios.schema import ScenarioModel
from recoverybench.scenarios.validator import load_scenario
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService


def resolve_scenario(scenario_input: str | Path | ScenarioModel) -> ScenarioModel:
    """Resolve a scenario identifier, file path, or instance into a ScenarioModel."""
    if isinstance(scenario_input, ScenarioModel):
        return scenario_input

    scenario_str = str(scenario_input)

    # 1. Check if it's a file on disk
    path = Path(scenario_str)
    if path.exists() and path.is_file():
        return load_scenario(path)

    # 2. Check built-in registry
    registry = ScenarioRegistry.get_default()
    found = registry.get(scenario_str)
    if found:
        return found

    raise ConfigurationError(
        f"Scenario '{scenario_str}' not found in built-ins or local filesystem. "
        f"Available built-in scenarios: {[s.scenario_id for s in registry.list_all()]}"
    )


def test_agent(
    scenario: str | Path | ScenarioModel,
    agent: AgentAdapter | AgentSpec | Callable[..., Any],
    *,
    run_id: str | None = None,
    seed: int | None = None,
    max_attempts: int | None = None,
    ledger_type: str = "memory",
    db_path: str | Path | None = None,
    custom_tools: ToolRegistry | None = None,
    backend: ExecutionBackend | None = None,
    isolation: str = "trusted",
) -> RunResult:
    """Run a deterministic recovery test against an agent adapter, spec, or callable.

    Args:
        scenario: Scenario ID (e.g. 'payment_timeout_after_commit'), YAML path, or ScenarioModel.
        agent: AgentAdapter instance, AgentSpec, or generic Python callable.
        run_id: Optional unique identifier for this test run.
        seed: Deterministic PRNG seed override.
        max_attempts: Circuit breaker threshold for maximum tool calls.
        ledger_type: 'memory' for fast ephemeral tests, or 'sqlite' for persistent storage.
        db_path: SQLite database file path (if ledger_type is 'sqlite').
        custom_tools: Custom ToolRegistry; defaults to registry with MockPaymentService.
        backend: Optional ExecutionBackend instance.
        isolation: Isolation mode ('trusted' or 'isolated') if backend is not explicitly provided.

    Returns:
        RunResult containing evaluation verdict, assertion outcomes, and cited ledger events.

    Example:
        >>> from recoverybench.sdk import test_agent
        >>> result = test_agent("payment_timeout_after_commit", my_agent)
        >>> assert result.passed
    """
    model = resolve_scenario(scenario)
    if seed is not None:
        model = model.model_copy(update={"seed": seed})

    # Prepare ledger
    ledger: Ledger
    if ledger_type == "sqlite":
        path = str(db_path) if db_path else ":memory:"
        ledger = SQLiteLedger(path)
    else:
        ledger = InMemoryLedger()

    # Prepare tool registry
    tools = custom_tools
    if tools is None:
        tools = ToolRegistry()
        tools.register(MockPaymentService())

    # Resolve backend
    active_backend = backend
    if active_backend is None:
        clean_iso = isolation.strip().lower()
        if clean_iso in ("isolated", "docker", "sandbox"):
            active_backend = DockerExecutionBackend()
        else:
            active_backend = InProcessExecutionBackend()

    engine = ExecutionEngine(
        ledger=ledger,
        tool_registry=tools,
        clock=VirtualClock(),
        max_attempts=max_attempts,
        backend=active_backend,
    )

    return engine.run(model, agent, run_id=run_id, backend=active_backend)


class RecoveryBenchTester:
    """Context manager and testing harness for running multiple recovery evaluations."""

    def __init__(
        self,
        *,
        ledger_type: str = "memory",
        db_path: str | Path | None = None,
        custom_tools: ToolRegistry | None = None,
        default_seed: int = 42,
    ) -> None:
        self.ledger_type = ledger_type
        self.db_path = db_path
        self.default_seed = default_seed

        if ledger_type == "sqlite":
            path = str(db_path) if db_path else ":memory:"
            self.ledger: Ledger = SQLiteLedger(path)
        else:
            self.ledger = InMemoryLedger()

        self.tools = custom_tools or ToolRegistry()
        if not self.tools.get("payment_gateway"):
            self.tools.register(MockPaymentService())

        self.clock = VirtualClock()
        self.engine = ExecutionEngine(
            ledger=self.ledger,
            tool_registry=self.tools,
            clock=self.clock,
        )

    def test(
        self,
        scenario: str | Path | ScenarioModel,
        agent: AgentAdapter | Callable[..., Any],
        *,
        run_id: str | None = None,
        seed: int | None = None,
    ) -> RunResult:
        """Execute a recovery test using this tester's ledger and environment."""
        model = resolve_scenario(scenario)
        target_seed = seed if seed is not None else self.default_seed
        model = model.model_copy(update={"seed": target_seed})

        resolved_agent = agent if isinstance(agent, AgentAdapter) else wrap_agent(agent)
        return self.engine.run(model, resolved_agent, run_id=run_id)

    def get_ledger_events(self, run_id: str) -> list[LedgerEvent]:
        """Fetch all recorded effect ledger events for a specific run."""
        return self.ledger.get_events(run_id)

    def reset(self) -> None:
        """Reset virtual clock and tools state."""
        self.clock.reset()
        self.tools.reset_all()

    def __enter__(self) -> "RecoveryBenchTester":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.reset()
