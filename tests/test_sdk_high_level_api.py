"""Automated tests for high-level RecoveryBench SDK (test_agent, RecoveryBenchTester)."""

from unittest.mock import MagicMock, patch

import pytest

from recoverybench.agent.base import AgentAdapter, AgentResult, TaskContext, ToolCaller
from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.clock import VirtualClock
from recoverybench.errors import ConfigurationError, InfrastructureError
from recoverybench.ledger.memory import InMemoryLedger
from recoverybench.ledger.sqlite import SQLiteLedger
from recoverybench.models import EvaluationVerdict
from recoverybench.sandbox.backend import (
    DockerExecutionBackend,
    InProcessExecutionBackend,
)
from recoverybench.sandbox.docker_client import DockerClient
from recoverybench.sandbox.spec import AgentSpec
from recoverybench.sdk.tester import (
    RecoveryBenchTester,
    resolve_backend,
    resolve_ledger,
    resolve_scenario,
)
from recoverybench.sdk.tester import (
    test_agent as sdk_test_agent,
)


def test_resolve_scenario_builtin_and_error() -> None:
    """Test scenario resolution from registry and non-existent name error."""
    scenario = resolve_scenario("payment_timeout_after_commit")
    assert scenario.scenario_id == "payment_timeout_after_commit"

    with pytest.raises(ConfigurationError) as exc_info:
        resolve_scenario("non_existent_scenario_xyz")
    assert "not found in built-ins" in str(exc_info.value)


def test_test_agent_function_with_seed_override() -> None:
    """Test test_agent function with deterministic seed override."""
    agent = IdempotentPaymentAgent()
    result = sdk_test_agent(
        "payment_timeout_after_commit",
        agent,
        seed=999,
        run_id="sdk_test_run_seed_999",
    )
    assert result.run_id == "sdk_test_run_seed_999"
    assert result.passed is True
    assert result.verdict == "PASS"


def test_recoverybench_tester_context_manager() -> None:
    """Test RecoveryBenchTester harness and ledger event retrieval."""
    with RecoveryBenchTester() as tester:
        safe_agent = IdempotentPaymentAgent()
        result_safe = tester.test("payment_timeout_after_commit", safe_agent, run_id="tester_safe_01")
        assert result_safe.passed is True

        events = tester.get_ledger_events("tester_safe_01")
        assert len(events) >= 2

        # Re-run with naive agent
        naive_agent = NaivePaymentAgent()
        result_naive = tester.test("payment_timeout_after_commit", naive_agent, run_id="tester_naive_01")
        assert result_naive.passed is False

        # Verify assert_passed helper raises on failure
        with pytest.raises(AssertionError) as exc_info:
            result_naive.assert_passed()
        assert "failed" in str(exc_info.value).lower()


# ==============================================================================
# Phase 3B.1 Regression Tests: SDK Contract Harmonization
# ==============================================================================


def test_test_agent_trusted_default() -> None:
    """1. test_agent() defaults to trusted in-process execution."""
    agent = IdempotentPaymentAgent()
    result = sdk_test_agent("payment_timeout_after_commit", agent)
    assert result.passed is True
    assert result.isolation_mode == "trusted"


def test_test_agent_isolated_selection() -> None:
    """2. test_agent() isolated selection instantiates DockerExecutionBackend."""
    agent = IdempotentPaymentAgent()
    mock_docker_backend = MagicMock(spec=DockerExecutionBackend)
    mock_docker_backend.mode.value = "isolated"
    mock_docker_backend.is_isolated = True

    dummy_agent_res = AgentResult(success=True, attempts=2, status_message="Mock isolated success")
    mock_docker_backend.run.return_value = dummy_agent_res

    # When isolation="isolated", resolve_backend produces DockerExecutionBackend
    backend = resolve_backend(isolation="isolated")
    assert isinstance(backend, DockerExecutionBackend)

    # When passed to test_agent via backend precedence or isolation
    with patch("recoverybench.sdk.tester.DockerExecutionBackend", return_value=mock_docker_backend):
        res = sdk_test_agent("payment_timeout_after_commit", agent, isolation="isolated")
        assert mock_docker_backend.run.called
        assert res.isolation_mode == "isolated"


def test_invalid_isolation_raises_configuration_error() -> None:
    """3. Invalid isolation mode raises ConfigurationError and never falls back silently."""
    agent = IdempotentPaymentAgent()

    invalid_modes = ["unsupported", "remote", "container", "vm", "", "   "]
    for bad_mode in invalid_modes:
        with pytest.raises(ConfigurationError) as exc_info:
            sdk_test_agent("payment_timeout_after_commit", agent, isolation=bad_mode)
        assert f"Invalid isolation mode '{bad_mode}'" in str(exc_info.value)

        with pytest.raises(ConfigurationError) as exc_tester:
            RecoveryBenchTester(isolation=bad_mode)
        assert f"Invalid isolation mode '{bad_mode}'" in str(exc_tester.value)


def test_documented_isolation_aliases_mapping() -> None:
    """4. Every documented isolation alias maps to the intended backend with case/whitespace normalization."""
    # Isolated aliases
    for alias in ["isolated", "docker", "sandbox", "  ISOLATED  ", "Docker", " SandBox "]:
        b = resolve_backend(isolation=alias)
        assert isinstance(b, DockerExecutionBackend), f"Expected DockerExecutionBackend for alias '{alias}'"
        assert b.mode.value == "isolated"
        assert b.is_isolated is True

    # Trusted aliases
    for alias in ["trusted", "in-process", "inprocess", "local", " Trusted ", "IN-PROCESS", " Local "]:
        b = resolve_backend(isolation=alias)
        assert isinstance(b, InProcessExecutionBackend), f"Expected InProcessExecutionBackend for alias '{alias}'"
        assert b.mode.value == "trusted"
        assert b.is_isolated is False


def test_recoverybench_tester_trusted_configuration() -> None:
    """5. RecoveryBenchTester defaults to and supports trusted in-process configuration."""
    # Default
    tester_default = RecoveryBenchTester()
    assert isinstance(tester_default.backend, InProcessExecutionBackend)
    assert tester_default.isolation == "trusted"

    # Explicit trusted alias
    tester_explicit = RecoveryBenchTester(isolation="local")
    assert isinstance(tester_explicit.backend, InProcessExecutionBackend)

    agent = IdempotentPaymentAgent()
    res = tester_default.test("payment_timeout_after_commit", agent)
    assert res.passed is True
    assert res.isolation_mode == "trusted"


def test_recoverybench_tester_isolated_configuration() -> None:
    """6. RecoveryBenchTester supports explicit isolated configuration."""
    tester = RecoveryBenchTester(isolation="isolated")
    assert isinstance(tester.backend, DockerExecutionBackend)
    assert tester.backend.is_isolated is True

    tester_sandbox = RecoveryBenchTester(isolation="sandbox")
    assert isinstance(tester_sandbox.backend, DockerExecutionBackend)


def test_recoverybench_tester_accepts_agent_spec() -> None:
    """7. RecoveryBenchTester accepts AgentSpec directly without wrapping."""
    spec = AgentSpec.from_callable(IdempotentPaymentAgent)

    with RecoveryBenchTester(isolation="trusted") as tester:
        # InProcessExecutionBackend supports AgentSpec by dynamically importing entrypoint
        result = tester.test("payment_timeout_after_commit", spec, run_id="spec_test_run_01")
        assert result.passed is True
        assert result.isolation_mode == "trusted"
        assert result.evaluation.actual_side_effects == 1
        assert result.evaluation.duplicate_effect_count == 0


def test_recoverybench_tester_accepts_ordinary_callables() -> None:
    """8. RecoveryBenchTester accepts generic Python callables."""
    def custom_callable_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        stable_key = "callable_stable_key_001"
        for attempt in range(1, 4):
            try:
                res = tool_call("payment_gateway", "charge", task.task_params, stable_key)
                return AgentResult(success=True, data=res, attempts=attempt, status_message="Charged")
            except Exception:
                continue
        return AgentResult(success=False, error="Failed retries")

    with RecoveryBenchTester() as tester:
        res = tester.test("payment_timeout_after_commit", custom_callable_agent)
        assert res.passed is True
        assert res.evaluation.actual_side_effects == 1
        assert res.evaluation.duplicate_effect_count == 0


def test_recoverybench_tester_accepts_agent_adapter_implementations() -> None:
    """9. RecoveryBenchTester accepts AgentAdapter implementations."""
    agent = IdempotentPaymentAgent()
    assert isinstance(agent, AgentAdapter)

    with RecoveryBenchTester() as tester:
        res = tester.test("payment_timeout_after_commit", agent)
        assert res.passed is True
        assert res.evaluation.actual_side_effects == 1


def test_invalid_ledger_type_raises_configuration_error() -> None:
    """10. Invalid ledger type raises ConfigurationError in test_agent and RecoveryBenchTester."""
    agent = IdempotentPaymentAgent()

    for bad_ledger in ["postgres", "redis", "dynamo", "invalid", "", "   "]:
        with pytest.raises(ConfigurationError) as exc_info:
            sdk_test_agent("payment_timeout_after_commit", agent, ledger_type=bad_ledger)
        assert f"Invalid ledger_type '{bad_ledger}'" in str(exc_info.value)

        with pytest.raises(ConfigurationError) as exc_tester:
            RecoveryBenchTester(ledger_type=bad_ledger)
        assert f"Invalid ledger_type '{bad_ledger}'" in str(exc_tester.value)

    # Valid ledger types with whitespace and case
    assert isinstance(resolve_ledger("memory"), InMemoryLedger)
    assert isinstance(resolve_ledger("  MEMORY  "), InMemoryLedger)
    assert isinstance(resolve_ledger("sqlite"), SQLiteLedger)
    assert isinstance(resolve_ledger(" SQLite "), SQLiteLedger)


def test_existing_sdk_behavior_and_backend_precedence() -> None:
    """11. Existing SDK options remain compatible and backend precedence is strictly enforced."""
    agent = IdempotentPaymentAgent()

    # Precedence: Explicit backend overrides isolation string
    custom_backend = InProcessExecutionBackend()
    res = sdk_test_agent(
        "payment_timeout_after_commit",
        agent,
        backend=custom_backend,
        isolation="isolated",  # Valid isolation string, but backend takes precedence
    )
    assert res.passed is True
    assert res.isolation_mode == "trusted"

    # Precedence in RecoveryBenchTester
    tester = RecoveryBenchTester(backend=custom_backend, isolation="sandbox")
    assert tester.backend is custom_backend

    # If isolation is invalid while backend is supplied, still raise ConfigurationError
    with pytest.raises(ConfigurationError) as exc_info:
        RecoveryBenchTester(backend=custom_backend, isolation="invalid_isolation")
    assert "Invalid isolation mode 'invalid_isolation'" in str(exc_info.value)

    # Invalid backend type raises ConfigurationError
    with pytest.raises(ConfigurationError) as exc_backend:
        RecoveryBenchTester(backend="not_a_backend")  # type: ignore[arg-type]
    assert "Invalid backend parameter" in str(exc_backend.value)


def test_docker_unavailable_isolated_execution_fails_closed() -> None:
    """12. Docker-unavailable isolated execution fails closed."""
    agent = IdempotentPaymentAgent()
    spec = AgentSpec.from_callable(IdempotentPaymentAgent)

    with patch.object(DockerClient, "is_docker_available", return_value=(False, "Docker daemon not running")):
        # 1. Direct Docker backend execution fails closed with InfrastructureError
        backend = DockerExecutionBackend()
        task_ctx = TaskContext(scenario_id="test", clock=VirtualClock())
        mock_tool = MagicMock()
        with pytest.raises(InfrastructureError) as exc_backend:
            backend.run(spec, task_ctx, mock_tool)
        assert "Docker sandbox execution requested, but Docker is unavailable" in str(exc_backend.value)

        # 2. In test_agent with isolation="isolated", execution engine reports INFRASTRUCTURE_ERROR
        res = sdk_test_agent("payment_timeout_after_commit", agent, isolation="isolated")
        assert res.passed is False
        assert res.verdict == "INFRASTRUCTURE_ERROR"
        assert res.evaluation.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR
        assert any("InfrastructureError" in reason for reason in res.evaluation.failure_reasons)
        with pytest.raises(AssertionError):
            res.assert_passed()

        # 3. In RecoveryBenchTester with isolation="isolated", test fails closed
        tester = RecoveryBenchTester(isolation="isolated")
        res_tester = tester.test("payment_timeout_after_commit", agent)
        assert res_tester.passed is False
        assert res_tester.verdict == "INFRASTRUCTURE_ERROR"
        assert any("InfrastructureError" in reason for reason in res_tester.evaluation.failure_reasons)
        with pytest.raises(AssertionError):
            res_tester.assert_passed()
