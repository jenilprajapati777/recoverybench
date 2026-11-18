"""JSON report serializer for machine-readable output adhering to the stable report contract."""

import json
from pathlib import Path
from typing import Any, cast

from recoverybench.engine.context import RunResult
from recoverybench.errors import ReportValidationError
from recoverybench.models import (
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
)
from recoverybench.reporting.schema import (
    CURRENT_REPORT_SCHEMA_VERSION,
    ReportModel,
    ReportValidator,
    SideEffectSafety,
    determine_side_effect_safety,
)
from recoverybench.security import redact_sensitive_data


def get_package_version() -> str:
    """Retrieve the installed package version safely."""
    try:
        from recoverybench import __version__

        return __version__
    except Exception:
        return "0.1.0"


def classify_side_effect_safety(result: RunResult) -> SideEffectSafety:
    """Classify side-effect safety using the unified determine_side_effect_safety priority rules."""
    side_assertion = next((a for a in result.evaluation.assertions if a.name == "side_effect_count"), None)
    has_incomplete = any(
        e.commit_status == CommitStatus.COMMITTED and (
            not e.effect_id or not e.effect_id.strip() or
            not e.operation_id or not e.operation_id.strip()
        )
        for e in result.events
    )
    return determine_side_effect_safety(
        actual_count=result.evaluation.actual_side_effects,
        duplicate_count=result.evaluation.duplicate_effect_count,
        expected_count=result.scenario.assertions.expected_side_effects,
        verdict=result.evaluation.verdict.value,
        has_events=len(result.events) > 0,
        side_effect_assertion_passed=side_assertion.passed if side_assertion else None,
        has_incomplete_identity=has_incomplete,
    )


def compute_execution_outcome(result: RunResult) -> dict[str, Any]:
    """Compute explicit categorical outcomes distinguishing execution, recovery, safety, and errors."""
    verdict = result.evaluation.verdict.value
    is_infra = result.evaluation.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR
    has_config_integrity = any(a.name == "configuration_integrity" for a in result.evaluation.assertions)

    validation_failure = is_infra and has_config_integrity
    runtime_failure = is_infra and not has_config_integrity
    execution_success = not is_infra
    recovery_success = bool(result.evaluation.passed and result.evaluation.verdict == EvaluationVerdict.PASS)

    safety = classify_side_effect_safety(result)
    side_effect_safe = (safety == SideEffectSafety.VERIFIED_SAFE)

    return {
        "verdict": verdict,
        "execution_success": execution_success,
        "recovery_success": recovery_success,
        "side_effect_safety": safety.value,
        "side_effect_safe": side_effect_safe,
        "validation_failure": validation_failure,
        "runtime_failure": runtime_failure,
    }


class JSONReporter:
    """Serializes test run results to machine-readable JSON adhering to the stable report contract."""

    @classmethod
    def generate_report_model(cls, result: RunResult) -> ReportModel:
        """Convert RunResult into a validated ReportModel instance."""
        report_dict = cls.generate_report_dict(result)
        return ReportValidator.validate_dict(report_dict, source_label=f"run_{result.run_id}")

    @classmethod
    def generate_report_dict(cls, result: RunResult) -> dict[str, Any]:
        """Convert RunResult into the standardized report dictionary with sensitive data redacted."""
        pkg_version = get_package_version()
        outcome = compute_execution_outcome(result)
        safety = classify_side_effect_safety(result)

        # Authoritative expected count
        expected_count = result.scenario.assertions.expected_side_effects

        # 1. Committed effect IDs
        committed_ids: list[str] = []
        for e in result.events:
            if e.commit_status == CommitStatus.COMMITTED and e.effect_id and e.effect_id.strip():
                eff_id = e.effect_id.strip()
                if eff_id not in committed_ids:
                    committed_ids.append(eff_id)

        side_effects_dict: dict[str, Any] = {
            "expected_count": expected_count,
            "actual_count": result.evaluation.actual_side_effects,
            "duplicate_count": result.evaluation.duplicate_effect_count,
            "idempotency_key_stable": result.evaluation.idempotency_key_stable,
            "safety_status": safety.value,
            "safe": outcome["side_effect_safe"],
            "committed_effect_ids": committed_ids,
        }

        # 2. Fault injection details
        configured_faults = [
            {
                "type": f.type.value,
                "target_tool": f.target_tool,
                "target_action": f.target_action,
                "failure_count": f.failure_count,
                "error_message": f.error_message,
            }
            for f in result.scenario.faults
        ]

        injected_faults = [
            {
                "event_id": e.event_id,
                "attempt_number": e.attempt_number,
                "tool_name": e.tool_name,
                "action_type": e.action_type,
                "error_type": e.error_type,
                "commit_status": e.commit_status.value,
                "timestamp": e.simulated_timestamp,
            }
            for e in result.events
            if e.execution_status == ExecutionStatus.FAULT_INJECTED
        ]

        fault_injection_dict: dict[str, Any] = {
            "configured_faults": configured_faults,
            "injected_fault_count": len(injected_faults),
            "injected_faults": injected_faults,
        }

        # 3. Errors collection
        errors_list: list[str] = list(result.evaluation.failure_reasons)
        if result.agent_raw_result and isinstance(result.agent_raw_result, dict):
            agent_err = result.agent_raw_result.get("error")
            if agent_err and isinstance(agent_err, str) and agent_err not in errors_list:
                errors_list.append(agent_err)

        # 4. Metadata
        metadata_dict: dict[str, Any] = {
            "agent_id": result.agent_id,
            "isolation_mode": getattr(result, "isolation_mode", "trusted"),
            "created_at": getattr(result.summary, "created_at", ""),
            "duration_ms": result.summary.duration_ms,
            "total_events": len(result.events),
            "max_retries_observed": result.evaluation.max_retries_observed,
            "seed": result.scenario.seed,
        }

        # 5. Scenario Report Info
        scenario_dict: dict[str, Any] = {
            "id": result.scenario.scenario_id,
            "name": result.scenario.name,
            "description": result.scenario.description,
            "schema_version": result.scenario.schema_version,
            "target_tool": result.scenario.target_tool,
            "target_action": result.scenario.target_action,
            "is_single_operation": result.scenario.is_single_operation,
        }

        # 6. Evaluation Details
        evaluation_dict: dict[str, Any] = {
            "verdict": result.evaluation.verdict.value,
            "passed": result.evaluation.passed,
            "isolation_mode": getattr(result, "isolation_mode", "trusted"),
            "duration_ms": result.evaluation.duration_ms,
            "expected_side_effects": expected_count,
            "actual_side_effects": result.evaluation.actual_side_effects,
            "duplicate_effect_count": result.evaluation.duplicate_effect_count,
            "idempotency_key_stable": result.evaluation.idempotency_key_stable,
            "max_retries_observed": result.evaluation.max_retries_observed,
            "failure_reasons": result.evaluation.failure_reasons,
            "evidence_summary": result.evaluation.evidence_summary,
            "assertions": [
                {
                    "name": a.name,
                    "passed": a.passed,
                    "expected": a.expected,
                    "actual": a.actual,
                    "message": a.message,
                    "evidence": a.evidence,
                    "event_ids": a.event_ids,
                }
                for a in result.evaluation.assertions
            ],
        }

        # 7. Events List
        events_list: list[dict[str, Any]] = [
            {
                "event_id": e.event_id,
                "operation_id": e.operation_id,
                "attempt_id": e.attempt_id,
                "attempt_number": e.attempt_number,
                "tool_name": e.tool_name,
                "action_type": e.action_type,
                "execution_status": e.execution_status.value,
                "commit_status": e.commit_status.value,
                "effect_id": e.effect_id,
                "idempotency_key_hash": e.idempotency_key_hash,
                "request_fingerprint": e.request_fingerprint,
                "simulated_timestamp": e.simulated_timestamp,
                "error_type": e.error_type,
                "evidence_reference": e.evidence_reference,
                "details": e.details,
            }
            for e in result.events
        ]

        report: dict[str, Any] = {
            "report_schema_version": CURRENT_REPORT_SCHEMA_VERSION,
            "recoverybench_version": pkg_version,
            "version": pkg_version,  # Preserved for backward compatibility
            "run_id": result.run_id,
            "scenario": scenario_dict,
            "agent_id": result.agent_id,
            "isolation_mode": getattr(result, "isolation_mode", "trusted"),
            "outcome": outcome,
            "evaluation": evaluation_dict,
            "side_effects": side_effects_dict,
            "fault_injection": fault_injection_dict,
            "events": events_list,
            "errors": errors_list,
            "metadata": metadata_dict,
            "agent_result": result.agent_raw_result,
        }
        return cast(dict[str, Any], redact_sensitive_data(report))

    @classmethod
    def to_json_string(cls, result: RunResult | ReportModel | dict[str, Any], indent: int = 2) -> str:
        """Return deterministic JSON formatted string of run result, model, or dictionary.

        Guarantees sensitive data redaction across every supported input type.
        """
        data: dict[str, Any]
        if isinstance(result, ReportModel):
            data = result.model_dump()
        elif isinstance(result, dict):
            data = result
        else:
            data = cls.generate_report_dict(result)

        # Enforce sensitive data redaction across all serialization entry points
        sanitized = redact_sensitive_data(data)
        return json.dumps(sanitized, indent=indent, sort_keys=True, default=str)

    @classmethod
    def save_report(cls, result: RunResult | ReportModel | dict[str, Any], output_path: str | Path) -> Path:
        """Write deterministic report JSON to disk with sensitive data redacted."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cls.to_json_string(result), encoding="utf-8")
        return path

    @classmethod
    def validate_report(cls, report_input: str | Path | dict[str, Any]) -> ReportModel:
        """Validate serialized report from dictionary, JSON string, or file path."""
        if isinstance(report_input, dict):
            return ReportValidator.validate_dict(report_input)
        if isinstance(report_input, Path):
            return ReportValidator.validate_file(report_input)
        if isinstance(report_input, str):
            candidate_path = Path(report_input)
            if candidate_path.exists() and candidate_path.is_file():
                return ReportValidator.validate_file(candidate_path)
            return ReportValidator.validate_json(report_input)
        raise ReportValidationError(
            f"Unsupported report input type: {type(report_input).__name__}."
        )
