"""Reporting utilities: terminal, JSON, HTML, and JUnit XML."""

from recoverybench.errors import ReportValidationError
from recoverybench.reporting.console import render_comparison, render_run_result
from recoverybench.reporting.github_summary import (
    append_github_step_summary,
    generate_markdown_summary,
)
from recoverybench.reporting.html_reporter import HTMLReporter
from recoverybench.reporting.json_reporter import JSONReporter
from recoverybench.reporting.junit_reporter import JUnitReporter
from recoverybench.reporting.schema import (
    CURRENT_REPORT_SCHEMA_VERSION,
    SUPPORTED_REPORT_SCHEMA_VERSIONS,
    SUPPORTED_VERDICTS,
    AssertionReport,
    EvaluationReport,
    EventReport,
    ExecutionOutcome,
    FaultConfigReport,
    FaultInjectionReport,
    InjectedFaultReport,
    MetadataReport,
    ReportModel,
    ReportValidator,
    ScenarioReportInfo,
    SideEffectReport,
    SideEffectSafety,
    StrictReportBaseModel,
    determine_side_effect_safety,
)

__all__ = [
    "render_run_result",
    "render_comparison",
    "JSONReporter",
    "HTMLReporter",
    "JUnitReporter",
    "generate_markdown_summary",
    "append_github_step_summary",
    "CURRENT_REPORT_SCHEMA_VERSION",
    "SUPPORTED_REPORT_SCHEMA_VERSIONS",
    "SUPPORTED_VERDICTS",
    "ReportModel",
    "ReportValidator",
    "ReportValidationError",
    "StrictReportBaseModel",
    "determine_side_effect_safety",
    "ScenarioReportInfo",
    "ExecutionOutcome",
    "SideEffectReport",
    "SideEffectSafety",
    "FaultConfigReport",
    "InjectedFaultReport",
    "FaultInjectionReport",
    "AssertionReport",
    "EvaluationReport",
    "EventReport",
    "MetadataReport",
]
