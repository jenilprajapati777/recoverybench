"""JSON report serializer for machine-readable output."""

import json
from pathlib import Path
from typing import cast

from recoverybench.engine.context import RunResult
from recoverybench.security import redact_sensitive_data


class JSONReporter:
    """Serializes test run results to machine-readable JSON."""

    @staticmethod
    def generate_report_dict(result: RunResult) -> dict[str, object]:
        """Convert RunResult into a clean, serializable dictionary with sensitive data redacted."""
        report: dict[str, object] = {
            "version": "0.1.0",
            "run_id": result.run_id,
            "scenario": {
                "id": result.scenario.scenario_id,
                "name": result.scenario.name,
                "target_tool": result.scenario.target_tool,
                "target_action": result.scenario.target_action,
            },
            "agent_id": result.agent_id,
            "isolation_mode": getattr(result, "isolation_mode", "trusted"),
            "evaluation": {
                "verdict": result.evaluation.verdict.value,
                "passed": result.evaluation.passed,
                "isolation_mode": getattr(result, "isolation_mode", "trusted"),
                "expected_side_effects": result.evaluation.expected_side_effects,
                "actual_side_effects": result.evaluation.actual_side_effects,
                "duplicate_effect_count": result.evaluation.duplicate_effect_count,
                "idempotency_key_stable": result.evaluation.idempotency_key_stable,
                "max_retries_observed": result.evaluation.max_retries_observed,
                "duration_ms": result.evaluation.duration_ms,
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
            },
            "events": [
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
                }
                for e in result.events
            ],
            "agent_result": result.agent_raw_result,
        }
        return cast(dict[str, object], redact_sensitive_data(report))

    @classmethod
    def to_json_string(cls, result: RunResult, indent: int = 2) -> str:
        """Return JSON formatted string of run result."""
        data = cls.generate_report_dict(result)
        return json.dumps(data, indent=indent, default=str)

    @classmethod
    def save_report(cls, result: RunResult, output_path: str | Path) -> Path:
        """Write report to file."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cls.to_json_string(result), encoding="utf-8")
        return path
