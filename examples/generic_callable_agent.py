"""RecoveryBench Example: Testing Generic Python Agent Callables.

This example shows how to use RecoveryBench to test arbitrary Python agent functions
without needing any special framework or base class.
"""

from rich import print as rprint
from rich.panel import Panel

import recoverybench as rb
from recoverybench.agent.base import AgentResult, TaskContext, ToolCaller


# 1. Unsafe Naive Agent (Function)
@rb.agent(id="naive_callable_agent")
def naive_payment_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
    """An agent that blindly retries on timeout with a new idempotency key."""
    for attempt in range(1, 4):
        try:
            # BUG: Each retry generates a different idempotency key
            unstable_key = f"key_attempt_{attempt}"
            response = tool_call(
                "payment_gateway",
                "charge",
                task.task_params,
                unstable_key,
            )
            return AgentResult(
                success=True,
                data=response,
                attempts=attempt,
                status_message=f"Charged on attempt {attempt}",
            )
        except Exception:
            # Network timeout occurred! Naive loop continues to retry...
            continue

    return AgentResult(success=False, error="Exhausted retries")


# 2. Resilient Idempotent Agent (Function)
@rb.agent(id="safe_callable_agent")
def safe_payment_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
    """A resilient agent that derives and maintains a stable idempotency key across retries."""
    # Compute stable key upfront for this transaction
    stable_key = "idem_tx_stable_9941"

    for attempt in range(1, 4):
        try:
            # RESILIENT: Presents the same stable key on every attempt
            response = tool_call(
                "payment_gateway",
                "charge",
                task.task_params,
                stable_key,
            )
            return AgentResult(
                success=True,
                data=response,
                attempts=attempt,
                status_message=f"Payment confirmed on attempt {attempt}",
            )
        except Exception:
            # Network timeout occurred; retry with same stable key
            continue

    return AgentResult(success=False, error="Exhausted retries")


def main() -> None:
    rprint(Panel.fit("[bold blue]RecoveryBench Generic Callable Agent Demo[/bold blue]"))

    # Test Unsafe Naive Agent
    rprint("\n[bold yellow]1. Testing Naive Agent under 'payment_timeout_after_commit'...[/bold yellow]")
    naive_result = rb.test_agent("payment_timeout_after_commit", naive_payment_agent)
    rprint(f"  Result Verdict: [bold red]{naive_result.verdict}[/bold red]")
    rprint(f"  Actual Committed Effects: {naive_result.evaluation.actual_side_effects} (Expected: 1)")
    rprint(f"  Duplicate Side Effects: [bold red]{naive_result.evaluation.duplicate_effect_count}[/bold red]")
    rprint(f"  Pass: {naive_result.passed}")

    # Test Resilient Safe Agent
    rprint("\n[bold green]2. Testing Resilient Agent under 'payment_timeout_after_commit'...[/bold green]")
    safe_result = rb.test_agent("payment_timeout_after_commit", safe_payment_agent)
    rprint(f"  Result Verdict: [bold green]{safe_result.verdict}[/bold green]")
    rprint(f"  Actual Committed Effects: {safe_result.evaluation.actual_side_effects} (Expected: 1)")
    rprint(f"  Duplicate Side Effects: {safe_result.evaluation.duplicate_effect_count}")
    rprint(f"  Pass: [bold green]{safe_result.passed}[/bold green]")

    # Audit Ledger Events
    rprint(f"\n[cyan]Ledger Events for Safe Agent ({len(safe_result.events)} recorded):[/cyan]")
    for evt in safe_result.events:
        rprint(
            f"  - Event #{evt.event_id} (Attempt {evt.attempt_number}): "
            f"status={evt.execution_status.value}, commit={evt.commit_status.value}, "
            f"key_hash={evt.idempotency_key_hash[:12]}..."
        )


if __name__ == "__main__":
    main()
