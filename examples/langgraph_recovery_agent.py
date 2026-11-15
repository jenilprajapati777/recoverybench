"""RecoveryBench Example: Testing LangGraph StateGraph Agents.

Demonstrates how to evaluate a real LangGraph agent against distributed failure modes:
1. Naive LangGraph agent retrying tool calls without preserving idempotency keys (Causes double charges).
2. Resilient LangGraph agent storing the idempotency key in its graph state and safely recovering.
"""

from rich import print as rprint
from rich.panel import Panel

import recoverybench as rb
from recoverybench.adapters.langgraph import (
    IdempotentLangGraphAgent,
    NaiveLangGraphAgent,
)


def main() -> None:
    rprint(Panel.fit("[bold blue]RecoveryBench LangGraph Integration Demo[/bold blue]"))

    scenario_name = "payment_timeout_after_commit"

    # 1. Evaluate Naive LangGraph Workflow
    rprint(f"\n[yellow]Executing Naive LangGraph Agent on '{scenario_name}'...[/yellow]")
    naive_agent = NaiveLangGraphAgent()
    naive_result = rb.test_agent(scenario_name, naive_agent)

    rprint(f"  Agent ID: [bold]{naive_result.agent_id}[/bold]")
    rprint(f"  Verdict: [bold red]{naive_result.verdict}[/bold red]")
    rprint(f"  Committed Effects: [bold red]{naive_result.evaluation.actual_side_effects}[/bold red] (Expected: 1)")
    rprint(f"  Duplicate Effects: [bold red]{naive_result.evaluation.duplicate_effect_count}[/bold red]")
    rprint(f"  Passed: {naive_result.passed}")

    # 2. Evaluate Resilient LangGraph Workflow
    rprint(f"\n[green]Executing Resilient Idempotent LangGraph Agent on '{scenario_name}'...[/green]")
    safe_agent = IdempotentLangGraphAgent()
    safe_result = rb.test_agent(scenario_name, safe_agent)

    rprint(f"  Agent ID: [bold]{safe_result.agent_id}[/bold]")
    rprint(f"  Verdict: [bold green]{safe_result.verdict}[/bold green]")
    rprint(f"  Committed Effects: [bold green]{safe_result.evaluation.actual_side_effects}[/bold green] (Expected: 1)")
    rprint(f"  Duplicate Effects: {safe_result.evaluation.duplicate_effect_count}")
    rprint(f"  Passed: [bold green]{safe_result.passed}[/bold green]")

    # 3. Assertions Breakdown
    rprint("\n[bold cyan]Evaluation Invariants Breakdown (Resilient Agent):[/bold cyan]")
    for assertion in safe_result.evaluation.assertions:
        status_color = "green" if assertion.passed else "red"
        rprint(f"  - [{status_color}]{assertion.name}[/{status_color}]: {assertion.message}")
        for ev in assertion.evidence:
            rprint(f"      [dim]* {ev}[/dim]")


if __name__ == "__main__":
    main()
