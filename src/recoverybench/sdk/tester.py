"""High-level SDK test runner and testing harness."""

import inspect
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

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
from recoverybench.tools.base import ToolRegistry, ToolService
from recoverybench.tools.payment import MockPaymentService

ISOLATED_ALIASES: frozenset[str] = frozenset({"isolated", "docker", "sandbox"})
TRUSTED_ALIASES: frozenset[str] = frozenset({"trusted", "in-process", "inprocess", "local"})
SUPPORTED_ISOLATION_MODES: frozenset[str] = ISOLATED_ALIASES | TRUSTED_ALIASES
SUPPORTED_LEDGER_TYPES: frozenset[str] = frozenset({"memory", "sqlite"})


def resolve_tool_registry(
    custom_tools: ToolRegistry | ToolService | Sequence[ToolService] | None = None,
) -> ToolRegistry:
    """Resolve and validate custom tool inputs into a ToolRegistry instance.

    Supports:
    - None: returns default ToolRegistry with MockPaymentService registered.
    - ToolRegistry instance: returned as-is.
    - ToolService instance: registered in a new ToolRegistry.
    - Sequence[ToolService]: all tools registered in a new ToolRegistry.
    """
    if custom_tools is None:
        tools = ToolRegistry()
        tools.register(MockPaymentService())
        return tools

    if isinstance(custom_tools, ToolRegistry):
        return custom_tools

    if isinstance(custom_tools, ToolService):
        registry = ToolRegistry()
        registry.register(custom_tools)
        return registry

    if isinstance(custom_tools, (list, tuple, Sequence)) and not isinstance(custom_tools, (str, bytes)):
        registry = ToolRegistry()
        for idx, item in enumerate(custom_tools):
            if not isinstance(item, ToolService):
                raise ConfigurationError(
                    f"Invalid item in custom_tools at index {idx}: expected ToolService, got {type(item).__name__}."
                )
            registry.register(item)
        return registry

    raise ConfigurationError(
        f"Invalid custom_tools parameter: expected ToolRegistry, ToolService, or Sequence[ToolService], "
        f"got {type(custom_tools).__name__}."
    )


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


def resolve_ledger(
    ledger_type: str = "memory",
    db_path: str | Path | None = None,
) -> Ledger:
    """Resolve and validate the effect ledger implementation.

    Args:
        ledger_type: 'memory' for ephemeral in-memory ledger, 'sqlite' for persistent storage.
        db_path: Optional SQLite database file path (if ledger_type is 'sqlite').

    Returns:
        The instantiated Ledger implementation.

    Raises:
        ConfigurationError: If ledger_type is not supported.
    """
    if not isinstance(ledger_type, str):
        raise ConfigurationError(
            f"Invalid ledger_type: expected string, got {type(ledger_type).__name__}."
        )

    clean_ledger = ledger_type.strip().lower()
    if clean_ledger == "memory":
        return InMemoryLedger()
    elif clean_ledger == "sqlite":
        path = str(db_path) if db_path else ":memory:"
        return SQLiteLedger(path)
    else:
        raise ConfigurationError(
            f"Invalid ledger_type '{ledger_type}'. Supported ledger types: 'memory', 'sqlite'."
        )


def resolve_backend(
    isolation: str = "trusted",
    backend: ExecutionBackend | None = None,
) -> ExecutionBackend:
    """Resolve and validate the execution backend based on explicit backend or isolation mode.

    Precedence:
        If `backend` is explicitly provided, it determines the execution backend.
        If `isolation` is also provided, it is validated against recognized modes
        ('isolated', 'docker', 'sandbox', 'trusted', 'in-process', 'inprocess', 'local')
        to prevent silent misconfigurations, but `backend` takes precedence.

    Args:
        isolation: Isolation mode string alias ('trusted' or 'isolated').
        backend: Explicit ExecutionBackend instance.

    Returns:
        The validated active ExecutionBackend instance.

    Raises:
        ConfigurationError: If isolation is unknown or backend is of invalid type.
    """
    if not isinstance(isolation, str):
        raise ConfigurationError(
            f"Invalid isolation parameter: expected string, got {type(isolation).__name__}."
        )

    clean_iso = isolation.strip().lower()
    if clean_iso not in SUPPORTED_ISOLATION_MODES:
        all_modes = sorted(SUPPORTED_ISOLATION_MODES)
        raise ConfigurationError(
            f"Invalid isolation mode '{isolation}'. Supported modes: {all_modes}. "
            "Isolated aliases: ['docker', 'isolated', 'sandbox']. "
            "Trusted aliases: ['in-process', 'inprocess', 'local', 'trusted']."
        )

    if backend is not None:
        if not isinstance(backend, ExecutionBackend):
            raise ConfigurationError(
                f"Invalid backend parameter: expected ExecutionBackend instance, got {type(backend).__name__}."
            )
        return backend

    if clean_iso in ISOLATED_ALIASES:
        return DockerExecutionBackend()
    return InProcessExecutionBackend()


def test_agent(
    scenario: str | Path | ScenarioModel,
    agent: AgentAdapter | AgentSpec | Callable[..., Any],
    *,
    run_id: str | None = None,
    seed: int | None = None,
    max_attempts: int | None = None,
    ledger_type: str = "memory",
    db_path: str | Path | None = None,
    custom_tools: ToolRegistry | ToolService | Sequence[ToolService] | None = None,
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
        custom_tools: Custom ToolRegistry, ToolService, or list of ToolService instances.
            Defaults to registry with MockPaymentService.
        backend: Optional ExecutionBackend instance. Precedes `isolation` if provided.
        isolation: Isolation mode ('trusted' or 'isolated'). Defaults to 'trusted' (in-process).

    Returns:
        RunResult containing evaluation verdict, assertion outcomes, and cited ledger events.

    Raises:
        ConfigurationError: If isolation mode, ledger type, or scenario is invalid.

    Example:
        >>> from recoverybench.sdk import test_agent
        >>> result = test_agent("payment_timeout_after_commit", my_agent)
        >>> assert result.passed
    """
    raw_fn = getattr(agent, "fn", agent)
    if inspect.iscoroutinefunction(raw_fn):
        fn_name = getattr(raw_fn, "__name__", getattr(agent, "name", str(agent)))
        raise ConfigurationError(
            f"Callable '{fn_name}' is an asynchronous function ('async def'). "
            "Asynchronous agents must be tested using 'await test_agent_async(...)' or 'run_async()' "
            "rather than synchronous 'test_agent()'."
        )

    model = resolve_scenario(scenario)
    if seed is not None:
        model = model.model_copy(update={"seed": seed})

    ledger = resolve_ledger(ledger_type=ledger_type, db_path=db_path)
    tools = resolve_tool_registry(custom_tools)
    active_backend = resolve_backend(isolation=isolation, backend=backend)

    engine = ExecutionEngine(
        ledger=ledger,
        tool_registry=tools,
        clock=VirtualClock(),
        max_attempts=max_attempts,
        backend=active_backend,
    )

    return engine.run(model, agent, run_id=run_id, backend=active_backend)


async def test_agent_async(
    scenario: str | Path | ScenarioModel,
    agent: AgentAdapter | AgentSpec | Callable[..., Any],
    *,
    run_id: str | None = None,
    seed: int | None = None,
    max_attempts: int | None = None,
    ledger_type: str = "memory",
    db_path: str | Path | None = None,
    custom_tools: ToolRegistry | ToolService | Sequence[ToolService] | None = None,
    backend: ExecutionBackend | None = None,
    isolation: str = "trusted",
) -> RunResult:
    """Asynchronously run a deterministic recovery test against an agent adapter, spec, or callable.

    Args:
        scenario: Scenario ID, YAML/JSON path, or ScenarioModel.
        agent: AgentAdapter instance, AgentSpec, or generic Python callable.
        run_id: Optional unique identifier for this test run.
        seed: Deterministic PRNG seed override.
        max_attempts: Circuit breaker threshold for maximum tool calls.
        ledger_type: 'memory' for fast ephemeral tests, or 'sqlite' for persistent storage.
        db_path: SQLite database file path (if ledger_type is 'sqlite').
        custom_tools: Custom ToolRegistry, ToolService, or list of ToolService instances.
        backend: Optional ExecutionBackend instance. Precedes `isolation` if provided.
        isolation: Isolation mode ('trusted' or 'isolated'). Defaults to 'trusted' (in-process).

    Returns:
        RunResult containing evaluation verdict, assertion outcomes, and cited ledger events.

    Example:
        >>> from recoverybench.sdk import test_agent_async
        >>> result = await test_agent_async("payment_timeout_after_commit", my_async_agent)
        >>> assert result.passed
    """
    model = resolve_scenario(scenario)
    if seed is not None:
        model = model.model_copy(update={"seed": seed})

    ledger = resolve_ledger(ledger_type=ledger_type, db_path=db_path)
    tools = resolve_tool_registry(custom_tools)
    active_backend = resolve_backend(isolation=isolation, backend=backend)

    if active_backend.mode.value == "isolated":
        raise ConfigurationError(
            "Isolated Docker container execution currently only supports synchronous agents. "
            "To evaluate asynchronous agents ('async def'), use in-process trusted execution ('--isolation trusted') "
            "with 'await test_agent_async(...)'. Containerized asynchronous IPC execution is scheduled for a future release."
        )

    engine = ExecutionEngine(
        ledger=ledger,
        tool_registry=tools,
        clock=VirtualClock(),
        max_attempts=max_attempts,
        backend=active_backend,
    )

    return await engine.run_async(model, agent, run_id=run_id, backend=active_backend)


class RecoveryBenchTester:
    """Context manager and testing harness for running multiple recovery evaluations."""

    def __init__(
        self,
        *,
        isolation: str = "trusted",
        backend: ExecutionBackend | None = None,
        ledger_type: str = "memory",
        db_path: str | Path | None = None,
        custom_tools: ToolRegistry | ToolService | Sequence[ToolService] | None = None,
        default_seed: int = 42,
    ) -> None:
        """Initialize a RecoveryBenchTester evaluation harness.

        Args:
            isolation: Isolation mode ('trusted' or 'isolated'). Defaults to 'trusted'.
            backend: Optional ExecutionBackend instance. Precedes `isolation` if provided.
            ledger_type: 'memory' for fast ephemeral tests, or 'sqlite' for persistent storage.
            db_path: SQLite database file path (if ledger_type is 'sqlite').
            custom_tools: Custom ToolRegistry, ToolService, or list of ToolService instances.
            default_seed: Default deterministic PRNG seed for runs.

        Raises:
            ConfigurationError: If isolation mode or ledger type is invalid.
        """
        self.ledger_type = ledger_type
        self.db_path = db_path
        self.default_seed = default_seed
        self.isolation = isolation

        self.ledger: Ledger = resolve_ledger(ledger_type=ledger_type, db_path=db_path)
        self.backend: ExecutionBackend = resolve_backend(isolation=isolation, backend=backend)

        self.tools = resolve_tool_registry(custom_tools)

        self.clock = VirtualClock()
        self.engine = ExecutionEngine(
            ledger=self.ledger,
            tool_registry=self.tools,
            clock=self.clock,
            backend=self.backend,
        )

    def test(
        self,
        scenario: str | Path | ScenarioModel,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        *,
        run_id: str | None = None,
        seed: int | None = None,
    ) -> RunResult:
        """Execute a recovery test using this tester's ledger, backend, and environment.

        Supports AgentAdapter instances, declarative AgentSpec objects, or generic callables.

        Args:
            scenario: Scenario identifier, YAML Path, or ScenarioModel.
            agent: AgentAdapter instance, AgentSpec, or generic Python callable.
            run_id: Optional unique identifier for this test run.
            seed: Deterministic PRNG seed override.

        Returns:
            RunResult with evaluation verdict, assertion outcomes, and cited ledger events.
        """
        raw_fn = getattr(agent, "fn", agent)
        if inspect.iscoroutinefunction(raw_fn):
            fn_name = getattr(raw_fn, "__name__", getattr(agent, "name", str(agent)))
            raise ConfigurationError(
                f"Callable '{fn_name}' is an asynchronous function ('async def'). "
                "Asynchronous agents must be tested using 'await test_agent_async(...)' or 'run_async()' "
                "rather than synchronous 'test_agent()'."
            )

        model = resolve_scenario(scenario)
        target_seed = seed if seed is not None else self.default_seed
        model = model.model_copy(update={"seed": target_seed})

        return self.engine.run(model, agent, run_id=run_id, backend=self.backend)

    async def test_async(
        self,
        scenario: str | Path | ScenarioModel,
        agent: AgentAdapter | AgentSpec | Callable[..., Any],
        *,
        run_id: str | None = None,
        seed: int | None = None,
    ) -> RunResult:
        """Asynchronously execute a recovery test using this tester's ledger, backend, and environment.

        Args:
            scenario: Scenario identifier, YAML Path, or ScenarioModel.
            agent: AgentAdapter instance, AgentSpec, or generic Python callable.
            run_id: Optional unique identifier for this test run.
            seed: Deterministic PRNG seed override.

        Returns:
            RunResult with evaluation verdict, assertion outcomes, and cited ledger events.
        """
        if self.backend.mode.value == "isolated":
            raise ConfigurationError(
                "Isolated Docker container execution currently only supports synchronous agents. "
                "To evaluate asynchronous agents ('async def'), use in-process trusted execution ('--isolation trusted') "
                "with 'await test_agent_async(...)'. Containerized asynchronous IPC execution is scheduled for a future release."
            )

        model = resolve_scenario(scenario)
        target_seed = seed if seed is not None else self.default_seed
        model = model.model_copy(update={"seed": target_seed})

        return await self.engine.run_async(model, agent, run_id=run_id, backend=self.backend)

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

    async def __aenter__(self) -> "RecoveryBenchTester":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.reset()
