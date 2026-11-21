"""JSON report serializer for machine-readable output adhering to the stable report contract."""

import json
import math
from pathlib import Path
from typing import Any, cast

from recoverybench.engine.context import RunResult
from recoverybench.errors import IncompatibleSchemaVersionError, ReportValidationError
from recoverybench.models import (
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
)
from recoverybench.reporting.schema import (
    CURRENT_REPORT_SCHEMA_VERSION,
    SUPPORTED_REPORT_SCHEMA_VERSIONS,
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

    behavioral_verdict = getattr(result.evaluation, "behavioral_verdict", None)
    if behavioral_verdict is None:
        behavioral_verdict = verdict

    side_effect_status = getattr(result.evaluation, "side_effect_status", None)
    if side_effect_status is None:
        if safety == SideEffectSafety.VERIFIED_SAFE:
            side_effect_status = "EXACT_MATCH"
        elif result.evaluation.actual_side_effects == 0 and result.scenario.assertions.expected_side_effects == 0:
            side_effect_status = "CONFIRMED_ABSENT"
        elif result.evaluation.duplicate_effect_count > 0:
            side_effect_status = "DUPLICATE_COMMITTED"
        else:
            side_effect_status = "UNRESOLVED_INTERVAL"

    primary_infra = getattr(result.evaluation, "primary_infrastructure_status", None)
    if primary_infra is None:
        primary_infra = "CLEAN"

    recon_infra = getattr(result.evaluation, "reconciliation_infrastructure_status", None)
    overall_outcome = getattr(result.evaluation, "overall_outcome", None) or verdict
    finality = getattr(result.evaluation, "execution_finality", "FINAL")

    return {
        "verdict": verdict,
        "execution_success": execution_success,
        "recovery_success": recovery_success,
        "side_effect_safety": safety.value,
        "side_effect_safe": side_effect_safe,
        "validation_failure": validation_failure,
        "runtime_failure": runtime_failure,
        "behavioral_verdict": behavioral_verdict,
        "side_effect_status": side_effect_status,
        "primary_infrastructure_status": primary_infra,
        "reconciliation_infrastructure_status": recon_infra,
        "overall_outcome": overall_outcome,
        "execution_finality": finality,
    }


class JSONReporter:
    """Serializes test run results to machine-readable JSON adhering to the stable report contract."""

    @classmethod
    def _check_lossless_predicate(cls, report_dict: dict[str, Any]) -> tuple[bool, str]:
        """Check all 10 conditions of the Lossless Compatibility Predicate P_lossless(R)."""
        # C1: All events commit_status in {COMMITTED, ABORTED, NOT_APPLICABLE}
        for idx, ev in enumerate(report_dict.get("events", [])):
            c_status = ev.get("commit_status")
            if c_status not in ("COMMITTED", "ABORTED", "NOT_APPLICABLE"):
                return False, f"C1: Event #{idx+1} has non-v1 commit_status '{c_status}' (zero UNCERTAIN events permitted)"

        # C2: Scalar bounds only (bounds[0] == bounds[1] != infinity)
        side = report_dict.get("side_effects", {})
        bounds = side.get("bounds", [0, 0])
        if not isinstance(bounds, list) or len(bounds) != 2:
            return False, f"C2: Invalid bounds format: {bounds}"
        if bounds[0] != bounds[1]:
            return False, f"C2: Non-scalar bounds [{bounds[0]}, {bounds[1]}]"
        if bounds[1] in ("inf", "infinity", float("inf"), math.inf):
            return False, f"C2: Unbounded upper capacity limit {bounds[1]}"

        # C3: uncertain_count == 0
        uncertain_count = side.get("uncertain_count", 0)
        if uncertain_count != 0:
            return False, f"C3: Non-zero uncertain_count: {uncertain_count}"

        # C4: execution_finality == FINAL
        outcome = report_dict.get("outcome", {})
        finality = outcome.get("execution_finality", "FINAL")
        if finality != "FINAL":
            return False, f"C4: Execution finality is '{finality}' (must be FINAL)"

        # C5: metadata.unresolved_workers == 0
        unresolved = report_dict.get("metadata", {}).get("unresolved_workers", 0)
        if unresolved != 0:
            return False, f"C5: Non-zero unresolved_workers: {unresolved}"

        # C6: outcome.infrastructure_status == CLEAN
        infra = outcome.get("primary_infrastructure_status", "CLEAN")
        if infra != "CLEAN":
            return False, f"C6: Primary infrastructure status is '{infra}' (must be CLEAN)"

        # C7: behavioral_verdict == overall_outcome
        bv = outcome.get("behavioral_verdict")
        oo = outcome.get("overall_outcome", outcome.get("verdict"))
        if bv is not None and oo is not None and bv != oo:
            return False, f"C7: Split verdict detected: behavioral_verdict '{bv}' != overall_outcome '{oo}'"

        # C8: supplemental_reconciliation is None and len(late_events) == 0
        if report_dict.get("supplemental_reconciliation") is not None:
            return False, "C8: Supplemental reconciliation data is present"
        if len(report_dict.get("late_events", [])) > 0:
            return False, "C8: Late outcome events are present"

        # C9: No active v2 event metadata fields or unknown unmapped fields
        for idx, ev in enumerate(report_dict.get("events", [])):
            for ef in ("uncertainty_reason", "reconciliation_status", "writer_id", "event_sequence"):
                if ev.get(ef) is not None:
                    return False, f"C9: Event #{idx+1} has active v2 field '{ef}'"
            cap = ev.get("capacity_provenance")
            if cap is not None and cap not in ("TIER_C1", "C1"):
                return False, f"C9: Event #{idx+1} has active capacity_provenance '{cap}'"

        known_v2_top_keys = {
            "report_schema_version", "recoverybench_version", "version", "run_id",
            "scenario", "agent_id", "isolation_mode", "outcome", "evaluation",
            "side_effects", "fault_injection", "events", "errors", "metadata",
            "agent_result", "supplemental_reconciliation", "late_events",
        }
        for k in report_dict:
            if k not in known_v2_top_keys:
                return False, f"C9: Unknown unmapped field '{k}' detected"

        # C10: overall_outcome in SUPPORTED_VERDICTS
        if oo not in ("PASS", "GENUINE_FAILURE", "INFRASTRUCTURE_ERROR", "INSUFFICIENT_EVIDENCE"):
            return False, f"C10: Unsupported verdict '{oo}'"

        return True, ""

    @classmethod
    def generate_report_model(cls, result: RunResult, schema_version: int = CURRENT_REPORT_SCHEMA_VERSION) -> ReportModel:
        """Convert RunResult into a validated ReportModel instance."""
        report_dict = cls.generate_report_dict(result, schema_version=schema_version)
        return ReportValidator.validate_dict(report_dict, source_label=f"run_{result.run_id}")

    @classmethod
    def generate_report_dict(
        cls, result: RunResult, schema_version: int = CURRENT_REPORT_SCHEMA_VERSION
    ) -> dict[str, Any]:
        """Convert RunResult into the standardized report dictionary with sensitive data redacted."""
        if schema_version not in SUPPORTED_REPORT_SCHEMA_VERSIONS:
            raise IncompatibleSchemaVersionError(
                f"Unsupported report_schema_version: {schema_version}. "
                f"Supported versions: {sorted(SUPPORTED_REPORT_SCHEMA_VERSIONS)}."
            )

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

        bounds = getattr(result.evaluation, "bounds", None)
        if bounds is None:
            bounds = [result.evaluation.actual_side_effects, result.evaluation.actual_side_effects]
        uncertain_count = getattr(result.evaluation, "uncertain_count", 0)

        side_effects_dict: dict[str, Any] = {
            "expected_count": expected_count,
            "actual_count": result.evaluation.actual_side_effects,
            "duplicate_count": result.evaluation.duplicate_effect_count,
            "idempotency_key_stable": result.evaluation.idempotency_key_stable,
            "safety_status": safety.value,
            "safe": outcome["side_effect_safe"],
            "committed_effect_ids": committed_ids,
            "uncertain_count": uncertain_count,
            "bounds": bounds,
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
            "unresolved_workers": getattr(
                result, "unresolved_workers", getattr(result.evaluation, "unresolved_workers", 0)
            ),
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
                "uncertainty_reason": getattr(e, "uncertainty_reason", None),
                "reconciliation_status": getattr(e, "reconciliation_status", None),
                "capacity_provenance": getattr(e, "capacity_provenance", None),
                "writer_id": getattr(e, "writer_id", None),
                "event_sequence": getattr(e, "event_sequence", None),
            }
            for e in result.events
        ]

        v2_report: dict[str, Any] = {
            "report_schema_version": 2,
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
            "supplemental_reconciliation": getattr(result, "supplemental_reconciliation", None),
            "late_events": getattr(result, "late_events", []),
        }

        if schema_version == 1:
            is_valid, reason = cls._check_lossless_predicate(v2_report)
            if not is_valid:
                raise IncompatibleSchemaVersionError(
                    f"Cannot project Schema v2 report to Schema v1: {reason}"
                )
            v1_report = json.loads(json.dumps(v2_report))
            v1_report["report_schema_version"] = 1
            v1_outcome = v1_report["outcome"]
            v1_outcome["verdict"] = v1_outcome.get("overall_outcome", v1_outcome.get("verdict"))
            for f in (
                "behavioral_verdict", "overall_outcome", "side_effect_status",
                "primary_infrastructure_status", "reconciliation_infrastructure_status",
                "execution_finality",
            ):
                v1_outcome.pop(f, None)

            v1_side = v1_report["side_effects"]
            if "bounds" in v1_side:
                v1_side["actual_count"] = v1_side["bounds"][0]
                v1_side.pop("bounds", None)
            v1_side.pop("uncertain_count", None)

            v1_report["metadata"].pop("unresolved_workers", None)

            for ev in v1_report.get("events", []):
                for ef in ("uncertainty_reason", "reconciliation_status", "capacity_provenance", "writer_id", "event_sequence"):
                    ev.pop(ef, None)

            v1_report.pop("supplemental_reconciliation", None)
            v1_report.pop("late_events", None)
            return cast(dict[str, Any], redact_sensitive_data(v1_report))

        return cast(dict[str, Any], redact_sensitive_data(v2_report))

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
