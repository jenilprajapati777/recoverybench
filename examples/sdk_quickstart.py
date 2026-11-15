"""RecoveryBench Example: Quickstart SDK for Test Suites and Pytest.

Demonstrates using RecoveryBench in standard automated test suites:
- Using RecoveryBenchTester as a test fixture.
- Running multiple scenarios with assertions.
- Inspecting ledger event logs.
"""

from rich import print as rprint

from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.sdk.tester import RecoveryBenchTester


def run_test_suite() -> None:
    rprint("[bold blue]Starting RecoveryBench Automated Test Suite...[/bold blue]\n")

    agent = IdempotentPaymentAgent()

    test_scenarios = [
        "payment_timeout_after_commit",
        "payment_timeout_before_commit",
        "payment_rate_limit_429",
        "payment_http_500_transient",
    ]

    with RecoveryBenchTester() as tester:
        for scenario_id in test_scenarios:
            rprint(f"Testing [cyan]{scenario_id}[/cyan]...")
            result = tester.test(scenario_id, agent)

            # Assert execution invariants
            assert result.passed, f"Scenario '{scenario_id}' failed: {result.evaluation.failure_reasons}"
            rprint(f"  [green]✓ PASSED[/green] in {result.summary.duration_ms:.2f}ms (Committed: {result.evaluation.actual_side_effects})")

    rprint("\n[bold green]All recovery scenarios verified successfully![/bold green]")


if __name__ == "__main__":
    run_test_suite()
