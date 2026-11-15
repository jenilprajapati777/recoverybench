"""Command-Line Interface for RecoveryBench."""

import json
import sys
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from recoverybench.agent.base import AgentAdapter, AgentResult
from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.config import get_settings
from recoverybench.engine.context import RunResult
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.errors import ConfigurationError, ScenarioValidationError
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.ledger.sqlite import SQLiteLedger
from recoverybench.models import EvaluationVerdict
from recoverybench.replay.engine import ReplayEngine
from recoverybench.reporting.console import render_comparison, render_run_result
from recoverybench.reporting.github_summary import append_github_step_summary
from recoverybench.reporting.html_reporter import HTMLReporter
from recoverybench.reporting.json_reporter import JSONReporter
from recoverybench.reporting.junit_reporter import JUnitReporter
from recoverybench.sample_project.resources import (
    SAMPLE_CONFIG_YAML,
    SAMPLE_TIMEOUT_AFTER_COMMIT_YAML,
    SAMPLE_TIMEOUT_BEFORE_COMMIT_YAML,
)
from recoverybench.sandbox.backend import (
    DockerExecutionBackend,
    ExecutionBackend,
    InProcessExecutionBackend,
)
from recoverybench.sandbox.docker_client import DockerClient
from recoverybench.scenarios.registry import ScenarioRegistry
from recoverybench.scenarios.validator import ScenarioValidator
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService

app = typer.Typer(
    name="recoverybench",
    help="RecoveryBench: Local-first AI Agent Recovery Testing Platform.",
    add_completion=False,
)
console = Console()
err_console = Console(stderr=True)


def get_default_engine(db_path: Path | None = None) -> ExecutionEngine:
    """Instantiate execution engine wired with SQLite ledger and default mock tools."""
    settings = get_settings()
    target_db = db_path or settings.db_path
    ledger = SQLiteLedger(target_db)
    tool_registry = ToolRegistry()
    tool_registry.register(MockPaymentService())
    return ExecutionEngine(ledger=ledger, tool_registry=tool_registry)


@app.command()
def doctor(
    require_isolation: bool = typer.Option(
        False,
        "--require-isolation",
        help="Enforce that Docker isolation is ready (fails with exit code 3 if unavailable)",
    ),
) -> None:
    """Diagnose the local environment, Python runtime, and permissions."""
    table = Table(title="RecoveryBench Doctor - Environment Diagnostics", box=box.ROUNDED)
    table.add_column("Component", style="bold")
    table.add_column("Status", justify="center")
    table.add_column("Details")

    # 1. Python Version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 11)
    table.add_row(
        "Python Runtime",
        "[green]OK[/green]" if py_ok else "[red]FAIL[/red]",
        f"Version {py_ver} (3.11+ required)",
    )

    # 2. SQLite
    try:
        _ = SQLiteLedger(":memory:")
        sqlite_ok = True
        sqlite_details = "SQLite in-memory and foreign key validation active"
    except Exception as e:
        sqlite_ok = False
        sqlite_details = str(e)
    table.add_row(
        "SQLite Engine",
        "[green]OK[/green]" if sqlite_ok else "[red]FAIL[/red]",
        sqlite_details,
    )

    # 3. Write Permissions
    settings = get_settings()
    try:
        settings.ensure_directories()
        test_file = settings.data_dir / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        fs_ok = True
        fs_details = f"Write access verified at {settings.data_dir.resolve()}"
    except Exception as e:
        fs_ok = False
        fs_details = f"Write failed: {e}"
    table.add_row(
        "Filesystem Storage",
        "[green]OK[/green]" if fs_ok else "[red]FAIL[/red]",
        fs_details,
    )

    # 4. Scenarios
    registry = ScenarioRegistry()
    all_scenarios = registry.list_all()
    table.add_row(
        "Scenario Registry",
        "[green]OK[/green]",
        f"{len(all_scenarios)} scenario(s) loaded ({len(all_scenarios)} built-in)",
    )

    # 5. Docker CLI Executable
    docker_client = DockerClient(settings.sandbox)
    docker_bin = docker_client.find_docker_executable()
    docker_exe_ok = docker_bin is not None
    table.add_row(
        "Docker Executable",
        "[green]OK[/green]" if docker_exe_ok else "[yellow]NOT FOUND[/yellow]",
        f"Executable at {docker_bin}" if docker_exe_ok else "Docker binary not found on system PATH",
    )

    # 6. Docker Daemon Reachability & Linux Container Capability
    docker_daemon_ok = False
    if docker_exe_ok:
        docker_daemon_ok, daemon_msg = docker_client.is_docker_available()
    else:
        daemon_msg = "Skipped (executable missing)"
    table.add_row(
        "Docker Daemon",
        "[green]OK[/green]" if docker_daemon_ok else "[yellow]UNAVAILABLE[/yellow]",
        daemon_msg,
    )

    # 7. Runtime Image
    image_name = settings.sandbox.runtime_image
    image_ok = False
    if docker_daemon_ok:
        image_ok = docker_client.check_image_exists(image_name)
        img_msg = f"Image '{image_name}' cached locally" if image_ok else f"Image '{image_name}' not cached"
    else:
        img_msg = f"Cannot inspect image '{image_name}' (daemon unavailable)"
    table.add_row(
        "Runtime Image",
        "[green]OK[/green]" if image_ok else "[yellow]NOT CACHED[/yellow]",
        img_msg,
    )

    # 8. Sandbox Resource Limits
    cfg = settings.sandbox
    table.add_row(
        "Sandbox Resource Limits",
        "[green]OK[/green]",
        f"Memory: {cfg.memory_limit} | CPU: {cfg.cpu_limit} | PIDs: {cfg.pids_limit} | Timeout: {cfg.timeout_seconds}s",
    )

    # 9. Isolation Readiness
    isolation_ready = docker_exe_ok and docker_daemon_ok and image_ok
    table.add_row(
        "Isolation Readiness",
        "[green]READY[/green]" if isolation_ready else "[yellow]NOT READY[/yellow]",
        "Containerized network isolation active" if isolation_ready else "Trusted in-process execution available",
    )

    console.print(table)
    if not (py_ok and sqlite_ok and fs_ok):
        raise typer.Exit(code=3)
    if require_isolation and not isolation_ready:
        err_console.print(
            "[red]Doctor failure: Required container isolation is not ready. "
            "Ensure Docker is running and runtime image is cached.[/red]"
        )
        raise typer.Exit(code=3)

    console.print("[green]System diagnostics complete.[/green]")
    raise typer.Exit(code=0)


@app.command()
def init(
    target_dir: Path = typer.Option(Path("."), "--dir", "-d", help="Target directory for project files"),
) -> None:
    """Scaffold a new RecoveryBench configuration and sample scenarios."""
    scenarios_dir = target_dir / "scenarios"
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    config_path = target_dir / "recoverybench.yaml"
    if not config_path.exists():
        config_path.write_text(SAMPLE_CONFIG_YAML, encoding="utf-8")
        console.print(f"[green]Created config file:[/green] {config_path}")

    s1_path = scenarios_dir / "payment_timeout_after_commit.yaml"
    if not s1_path.exists():
        s1_path.write_text(SAMPLE_TIMEOUT_AFTER_COMMIT_YAML, encoding="utf-8")
        console.print(f"[green]Created sample scenario:[/green] {s1_path}")

    s2_path = scenarios_dir / "payment_timeout_before_commit.yaml"
    if not s2_path.exists():
        s2_path.write_text(SAMPLE_TIMEOUT_BEFORE_COMMIT_YAML, encoding="utf-8")
        console.print(f"[green]Created sample scenario:[/green] {s2_path}")

    console.print(
        Panel(
            "[bold green]RecoveryBench Project Initialized Successfully![/bold green]\n\n"
            "Try running the comparative demo:\n"
            "  [bold cyan]recoverybench demo[/bold cyan]\n\n"
            "Or run a specific scenario:\n"
            "  [bold cyan]recoverybench run payment_timeout_after_commit[/bold cyan]",
            title="Project Initialized",
            border_style="green",
        )
    )
    raise typer.Exit(code=0)


@app.command(name="list-scenarios")
def list_scenarios(
    json_output: bool = typer.Option(False, "--json", help="Format output as JSON"),
) -> None:
    """List all available built-in and user-defined recovery scenarios."""
    registry = ScenarioRegistry()
    scenarios = registry.list_all()

    if json_output:
        import json
        data = [
            {
                "id": s.scenario_id,
                "name": s.name,
                "description": s.description,
                "tool": s.target_tool,
                "action": s.target_action,
                "faults": [f.type.value for f in s.faults],
            }
            for s in scenarios
        ]
        console.print(json.dumps(data, indent=2))
        raise typer.Exit(code=0)

    table = Table(title="Available RecoveryBench Scenarios", box=box.ROUNDED)
    table.add_column("Scenario ID", style="cyan bold")
    table.add_column("Title", style="white")
    table.add_column("Target Tool / Action", style="blue")
    table.add_column("Active Faults", style="yellow")

    for s in scenarios:
        fault_summary = ", ".join(f.type.value for f in s.faults) or "None"
        target = f"{s.target_tool}:{s.target_action or '*'}"
        table.add_row(s.scenario_id, s.name, target, fault_summary)

    console.print(table)
    raise typer.Exit(code=0)


@app.command()
def validate(
    scenario_path: Path = typer.Argument(Path("scenarios"), help="Path to scenario YAML file or directory to validate"),
) -> None:
    """Validate a scenario YAML file or an entire directory of scenarios against schema rules."""
    if scenario_path.is_dir():
        try:
            valid_scenarios, errors = ScenarioValidator.validate_directory(scenario_path)
            if errors:
                err_console.print(f"[red bold]Scenario Validation Failed ({len(errors)} error(s)):[/red bold]")
                for err in errors:
                    err_console.print(f"  [red]•[/red] {err}")
                raise typer.Exit(code=2)

            table = Table(title=f"Validated Scenarios in '{scenario_path}'", box=box.ROUNDED)
            table.add_column("Scenario ID", style="cyan bold")
            table.add_column("Title", style="white")
            table.add_column("Faults", style="yellow")
            for s in valid_scenarios:
                faults_str = ", ".join(f.type.value for f in s.faults)
                table.add_row(s.scenario_id, s.name, faults_str)
            console.print(table)
            console.print(f"[green bold]✓ All {len(valid_scenarios)} scenario files in '{scenario_path}' are valid![/green bold]")
            return
        except ConfigurationError as ce:
            err_console.print(f"[red bold]Configuration Error:[/red bold] {ce.message}")
            raise typer.Exit(code=2) from None
        except typer.Exit:
            raise
        except Exception as exc:
            err_console.print(f"[red bold]Error validating directory:[/red bold] {exc}")
            raise typer.Exit(code=2) from None

    try:
        scenario = ScenarioValidator.validate_yaml_file(scenario_path)
    except ScenarioValidationError as sve:
        err_console.print(f"[red bold]Scenario Validation Error:[/red bold] {sve.message}")
        for err in sve.errors:
            err_console.print(f"  - {err}")
        raise typer.Exit(code=2) from None
    except ConfigurationError as ce:
        err_console.print(f"[red bold]Configuration Error:[/red bold] {ce.message}")
        raise typer.Exit(code=2) from None
    except Exception as exc:
        err_console.print(f"[red bold]Error reading scenario:[/red bold] {exc}")
        raise typer.Exit(code=2) from None

    console.print(f"[green]Valid scenario:[/green] '{scenario.name}' ({scenario.scenario_id})")


@app.command()
def demo(
    isolation: str = typer.Option(
        "trusted",
        "--isolation",
        "-i",
        help="Execution isolation mode: 'trusted' (default for in-process demo) or 'isolated' (Docker container)",
    ),
) -> None:
    """Run comparative demonstration: Naive Payment Agent vs. Safe Idempotent Agent."""
    registry = ScenarioRegistry()
    scenario = registry.get("payment_timeout_after_commit")
    if not scenario:
        err_console.print("[red]Built-in scenario 'payment_timeout_after_commit' not found.[/red]")
        raise typer.Exit(code=2)

    clean_iso = isolation.strip().lower()
    backend: ExecutionBackend
    if clean_iso in ("trusted", "in-process", "inprocess", "local"):
        backend = InProcessExecutionBackend()
        console.print(
            Panel(
                "[yellow bold]⚠ TRUSTED EXECUTION MODE (DEMO DEFAULT)[/yellow bold]\n"
                "[dim]Running in-process without container or network isolation.\n"
                "Suitable for quick exploration and demo scenarios.\n"
                "For untrusted agents or security verification, use [cyan]--isolation isolated[/cyan].[/dim]",
                border_style="yellow",
            )
        )
    elif clean_iso in ("isolated", "docker", "sandbox"):
        backend = DockerExecutionBackend()
    else:
        err_console.print(f"[red]Invalid isolation mode '{isolation}'. Supported: 'trusted', 'isolated'[/red]")
        raise typer.Exit(code=2)

    engine = get_default_engine()

    # 1. Run Naive Agent (will double charge and fail recovery invariants)
    naive_agent = NaivePaymentAgent(max_retries=3)
    naive_result = engine.run(scenario, naive_agent, run_id="demo_naive_agent_run", backend=backend)

    # 2. Run Safe Agent (will preserve stable key and pass recovery invariants)
    safe_agent = IdempotentPaymentAgent(max_retries=3)
    safe_result = engine.run(scenario, safe_agent, run_id="demo_safe_agent_run", backend=backend)

    # Check for infrastructure/configuration failures during execution
    for res in (naive_result, safe_result):
        if res.evaluation.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR:
            err_msg = ""
            if res.agent_raw_result and isinstance(res.agent_raw_result, dict):
                err_msg = str(res.agent_raw_result.get("error") or "")
            err_console.print(
                f"[red bold]Demo execution failed ({err_msg or 'infrastructure error'}):[/red bold]"
            )
            for reason in res.evaluation.failure_reasons:
                err_console.print(f"  [red]•[/red] {reason}")
            if any(a.name == "configuration_integrity" for a in res.evaluation.assertions):
                raise typer.Exit(code=2)
            raise typer.Exit(code=3)

    # Render side-by-side terminal comparison
    render_comparison(naive_result, safe_result, console)

    # Automatically save demo reports
    settings = get_settings()
    settings.ensure_directories()
    naive_report_path = settings.reports_dir / "demo_naive_report.json"
    safe_report_path = settings.reports_dir / "demo_safe_report.json"
    safe_html_path = settings.reports_dir / "demo_safe_report.html"
    safe_xml_path = settings.reports_dir / "demo_safe_report.xml"
    naive_xml_path = settings.reports_dir / "demo_naive_report.xml"

    JSONReporter.save_report(naive_result, naive_report_path)
    JSONReporter.save_report(safe_result, safe_report_path)
    HTMLReporter.save_report(safe_result, safe_html_path)
    JUnitReporter.save_report(safe_result, safe_xml_path)
    JUnitReporter.save_report(naive_result, naive_xml_path)

    console.print("\n[dim]Demo reports saved to:[/dim]")
    console.print(f"  - [cyan]{naive_report_path}[/cyan]")
    console.print(f"  - [cyan]{safe_report_path}[/cyan]")
    console.print(f"  - [cyan]{safe_html_path}[/cyan]")
    console.print(f"  - [cyan]{safe_xml_path}[/cyan]\n")

    raise typer.Exit(code=0)


@app.command()
def run(
    scenario_id: str = typer.Argument(..., help="Scenario ID or path to scenario YAML"),
    agent_name: str = typer.Option("idempotent", "--agent", "-a", help="Agent to run: 'idempotent', 'naive', 'langgraph-idempotent', 'langgraph-naive'"),
    isolation: str = typer.Option("isolated", "--isolation", "-i", help="Execution isolation mode: 'isolated' (Docker container, recommended) or 'trusted' (in-process)"),
    json_output: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
    html_output: Path | None = typer.Option(None, "--html", help="Path to save HTML report"),
    junit_output: Path | None = typer.Option(None, "--junit", help="Path to save JUnit XML report"),
    output_dir: Path | None = typer.Option(None, "--output-dir", "-o", help="Directory to save all report artifacts (JSON, HTML, XML)"),
) -> None:
    """Execute a recovery test scenario against an agent."""
    # 1. Resolve Scenario
    scenario = None
    path_candidate = Path(scenario_id)
    if path_candidate.exists():
        try:
            scenario = ScenarioValidator.validate_yaml_file(path_candidate)
        except ScenarioValidationError as sve:
            err_console.print(f"[red]Validation failed:[/red] {sve.message}")
            raise typer.Exit(code=2) from None
    else:
        registry = ScenarioRegistry()
        scenario = registry.get(scenario_id)

    if not scenario:
        err_console.print(f"[red]Scenario '{scenario_id}' not found.[/red]")
        raise typer.Exit(code=2)

    # 2. Resolve Agent
    agent: AgentAdapter
    if agent_name in ("naive", "naive_payment_agent"):
        agent = NaivePaymentAgent()
    elif agent_name in ("idempotent", "safe", "idempotent_payment_agent"):
        agent = IdempotentPaymentAgent()
    elif agent_name in ("langgraph-idempotent", "langgraph_idempotent", "langgraph-safe"):
        from recoverybench.adapters.langgraph import IdempotentLangGraphAgent
        agent = IdempotentLangGraphAgent()
    elif agent_name in ("langgraph-naive", "langgraph_naive"):
        from recoverybench.adapters.langgraph import NaiveLangGraphAgent
        agent = NaiveLangGraphAgent()
    else:
        err_console.print(
            f"[red]Unknown agent '{agent_name}'. Supported built-in agents: "
            f"'idempotent', 'naive', 'langgraph-idempotent', 'langgraph-naive'[/red]"
        )
        raise typer.Exit(code=2)

    # 3. Resolve Execution Backend
    clean_iso = isolation.strip().lower()
    backend: ExecutionBackend
    if clean_iso in ("isolated", "docker", "sandbox"):
        backend = DockerExecutionBackend()
    elif clean_iso in ("trusted", "in-process", "inprocess", "local"):
        backend = InProcessExecutionBackend()
    else:
        err_console.print(f"[red]Invalid isolation mode '{isolation}'. Supported: 'isolated', 'trusted'[/red]")
        raise typer.Exit(code=2)

    # 4. Execute
    engine = get_default_engine()
    result = engine.run(scenario, agent, backend=backend)

    # 4. Reporting & Output
    if json_output:
        console.print(JSONReporter.to_json_string(result))
    else:
        render_run_result(result, console)

    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / f"{scenario.scenario_id}_report.json"
        html_path = output_dir / f"{scenario.scenario_id}_report.html"
        junit_path = output_dir / f"{scenario.scenario_id}_report.xml"
        JSONReporter.save_report(result, json_path)
        HTMLReporter.save_report(result, html_path)
        JUnitReporter.save_report(result, junit_path)
        console.print(f"[green]Reports saved to {output_dir}:[/green]")
        console.print(f"  - [cyan]{json_path.name}[/cyan]")
        console.print(f"  - [cyan]{html_path.name}[/cyan]")
        console.print(f"  - [cyan]{junit_path.name}[/cyan]")

    if html_output:
        HTMLReporter.save_report(result, html_output)
        console.print(f"[green]HTML report saved to {html_output}[/green]")

    if junit_output:
        JUnitReporter.save_report(result, junit_output)
        console.print(f"[green]JUnit XML report saved to {junit_output}[/green]")

    # Append to GitHub Step Summary if running in CI
    append_github_step_summary([result])

    # Exit Codes: 0 if passed, 1 if failed, 2 if configuration error, 3 if infrastructure error
    if result.evaluation.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR:
        if any(a.name == "configuration_integrity" for a in result.evaluation.assertions):
            raise typer.Exit(code=2)
        raise typer.Exit(code=3)
    elif result.evaluation.passed:
        raise typer.Exit(code=0)
    else:
        raise typer.Exit(code=1)


@app.command()
def suite(
    scenarios_dir: Path = typer.Argument(Path("scenarios"), help="Directory containing scenario YAML files to evaluate"),
    agent_name: str = typer.Option("idempotent", "--agent", "-a", help="Agent to evaluate: 'idempotent', 'naive', 'langgraph-idempotent', 'langgraph-naive'"),
    isolation: str = typer.Option("isolated", "--isolation", "-i", help="Execution isolation mode: 'isolated' (Docker container) or 'trusted' (in-process)"),
    output_dir: Path = typer.Option(Path("reports"), "--output-dir", "-o", help="Directory to save test artifacts (JSON, HTML, JUnit XML)"),
    junit: bool = typer.Option(True, "--junit/--no-junit", help="Generate JUnit XML reports for CI test parsers"),
    json_summary: bool = typer.Option(False, "--json", help="Output summary matrix as JSON"),
) -> None:
    """Execute an entire suite of recovery scenarios in CI/CD or local test runs."""
    # 1. Validate and load all scenarios
    try:
        valid_scenarios, validation_errors = ScenarioValidator.validate_directory(scenarios_dir)
        if validation_errors:
            err_console.print(f"[red bold]Scenario Suite Validation Failed ({len(validation_errors)} error(s)):[/red bold]")
            for err in validation_errors:
                err_console.print(f"  [red]•[/red] {err}")
            raise typer.Exit(code=2)
    except ConfigurationError as ce:
        err_console.print(f"[red bold]Configuration Error:[/red bold] {ce.message}")
        raise typer.Exit(code=2) from None

    # 2. Resolve Agent
    agent: AgentAdapter
    if agent_name in ("naive", "naive_payment_agent"):
        agent = NaivePaymentAgent()
    elif agent_name in ("idempotent", "safe", "idempotent_payment_agent"):
        agent = IdempotentPaymentAgent()
    elif agent_name in ("langgraph-idempotent", "langgraph_idempotent", "langgraph-safe"):
        from recoverybench.adapters.langgraph import IdempotentLangGraphAgent
        agent = IdempotentLangGraphAgent()
    elif agent_name in ("langgraph-naive", "langgraph_naive"):
        from recoverybench.adapters.langgraph import NaiveLangGraphAgent
        agent = NaiveLangGraphAgent()
    else:
        err_console.print(
            f"[red]Unknown agent '{agent_name}'. Supported built-in agents: "
            f"'idempotent', 'naive', 'langgraph-idempotent', 'langgraph-naive'[/red]"
        )
        raise typer.Exit(code=2)

    # 3. Resolve Execution Backend
    clean_iso = isolation.strip().lower()
    backend: ExecutionBackend
    if clean_iso in ("isolated", "docker", "sandbox"):
        backend = DockerExecutionBackend()
    elif clean_iso in ("trusted", "in-process", "inprocess", "local"):
        backend = InProcessExecutionBackend()
    else:
        err_console.print(f"[red]Invalid isolation mode '{isolation}'. Supported: 'isolated', 'trusted'[/red]")
        raise typer.Exit(code=2)

    # 4. Execute Suite
    output_dir.mkdir(parents=True, exist_ok=True)
    engine = get_default_engine()
    results: list[RunResult] = []

    for scenario in valid_scenarios:
        result = engine.run(scenario, agent, backend=backend)
        results.append(result)

        # Save individual reports
        JSONReporter.save_report(result, output_dir / f"{scenario.scenario_id}_report.json")
        HTMLReporter.save_report(result, output_dir / f"{scenario.scenario_id}_report.html")
        if junit:
            JUnitReporter.save_report(result, output_dir / f"{scenario.scenario_id}_report.xml")

    # 4. Save combined suite summary JSON
    suite_summary_data = {
        "suite_directory": str(scenarios_dir),
        "agent": agent_name,
        "total_scenarios": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "scenarios": [
            {
                "id": r.scenario.scenario_id,
                "name": r.scenario.name,
                "verdict": r.verdict,
                "passed": r.passed,
                "expected_effects": r.evaluation.expected_side_effects,
                "actual_effects": r.evaluation.actual_side_effects,
                "duplicate_effects": r.evaluation.duplicate_effect_count,
                "duration_ms": r.evaluation.duration_ms,
            }
            for r in results
        ],
    }
    summary_path = output_dir / "suite_summary.json"
    summary_path.write_text(json.dumps(suite_summary_data, indent=2), encoding="utf-8")

    # 5. Output Results
    if json_summary:
        console.print(json.dumps(suite_summary_data, indent=2))
    else:
        table = Table(title=f"RecoveryBench Suite Results ({agent_name})", box=box.ROUNDED)
        table.add_column("Status", style="bold")
        table.add_column("Scenario ID", style="cyan bold")
        table.add_column("Verdict", style="white")
        table.add_column("Committed (Exp/Act)", style="blue")
        table.add_column("Duplicates", style="red")
        table.add_column("Key Stable", style="yellow")
        table.add_column("Duration", style="dim")

        for r in results:
            status = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
            dup_str = f"[bold red]{r.evaluation.duplicate_effect_count}[/bold red]" if r.evaluation.duplicate_effect_count else "0"
            key_str = "[green]Yes[/green]" if r.evaluation.idempotency_key_stable else "[red]No[/red]"
            effects_str = f"{r.evaluation.expected_side_effects} / {r.evaluation.actual_side_effects}"
            table.add_row(
                status,
                r.scenario.scenario_id,
                r.verdict,
                effects_str,
                dup_str,
                key_str,
                f"{r.evaluation.duration_ms:.1f}ms",
            )

        console.print(table)
        console.print(f"\n[green]All reports saved to '{output_dir}'. Summary saved to '{summary_path}'.[/green]")

    # Append to GitHub Step Summary
    append_github_step_summary(results)

    # 6. Exit Codes: 0 if passed, 1 if assertion failure, 2 if configuration error, 3 if infrastructure error
    if any(
        r.evaluation.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR
        and any(a.name == "configuration_integrity" for a in r.evaluation.assertions)
        for r in results
    ):
        raise typer.Exit(code=2)
    elif any(r.evaluation.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR for r in results):
        raise typer.Exit(code=3)
    elif all(r.passed for r in results):
        raise typer.Exit(code=0)
    else:
        raise typer.Exit(code=1)


@app.command()
def replay(
    run_id: str = typer.Argument(..., help="Historical run ID to replay"),
    json_output: bool = typer.Option(False, "--json", help="Output trace as JSON"),
) -> None:
    """Reconstruct and inspect a historical execution trace from the ledger."""
    settings = get_settings()
    ledger = SQLiteLedger(settings.db_path)
    replay_engine = ReplayEngine(ledger)
    trace = replay_engine.replay_run(run_id)

    if not trace:
        err_console.print(f"[red]Run '{run_id}' not found in ledger database ({settings.db_path}).[/red]")
        raise typer.Exit(code=2)

    if json_output:
        import json
        console.print(json.dumps(trace.model_dump(), indent=2, default=str))
        raise typer.Exit(code=0)

    summary = trace.summary
    console.print(
        Panel(
            f"[bold]Run ID:[/bold] {summary.run_id}\n"
            f"[bold]Scenario:[/bold] {summary.scenario_id} | [bold]Agent:[/bold] {summary.agent_id}\n"
            f"[bold]Verdict:[/bold] {summary.verdict.value} | [bold]Duration:[/bold] {summary.duration_ms:.1f}ms\n"
            f"[bold]Total Events:[/bold] {summary.total_events} | [bold]Committed Effects:[/bold] {summary.committed_effects} "
            f"(Duplicates: {summary.duplicate_effects})\n"
            f"[bold]Timestamp:[/bold] {summary.created_at}",
            title="Reconstructed Run Trace",
            border_style="cyan",
        )
    )

    table = Table(title="Historical Event Sequence", box=box.ROUNDED)
    table.add_column("Step", justify="right", style="cyan")
    table.add_column("Attempt", justify="center")
    table.add_column("Tool:Action", style="blue")
    table.add_column("Execution Status", justify="center")
    table.add_column("Commit Status", justify="center")
    table.add_column("Idempotency Hash", style="dim")
    table.add_column("Error", style="yellow")

    for step in trace.reconstructed_timeline:
        table.add_row(
            str(step["step"]),
            str(step["attempt"]),
            f"{step['tool']}:{step['action']}",
            str(step["execution_status"]),
            str(step["commit_status"]),
            str(step["idempotency_hash"] or "-"),
            str(step["error"] or "-"),
        )

    console.print(table)
    raise typer.Exit(code=0)


@app.command()
def report(
    run_id: str = typer.Argument(..., help="Run ID to export reports for"),
    format: str = typer.Option("all", "--format", "-f", help="Output format: 'json', 'html', 'junit', or 'all'"),
    output_dir: Path | None = typer.Option(None, "--output-dir", "-o", help="Target output directory"),
) -> None:
    """Generate HTML, JSON, and JUnit XML reports for a historical run."""
    settings = get_settings()
    target_dir = output_dir or settings.reports_dir
    target_dir.mkdir(parents=True, exist_ok=True)

    ledger = SQLiteLedger(settings.db_path)
    summary = ledger.get_run(run_id)
    if not summary:
        err_console.print(f"[red]Run '{run_id}' not found in ledger database.[/red]")
        raise typer.Exit(code=2)

    events = ledger.get_events(run_id)
    registry = ScenarioRegistry()
    scenario = registry.get(summary.scenario_id)

    if not scenario:
        err_console.print(f"[red]Scenario '{summary.scenario_id}' definition not found.[/red]")
        raise typer.Exit(code=2)

    evaluator = RecoveryEvaluator()
    dummy_agent_result = AgentResult(
        success=summary.passed,
        attempts=len(events),
        status_message="Historical replay reconstruction",
    )
    # Re-evaluate
    evaluation = evaluator.evaluate(
        events=events,
        assertions=scenario.assertions,
        agent_result=dummy_agent_result,
        duration_ms=summary.duration_ms,
    )
    from recoverybench.engine.context import RunResult
    run_result = RunResult(
        run_id=run_id,
        scenario=scenario,
        agent_id=summary.agent_id,
        evaluation=evaluation,
        events=events,
        summary=summary,
    )

    if format in ("json", "both", "all"):
        json_file = target_dir / f"{run_id}_report.json"
        JSONReporter.save_report(run_result, json_file)
        console.print(f"[green]Saved JSON report:[/green] {json_file}")

    if format in ("html", "both", "all"):
        html_file = target_dir / f"{run_id}_report.html"
        HTMLReporter.save_report(run_result, html_file)
        console.print(f"[green]Saved HTML report:[/green] {html_file}")

    if format in ("junit", "xml", "all"):
        junit_file = target_dir / f"{run_id}_report.xml"
        JUnitReporter.save_report(run_result, junit_file)
        console.print(f"[green]Saved JUnit XML report:[/green] {junit_file}")

    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
