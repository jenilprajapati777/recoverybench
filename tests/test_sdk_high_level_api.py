"""Automated tests for high-level RecoveryBench SDK (test_agent, RecoveryBenchTester)."""

import pytest

import recoverybench as rb
from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.errors import ConfigurationError
from recoverybench.sdk.tester import RecoveryBenchTester, resolve_scenario


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
    result = rb.test_agent(
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
