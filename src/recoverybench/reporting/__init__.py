"""Reporting utilities: terminal, JSON, HTML, and JUnit XML."""

from recoverybench.reporting.console import render_comparison, render_run_result
from recoverybench.reporting.github_summary import (
    append_github_step_summary,
    generate_markdown_summary,
)
from recoverybench.reporting.html_reporter import HTMLReporter
from recoverybench.reporting.json_reporter import JSONReporter
from recoverybench.reporting.junit_reporter import JUnitReporter

__all__ = [
    "render_run_result",
    "render_comparison",
    "JSONReporter",
    "HTMLReporter",
    "JUnitReporter",
    "generate_markdown_summary",
    "append_github_step_summary",
]
