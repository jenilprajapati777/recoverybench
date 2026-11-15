"""Rich console rendering for RecoveryBench results and comparisons."""

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from recoverybench.engine.context import RunResult
from recoverybench.models import CommitStatus, ExecutionStatus

console = Console()


def render_run_result(result: RunResult, console_out: Console = console) -> None:
    """Print formatted execution result and ledger trace to terminal."""
    verdict_color = "green" if result.evaluation.passed else "red"
    verdict_text = f"[{verdict_color} bold]{result.evaluation.verdict.value}[/{verdict_color} bold]"

    mode_text = (
        "[cyan]ISOLATED (Docker)[/cyan]"
        if getattr(result, "isolation_mode", "trusted") == "isolated"
        else "[yellow]TRUSTED (NOT ISOLATED)[/yellow]"
    )

    header_text = (
        f"[bold]Scenario:[/bold] {result.scenario.name} ({result.scenario.scenario_id})\n"
        f"[bold]Agent:[/bold] {result.agent_id} | [bold]Run ID:[/bold] {result.run_id} | [bold]Execution:[/bold] {mode_text}\n"
        f"[bold]Verdict:[/bold] {verdict_text} | [bold]Duration:[/bold] {result.evaluation.duration_ms:.1f}ms\n"
        f"[bold]Committed Effects:[/bold] {result.evaluation.actual_side_effects} "
        f"(Expected: {result.evaluation.expected_side_effects}, Duplicates: {result.evaluation.duplicate_effect_count})"
    )
    console_out.print(Panel(header_text, title="RecoveryBench Execution Result", border_style=verdict_color))

    # Event Ledger Table
    table = Table(title="Effect Ledger (Append-Only Event Stream)", box=box.ROUNDED)
    table.add_column("Evt #", justify="right", style="cyan", no_wrap=True)
    table.add_column("Attempt", justify="center", style="magenta")
    table.add_column("Tool", style="blue")
    table.add_column("Action", style="blue")
    table.add_column("Exec Status", justify="center")
    table.add_column("Commit Status", justify="center")
    table.add_column("Idempotency Hash", style="dim")
    table.add_column("Injected Fault / Error", style="yellow")

    for e in result.events:
        exec_style = "green" if e.execution_status == ExecutionStatus.SUCCESS else "yellow"
        commit_style = "bold red" if e.commit_status == CommitStatus.COMMITTED else "dim"

        table.add_row(
            str(e.event_id or "-"),
            str(e.attempt_number),
            e.tool_name,
            e.action_type,
            f"[{exec_style}]{e.execution_status.value}[/{exec_style}]",
            f"[{commit_style}]{e.commit_status.value}[/{commit_style}]",
            e.idempotency_key_hash or "none",
            e.error_type or "-",
        )

    console_out.print(table)

    # Assertion Details Table
    assert_table = Table(title="Deterministic Recovery Assertions", box=box.ROUNDED)
    assert_table.add_column("Rule Name", style="bold")
    assert_table.add_column("Expected", justify="center")
    assert_table.add_column("Actual", justify="center")
    assert_table.add_column("Result", justify="center")
    assert_table.add_column("Details", style="white")

    for a in result.evaluation.assertions:
        res_style = "green" if a.passed else "red"
        status_text = f"[{res_style}]{'PASS' if a.passed else 'FAIL'}[/{res_style}]"
        assert_table.add_row(
            a.name,
            str(a.expected),
            str(a.actual),
            status_text,
            a.message,
        )

    console_out.print(assert_table)


def render_comparison(
    naive_result: RunResult,
    safe_result: RunResult,
    console_out: Console = console,
) -> None:
    """Print comparative side-by-side analysis of Naive vs. Safe agent."""
    console_out.print("\n")
    console_out.print(
        Panel.fit(
            "[bold white on blue]  RECOVERYBENCH COMPARATIVE DEMONSTRATION: NAIVE vs. SAFE AGENT  [/]\n"
            "[italic]Scenario: Payment Timeout After Commit (Two Generals' Hazard)[/]",
            border_style="blue",
        )
    )

    console_out.print("\n[bold red]1. Unsafe Agent (Naive Payment Agent)[/bold red]")
    render_run_result(naive_result, console_out)

    console_out.print("\n[bold green]2. Resilient Agent (Idempotent Payment Agent)[/bold green]")
    render_run_result(safe_result, console_out)

    # Comparison Summary Table
    summary_table = Table(title="Comparative Summary & Ground-Truth Invariants", box=box.DOUBLE_EDGE)
    summary_table.add_column("Metric / Invariant", style="bold")
    summary_table.add_column("Naive Agent (Unsafe)", style="red", justify="center")
    summary_table.add_column("Idempotent Agent (Safe)", style="green", justify="center")

    summary_table.add_row(
        "Final Verdict",
        naive_result.evaluation.verdict.value,
        safe_result.evaluation.verdict.value,
    )
    summary_table.add_row(
        "Expected Side Effects",
        str(naive_result.evaluation.expected_side_effects),
        str(safe_result.evaluation.expected_side_effects),
    )
    summary_table.add_row(
        "Actual Committed Effects",
        f"[bold red]{naive_result.evaluation.actual_side_effects}[/bold red]",
        f"[bold green]{safe_result.evaluation.actual_side_effects}[/bold green]",
    )
    summary_table.add_row(
        "Duplicate Side Effects",
        f"[bold red]{naive_result.evaluation.duplicate_effect_count} (DOUBLE CHARGE!)[/bold red]",
        f"[bold green]{safe_result.evaluation.duplicate_effect_count} (None)[/bold green]",
    )
    summary_table.add_row(
        "Idempotency Key Stable?",
        "No (New key / None on retry)",
        "Yes (Preserved identical key)",
    )
    summary_table.add_row(
        "Real-World Impact",
        "[red]Customer charged twice on network drop[/red]",
        "[green]Customer charged once; recovery transparent[/green]",
    )

    console_out.print("\n")
    console_out.print(summary_table)
