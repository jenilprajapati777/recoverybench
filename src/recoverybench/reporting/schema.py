"""Report schema definitions, Pydantic models, and validators for RecoveryBench execution reports."""

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from recoverybench.errors import ConfigurationError, ReportValidationError
from recoverybench.models import CommitStatus
from recoverybench.scenarios.schema import SUPPORTED_SCENARIO_SCHEMA_VERSIONS
from recoverybench.security import redact_sensitive_data

CURRENT_REPORT_SCHEMA_VERSION: int = 2
SUPPORTED_REPORT_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2})

SUPPORTED_VERDICTS: frozenset[str] = frozenset({
    "PASS",
    "GENUINE_FAILURE",
    "INFRASTRUCTURE_ERROR",
    "INSUFFICIENT_EVIDENCE",
})

VALID_BEHAVIORAL_VERDICTS: frozenset[str] = frozenset({
    "PASS",
    "GENUINE_FAILURE",
    "INSUFFICIENT_EVIDENCE",
})

VALID_EXTERNAL_STATUSES: frozenset[str] = frozenset({
    "EXACT_MATCH",
    "DUPLICATE_COMMITTED",
    "MISSING_COMMITS",
    "CONFIRMED_ABSENT",
    "UNRESOLVED_INTERVAL",
    "UNBOUNDED_AMBIGUITY",
    "AUDIT_OBSERVED_NON_FINAL",
    "CONTRADICTORY_EVIDENCE",
})

VALID_PRIMARY_INFRASTRUCTURE_STATUSES: frozenset[str] = frozenset({
    "CLEAN",
    "WORKER_TIMED_OUT",
    "THREAD_LEAKED",
    "PROCESS_TERMINATED",
    "INTEGRITY_VIOLATION",
})

VALID_RECONCILIATION_INFRASTRUCTURE_STATUSES: frozenset[str] = frozenset({
    "CLEAN",
    "RECONCILIATION_FAILED",
})

VALID_EXECUTION_FINALITIES: frozenset[str] = frozenset({
    "FINAL",
    "PROVISIONAL",
})

SUPPORTED_COMMIT_STATUSES: frozenset[str] = frozenset(s.value for s in CommitStatus)


class SideEffectSafety(StrEnum):
    """Explicit three-state classification of side-effect safety."""

    VERIFIED_SAFE = "VERIFIED_SAFE"
    UNSAFE = "UNSAFE"
    UNVERIFIED = "UNVERIFIED"


def determine_side_effect_safety(
    *,
    actual_count: int,
    duplicate_count: int,
    expected_count: int | None,
    verdict: str,
    has_events: bool = True,
    side_effect_assertion_passed: bool | None = None,
    has_incomplete_identity: bool = False,
) -> SideEffectSafety:
    """Deterministically classify side-effect safety with strict priority ordering:

    1. duplicate_count > 0 -> UNSAFE (proven duplicates take highest precedence over insufficient evidence)
    2. Known unexpected count with adequate ledger evidence -> UNSAFE
    3. Insufficient evidence without proven unsafe effects -> UNVERIFIED
    4. Infrastructure crash before events were recorded -> UNVERIFIED
    5. Expected count not specified (expected_count is None) or positive expected count without events -> UNVERIFIED
    6. Incomplete committed-event identity evidence -> UNVERIFIED
    7. Exact expected count with sufficient evidence and 0 duplicates -> VERIFIED_SAFE
    """
    # 1. Proven duplicates take highest priority regardless of verdict
    if duplicate_count > 0:
        return SideEffectSafety.UNSAFE

    # 2. Known unexpected count with adequate ledger evidence (or evaluated failure)
    if expected_count is not None and actual_count != expected_count:
        if has_events or verdict in ("PASS", "GENUINE_FAILURE"):
            return SideEffectSafety.UNSAFE

    # 3. Insufficient evidence without proven unsafe effects
    if verdict == "INSUFFICIENT_EVIDENCE":
        return SideEffectSafety.UNVERIFIED

    # 4. Infrastructure crash before events were recorded
    if verdict == "INFRASTRUCTURE_ERROR" and not has_events:
        return SideEffectSafety.UNVERIFIED

    # 5. Unspecified expected count (open-ended scenario) or positive expected count lacking ledger events
    if expected_count is None or (expected_count > 0 and not has_events):
        return SideEffectSafety.UNVERIFIED

    # 6. Incomplete committed-event identity evidence prevents safe classification
    if has_incomplete_identity:
        return SideEffectSafety.UNVERIFIED

    # If side_effect_assertion_passed is explicitly False, cannot be verified safe
    if side_effect_assertion_passed is False:
        return SideEffectSafety.UNVERIFIED

    # 7. Exact expected count with sufficient evidence and zero duplicates
    return SideEffectSafety.VERIFIED_SAFE


class StrictReportBaseModel(BaseModel):
    """Shared base model for report schema contract enforcing strict typing and forbidding unknown fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ScenarioReportInfo(StrictReportBaseModel):
    """Scenario identity and metadata embedded in the report."""

    id: str = Field(description="Scenario identifier")
    name: str = Field(description="Scenario display name")
    description: str | None = Field(default=None, description="Scenario description")
    schema_version: int = Field(description="Scenario schema version")
    target_tool: str = Field(description="Target tool name")
    target_action: str | None = Field(default=None, description="Target tool action")
    is_single_operation: bool = Field(default=True, description="Whether scenario is single operation")


class ExecutionOutcome(StrictReportBaseModel):
    """Categorical classification of the execution and recovery outcomes."""

    verdict: str = Field(description="High-level evaluation verdict string (PASS, GENUINE_FAILURE, etc.)")
    execution_success: bool = Field(
        description="True if agent execution ran to completion without harness infrastructure crash"
    )
    recovery_success: bool = Field(
        description="True if all recovery assertions passed"
    )
    side_effect_safety: SideEffectSafety = Field(
        description="Explicit three-state safety classification: VERIFIED_SAFE, UNSAFE, or UNVERIFIED"
    )
    side_effect_safe: bool = Field(
        description="True ONLY if side_effect_safety is VERIFIED_SAFE"
    )
    validation_failure: bool = Field(
        description="True if run failed due to scenario, parameter, or configuration validation"
    )
    runtime_failure: bool = Field(
        description="True if run failed due to unexpected runtime/infrastructure failure"
    )
    behavioral_verdict: str | None = Field(
        default=None, description="Dimension 1: Behavioral classification (PASS, GENUINE_FAILURE, INSUFFICIENT_EVIDENCE)"
    )
    side_effect_status: str | None = Field(
        default=None, description="Dimension 2: External side-effect status"
    )
    primary_infrastructure_status: str | None = Field(
        default=None, description="Dimension 3: Primary worker containment status"
    )
    reconciliation_infrastructure_status: str | None = Field(
        default=None, description="Optional Dimension: Post-cutoff reconciliation status"
    )
    overall_outcome: str | None = Field(
        default=None, description="Dimension 4: Authoritative synthesized overall outcome"
    )
    execution_finality: str | None = Field(
        default=None, description="Execution finality: FINAL or PROVISIONAL"
    )

    @field_validator("side_effect_safety", mode="before")
    @classmethod
    def validate_side_effect_safety(cls, v: Any) -> SideEffectSafety:
        if isinstance(v, SideEffectSafety):
            return v
        if isinstance(v, str):
            try:
                return SideEffectSafety(v)
            except ValueError:
                raise ValueError(
                    f"Invalid side_effect_safety: '{v}'. Must be one of {[s.value for s in SideEffectSafety]}."
                ) from None
        raise ValueError(f"Invalid side_effect_safety type '{type(v).__name__}'. Must be string or SideEffectSafety.")

    @field_validator("verdict")
    @classmethod
    def validate_verdict(cls, v: str) -> str:
        if v not in SUPPORTED_VERDICTS:
            raise ValueError(f"Unsupported verdict '{v}'. Supported verdicts: {sorted(SUPPORTED_VERDICTS)}.")
        return v


def _default_bounds() -> list[int | str]:
    return [0, 0]


class SideEffectReport(StrictReportBaseModel):
    """Detailed verification results for tool side-effects and mutation safety."""

    expected_count: int | None = Field(default=None, description="Authoritative expected committed side effects")
    actual_count: int = Field(default=0, description="Actual committed side effects observed")
    duplicate_count: int = Field(default=0, description="Duplicate side effects detected")
    idempotency_key_stable: bool = Field(default=True, description="Whether idempotency keys remained stable")
    safety_status: SideEffectSafety = Field(
        default=SideEffectSafety.UNVERIFIED,
        description="Explicit three-state safety classification: VERIFIED_SAFE, UNSAFE, or UNVERIFIED",
    )
    safe: bool = Field(default=False, description="True ONLY if safety_status is VERIFIED_SAFE")
    committed_effect_ids: list[str] = Field(default_factory=list, description="Identifiers of committed effects")
    uncertain_count: int = Field(default=0, description="Count of uncertain side-effects")
    bounds: list[int | str] = Field(
        default_factory=_default_bounds,
        description="Two-layer accounting effect bounds [S_min, S_max]",
    )

    @field_validator("safety_status", mode="before")
    @classmethod
    def validate_safety_status(cls, v: Any) -> SideEffectSafety:
        if isinstance(v, SideEffectSafety):
            return v
        if isinstance(v, str):
            try:
                return SideEffectSafety(v)
            except ValueError:
                raise ValueError(
                    f"Invalid safety_status: '{v}'. Must be one of {[s.value for s in SideEffectSafety]}."
                ) from None
        raise ValueError(f"Invalid safety_status type '{type(v).__name__}'. Must be string or SideEffectSafety.")


class FaultConfigReport(StrictReportBaseModel):
    """Specification of a configured fault in the scenario."""

    type: str = Field(description="Fault type name")
    target_tool: str = Field(description="Target tool for fault injection")
    target_action: str | None = Field(default=None, description="Target action for fault injection")
    failure_count: int = Field(default=1, description="Number of times fault is injected")
    error_message: str | None = Field(default=None, description="Error message injected")


class InjectedFaultReport(StrictReportBaseModel):
    """Record of a fault that was actively injected during execution."""

    event_id: int | None = Field(default=None, description="Ledger sequence event ID")
    attempt_number: int = Field(description="Attempt number when fault was injected")
    tool_name: str = Field(description="Target tool name")
    action_type: str = Field(description="Target action name")
    error_type: str | None = Field(default=None, description="Injected error class/type")
    commit_status: str = Field(description="Commit status when fault occurred")
    timestamp: str = Field(description="ISO-8601 timestamp of injection")

    @field_validator("commit_status", mode="before")
    @classmethod
    def validate_commit_status(cls, v: Any) -> str:
        if isinstance(v, CommitStatus):
            return v.value
        if isinstance(v, str):
            if v in SUPPORTED_COMMIT_STATUSES:
                return v
            raise ValueError(
                f"Invalid commit_status '{v}'. Must be one of {sorted(SUPPORTED_COMMIT_STATUSES)}."
            )
        raise ValueError(f"Invalid commit_status type '{type(v).__name__}'. Must be string or CommitStatus.")


class FaultInjectionReport(StrictReportBaseModel):
    """Comprehensive summary of configured and observed fault injections."""

    configured_faults: list[FaultConfigReport] = Field(default_factory=list, description="Configured scenario faults")
    injected_fault_count: int = Field(default=0, description="Count of faults actively injected during run")
    injected_faults: list[InjectedFaultReport] = Field(default_factory=list, description="Observed injected faults")


class AssertionReport(StrictReportBaseModel):
    """Individual assertion outcome with supporting ledger evidence."""

    name: str = Field(description="Assertion name")
    passed: bool = Field(description="True if assertion passed")
    expected: Any = Field(description="Expected value or condition")
    actual: Any = Field(description="Actual observed value")
    message: str = Field(description="Human-readable explanation of verdict")
    evidence: list[str] = Field(default_factory=list, description="Concrete ledger evidence lines")
    event_ids: list[int] = Field(default_factory=list, description="Citations of ledger event sequence numbers")


class EvaluationReport(StrictReportBaseModel):
    """Complete recovery evaluation details and assertion outcomes."""

    verdict: str = Field(description="Evaluation verdict string")
    passed: bool = Field(description="True if all assertions passed")
    isolation_mode: str = Field(default="trusted", description="Execution isolation mode")
    duration_ms: float = Field(default=0.0, description="Evaluation duration in milliseconds")
    expected_side_effects: int | None = Field(default=None, description="Authoritative expected side-effects")
    actual_side_effects: int = Field(default=0, description="Actual side-effects observed")
    duplicate_effect_count: int = Field(default=0, description="Duplicate effects detected")
    idempotency_key_stable: bool = Field(default=True, description="Idempotency key stability")
    max_retries_observed: int = Field(default=0, description="Maximum retries observed")
    failure_reasons: list[str] = Field(default_factory=list, description="Reasons for assertion failures")
    evidence_summary: dict[str, list[str]] = Field(default_factory=dict, description="Summary of evidence cited")
    assertions: list[AssertionReport] = Field(default_factory=list, description="Detailed assertion list")

    @field_validator("verdict")
    @classmethod
    def validate_verdict(cls, v: str) -> str:
        if v not in SUPPORTED_VERDICTS:
            raise ValueError(f"Unsupported verdict '{v}'. Supported verdicts: {sorted(SUPPORTED_VERDICTS)}.")
        return v


class EventReport(StrictReportBaseModel):
    """Single immutable ledger event recorded in execution trace."""

    event_id: int | None = Field(default=None, description="Monotonic sequence number")
    operation_id: str | None = Field(default=None, description="Logical operation identifier")
    attempt_id: str | None = Field(default=None, description="Unique attempt invocation identifier")
    attempt_number: int = Field(description="1-based attempt sequence number")
    tool_name: str = Field(description="Target tool called")
    action_type: str = Field(description="Action executed")
    execution_status: str = Field(description="Invocation execution status")
    commit_status: str = Field(description="State of mutation")
    effect_id: str | None = Field(default=None, description="Persistent mutation identifier")
    idempotency_key_hash: str | None = Field(default=None, description="Hash of idempotency key")
    request_fingerprint: str = Field(description="Canonical fingerprint of payload")
    simulated_timestamp: str = Field(description="ISO-8601 timestamp")
    error_type: str | None = Field(default=None, description="Error name if raised")
    evidence_reference: str | None = Field(default=None, description="Transaction reference")
    details: dict[str, Any] = Field(default_factory=dict, description="Sanitized metadata")
    uncertainty_reason: str | None = Field(default=None, description="Uncertainty reason classification")
    reconciliation_status: str | None = Field(default=None, description="Status of late reconciliation")
    capacity_provenance: str | None = Field(default=None, description="Capacity provenance tier C1..C4")
    writer_id: str | None = Field(default=None, description="Writer identity: THREAD_HOOK, PROCESS_SUPERVISOR, RECON_ENGINE")
    event_sequence: int | None = Field(default=None, description="Monotonic sequence number per writer")

    @field_validator("commit_status", mode="before")
    @classmethod
    def validate_commit_status(cls, v: Any) -> str:
        if isinstance(v, CommitStatus):
            return v.value
        if isinstance(v, str):
            if v in SUPPORTED_COMMIT_STATUSES:
                return v
            raise ValueError(
                f"Invalid commit_status '{v}'. Must be one of {sorted(SUPPORTED_COMMIT_STATUSES)}."
            )
        raise ValueError(f"Invalid commit_status type '{type(v).__name__}'. Must be string or CommitStatus.")

    @model_validator(mode="after")
    def validate_event_identity(self) -> Self:
        # 1. Whitespace-only string rejection for string fields
        if self.effect_id is not None and not self.effect_id.strip():
            raise ValueError("Field 'effect_id' cannot be empty or whitespace-only.")
        if self.operation_id is not None and not self.operation_id.strip():
            raise ValueError("Field 'operation_id' cannot be empty or whitespace-only.")
        if self.attempt_id is not None and not self.attempt_id.strip():
            raise ValueError("Field 'attempt_id' cannot be empty or whitespace-only.")
        if not self.tool_name.strip():
            raise ValueError("Field 'tool_name' cannot be empty or whitespace-only.")
        if not self.action_type.strip():
            raise ValueError("Field 'action_type' cannot be empty or whitespace-only.")
        if not self.request_fingerprint.strip():
            raise ValueError("Field 'request_fingerprint' cannot be empty or whitespace-only.")

        # 2. A COMMITTED event must have a non-empty effect_id
        if self.commit_status == CommitStatus.COMMITTED.value:
            if self.effect_id is None or not self.effect_id.strip():
                raise ValueError("A COMMITTED event must have a non-empty effect_id.")

        return self


class MetadataReport(StrictReportBaseModel):
    """Execution environment, duration, and run metadata."""

    agent_id: str = Field(description="Agent identifier")
    isolation_mode: str = Field(default="trusted", description="Execution backend isolation")
    created_at: str = Field(description="ISO-8601 run start/creation timestamp")
    duration_ms: float = Field(default=0.0, description="Execution wall-clock duration in ms")
    total_events: int = Field(default=0, description="Total ledger events recorded")
    max_retries_observed: int = Field(default=0, description="Max observed retry attempts")
    seed: int | None = Field(default=None, description="PRNG seed override if applied")
    unresolved_workers: int = Field(default=0, description="Count of unresolved in-flight workers at cutoff")


class SupplementalReconciliationReport(StrictReportBaseModel):
    """Artifact 3: Supplemental reconciliation report recording post-cutoff audit and late completions."""

    reconciliation_id: str = Field(description="UUID of reconciliation operation")
    parent_report_sha256: str = Field(description="SHA-256 of immutable primary report")
    sequence_number: int = Field(ge=1, description="Strictly ordered sequence number")
    reconciliation_status: str = Field(description="Reconciliation status: EXACT_MATCH, UNRESOLVED_INTERVAL, etc.")
    reconciliation_infrastructure_status: str = Field(
        description="Reconciliation infra status: CLEAN, RECONCILIATION_FAILED"
    )
    bounds: list[int | str] = Field(default_factory=_default_bounds, description="Reconciled bounds")
    late_events: list[dict[str, Any]] = Field(default_factory=list, description="Late events processed")
    reconciliation_timestamp: str = Field(default="", description="ISO-8601 timestamp")
    details: dict[str, Any] = Field(default_factory=dict, description="Reconciliation metadata")


class ReportModel(StrictReportBaseModel):
    """Top-level standardized RecoveryBench execution report."""

    report_schema_version: int = Field(description="Report schema contract version (independent integer)")
    recoverybench_version: str = Field(description="RecoveryBench package version")
    version: str = Field(default="0.1.0", description="Legacy version string for backward compatibility")
    run_id: str = Field(description="Unique run execution identifier")
    scenario: ScenarioReportInfo = Field(description="Scenario under test metadata")
    agent_id: str = Field(description="Evaluated agent identifier")
    isolation_mode: str = Field(default="trusted", description="Execution isolation mode")
    outcome: ExecutionOutcome = Field(description="Categorical execution and recovery outcomes")
    evaluation: EvaluationReport = Field(description="Comprehensive recovery assertion results")
    side_effects: SideEffectReport = Field(description="Side effect mutation audit")
    fault_injection: FaultInjectionReport = Field(description="Configured and injected faults")
    events: list[EventReport] = Field(default_factory=list, description="Raw append-only ledger events")
    errors: list[str] = Field(default_factory=list, description="Validation, execution, and assertion errors")
    metadata: MetadataReport = Field(description="Execution run metadata")
    agent_result: dict[str, Any] | None = Field(default=None, description="Raw agent result dictionary")
    supplemental_reconciliation: SupplementalReconciliationReport | dict[str, Any] | None = Field(
        default=None, description="Artifact 3: Supplemental reconciliation report"
    )
    late_events: list[dict[str, Any]] = Field(
        default_factory=list, description="Late-arriving outcome events"
    )

    @field_validator("report_schema_version", mode="before")
    @classmethod
    def validate_schema_version(cls, v: Any) -> int:
        if v is None:
            raise ValueError(
                f"Missing required field 'report_schema_version'. "
                f"Supported versions: {sorted(SUPPORTED_REPORT_SCHEMA_VERSIONS)}."
            )
        if type(v) is not int:
            raise ValueError(
                f"Invalid report_schema_version type '{type(v).__name__}'. "
                f"report_schema_version must be an integer, supported versions: "
                f"{sorted(SUPPORTED_REPORT_SCHEMA_VERSIONS)}."
            )
        if v not in SUPPORTED_REPORT_SCHEMA_VERSIONS:
            raise ValueError(
                f"Unsupported report_schema_version: {v}. "
                f"Supported versions: {sorted(SUPPORTED_REPORT_SCHEMA_VERSIONS)}."
            )
        return v


class ReportValidator:
    """Validates serialized execution report data structures, JSON strings, and files."""

    @classmethod
    def validate_dict(cls, data: dict[str, Any], source_label: str = "report") -> ReportModel:
        """Validate an in-memory dictionary against the report contract schema and semantic rules."""
        if not isinstance(data, dict):
            raise ReportValidationError(
                f"Invalid report format in '{source_label}': expected JSON object (dictionary), got {type(data).__name__}."
            )

        sanitized_data = redact_sensitive_data(data)

        # 1. Strict report schema version check
        schema_version = sanitized_data.get("report_schema_version")
        if schema_version is None:
            raise ReportValidationError(
                f"Missing required field 'report_schema_version' in '{source_label}'. "
                f"Supported versions: {sorted(SUPPORTED_REPORT_SCHEMA_VERSIONS)}."
            )
        if type(schema_version) is not int or isinstance(schema_version, bool):
            raise ReportValidationError(
                f"Invalid report_schema_version type in '{source_label}'. "
                f"report_schema_version must be an integer, supported versions: "
                f"{sorted(SUPPORTED_REPORT_SCHEMA_VERSIONS)}."
            )
        if schema_version not in SUPPORTED_REPORT_SCHEMA_VERSIONS:
            raise ReportValidationError(
                f"Unsupported report_schema_version: {schema_version} in '{source_label}'. "
                f"Supported versions: {sorted(SUPPORTED_REPORT_SCHEMA_VERSIONS)}."
            )

        try:
            model = ReportModel.model_validate(sanitized_data)
        except ValidationError as val_err:
            formatted_errors = cls._format_validation_errors(val_err)
            raise ReportValidationError(
                f"Report schema validation failed for '{source_label}'",
                errors=formatted_errors,
            ) from val_err

        if schema_version == 1:
            cls._validate_v1(sanitized_data, model, source_label=source_label)
        elif schema_version == 2:
            cls._validate_v2(sanitized_data, model, source_label=source_label)

        return model

    @classmethod
    def _validate_v1(cls, data: dict[str, Any], model: ReportModel, source_label: str) -> None:
        """Validate Schema v1 report constraints and semantics."""
        errors: list[str] = []

        # Check raw input dictionary for forbidden v2 exclusive fields in v1
        outcome_raw = data.get("outcome", {})
        if isinstance(outcome_raw, dict):
            v2_outcome_fields = [
                "behavioral_verdict", "overall_outcome", "side_effect_status",
                "primary_infrastructure_status", "reconciliation_infrastructure_status",
                "execution_finality",
            ]
            for f in v2_outcome_fields:
                if f in outcome_raw and outcome_raw[f] is not None:
                    errors.append(f"Schema v1 report cannot contain Schema v2 outcome field '{f}'.")

        side_raw = data.get("side_effects", {})
        if isinstance(side_raw, dict):
            if "uncertain_count" in side_raw and side_raw["uncertain_count"] != 0:
                errors.append("Schema v1 report cannot contain non-zero 'uncertain_count'.")
            if "bounds" in side_raw and side_raw["bounds"] != [0, 0] and side_raw["bounds"] != [model.side_effects.actual_count, model.side_effects.actual_count]:
                b = side_raw["bounds"]
                if len(b) == 2 and b[0] != b[1]:
                    errors.append(f"Schema v1 report cannot contain non-scalar bounds: {b}.")

        meta_raw = data.get("metadata", {})
        if isinstance(meta_raw, dict):
            if "unresolved_workers" in meta_raw and meta_raw["unresolved_workers"] != 0:
                errors.append("Schema v1 report cannot contain non-zero 'unresolved_workers'.")

        if data.get("supplemental_reconciliation") is not None:
            errors.append("Schema v1 report cannot contain 'supplemental_reconciliation'.")

        if len(data.get("late_events", [])) > 0:
            errors.append("Schema v1 report cannot contain 'late_events'.")

        for idx, ev in enumerate(data.get("events", [])):
            if isinstance(ev, dict):
                for ef in ("uncertainty_reason", "reconciliation_status", "writer_id", "event_sequence"):
                    if ev.get(ef) is not None:
                        errors.append(f"Event #{idx+1} in Schema v1 report contains forbidden v2 field '{ef}'.")
                cap_prov = ev.get("capacity_provenance")
                if cap_prov is not None and cap_prov != "TIER_C1":
                    errors.append(f"Event #{idx+1} in Schema v1 report contains active capacity_provenance '{cap_prov}'.")

        if errors:
            raise ReportValidationError(
                f"Schema v1 validation failed for '{source_label}'",
                errors=errors,
            )

        # Run full v1 semantic consistency checks
        cls._validate_semantics(model, source_label=source_label)

    @classmethod
    def _validate_v2(cls, data: dict[str, Any], model: ReportModel, source_label: str) -> None:
        """Validate Schema v2 report constraints and semantics."""
        errors: list[str] = []

        outcome = model.outcome
        # 1. Required v2 outcome fields
        if outcome.behavioral_verdict is None or outcome.behavioral_verdict not in VALID_BEHAVIORAL_VERDICTS:
            errors.append(
                f"outcome.behavioral_verdict is required in Schema v2 and must be one of {sorted(VALID_BEHAVIORAL_VERDICTS)}."
            )
        if outcome.side_effect_status is None or outcome.side_effect_status not in VALID_EXTERNAL_STATUSES:
            errors.append(
                f"outcome.side_effect_status is required in Schema v2 and must be one of {sorted(VALID_EXTERNAL_STATUSES)}."
            )
        if outcome.primary_infrastructure_status is None or outcome.primary_infrastructure_status not in VALID_PRIMARY_INFRASTRUCTURE_STATUSES:
            errors.append(
                f"outcome.primary_infrastructure_status is required in Schema v2 and must be one of {sorted(VALID_PRIMARY_INFRASTRUCTURE_STATUSES)}."
            )
        if outcome.reconciliation_infrastructure_status is not None and outcome.reconciliation_infrastructure_status not in VALID_RECONCILIATION_INFRASTRUCTURE_STATUSES:
            errors.append(
                f"outcome.reconciliation_infrastructure_status must be one of {sorted(VALID_RECONCILIATION_INFRASTRUCTURE_STATUSES)}."
            )
        if outcome.overall_outcome is None or outcome.overall_outcome not in SUPPORTED_VERDICTS:
            errors.append(
                f"outcome.overall_outcome is required in Schema v2 and must be one of {sorted(SUPPORTED_VERDICTS)}."
            )
        if outcome.execution_finality is None or outcome.execution_finality not in VALID_EXECUTION_FINALITIES:
            errors.append(
                f"outcome.execution_finality is required in Schema v2 and must be one of {sorted(VALID_EXECUTION_FINALITIES)}."
            )

        if outcome.overall_outcome is not None and outcome.verdict != outcome.overall_outcome:
            errors.append(
                f"outcome.verdict ('{outcome.verdict}') must match outcome.overall_outcome ('{outcome.overall_outcome}')."
            )

        # 2. Side effects bounds check
        if len(model.side_effects.bounds) != 2:
            errors.append(
                f"side_effects.bounds must be a 2-element list [S_min, S_max], got {model.side_effects.bounds}."
            )

        # 3. Primary infrastructure containment preservation (Invariant 6)
        if outcome.primary_infrastructure_status in ("THREAD_LEAKED", "PROCESS_TERMINATED", "INTEGRITY_VIOLATION"):
            if outcome.overall_outcome != "INFRASTRUCTURE_ERROR":
                errors.append(
                    f"Overall outcome must be INFRASTRUCTURE_ERROR when primary_infrastructure_status is '{outcome.primary_infrastructure_status}'."
                )

        if outcome.reconciliation_infrastructure_status == "RECONCILIATION_FAILED":
            if outcome.overall_outcome != "INFRASTRUCTURE_ERROR":
                errors.append(
                    "Overall outcome must be INFRASTRUCTURE_ERROR when reconciliation_infrastructure_status is 'RECONCILIATION_FAILED'."
                )

        # 4. Supplemental reconciliation verification
        if model.supplemental_reconciliation is not None:
            sr = model.supplemental_reconciliation
            if isinstance(sr, dict):
                if not sr.get("reconciliation_id") or not str(sr.get("reconciliation_id")).strip():
                    errors.append("supplemental_reconciliation.reconciliation_id cannot be empty.")
                parent_hash = sr.get("parent_report_sha256", "")
                if not parent_hash or len(str(parent_hash)) != 64:
                    errors.append("supplemental_reconciliation.parent_report_sha256 must be a 64-character SHA-256 hash.")
            elif isinstance(sr, SupplementalReconciliationReport):
                if not sr.reconciliation_id.strip():
                    errors.append("supplemental_reconciliation.reconciliation_id cannot be empty.")
                if len(sr.parent_report_sha256) != 64:
                    errors.append("supplemental_reconciliation.parent_report_sha256 must be a 64-character SHA-256 hash.")

        if errors:
            raise ReportValidationError(
                f"Schema v2 validation failed for '{source_label}'",
                errors=errors,
            )

        # Basic structural and identifier checks
        if not model.run_id or not model.run_id.strip():
            errors.append("Field 'run_id' cannot be empty or whitespace.")
        if not model.scenario.id or not model.scenario.id.strip():
            errors.append("Field 'scenario.id' cannot be empty or whitespace.")
        if model.metadata.total_events != len(model.events):
            errors.append(
                f"metadata.total_events ({model.metadata.total_events}) conflicts with event count ({len(model.events)})."
            )
        if model.fault_injection.injected_fault_count != len(model.fault_injection.injected_faults):
            errors.append(
                f"fault_injection.injected_fault_count ({model.fault_injection.injected_fault_count}) conflicts with injected_faults ({len(model.fault_injection.injected_faults)})."
            )

        if errors:
            raise ReportValidationError(
                f"Schema v2 semantic validation failed for '{source_label}'",
                errors=errors,
            )

    @classmethod
    def validate_json(cls, json_str: str, source_label: str = "inline") -> ReportModel:
        """Parse and validate a JSON string as an execution report."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as jde:
            raise ReportValidationError(
                f"Failed to parse report JSON in '{source_label}': {jde.msg} (line {jde.lineno}, column {jde.colno})"
            ) from jde

        return cls.validate_dict(data, source_label=source_label)

    @classmethod
    def validate_file(cls, file_path: str | Path) -> ReportModel:
        """Read and validate a report JSON file from disk."""
        path = Path(file_path)
        if not path.exists():
            raise ConfigurationError(f"Report file '{path}' does not exist.")
        if not path.is_file():
            raise ConfigurationError(f"Report path '{path}' is a directory, not a file.")

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as oe:
            raise ConfigurationError(f"Cannot read report file '{path}': {oe}") from oe

        return cls.validate_json(content, source_label=path.name)

    @classmethod
    def _validate_semantics(cls, model: ReportModel, source_label: str) -> None:
        """Validate logical consistency across outcome, evaluation, and side-effects."""
        errors: list[str] = []

        # 1. Non-empty identifiers
        if not model.run_id or not model.run_id.strip():
            errors.append("Field 'run_id' cannot be empty or whitespace.")

        if not model.scenario.id or not model.scenario.id.strip():
            errors.append("Field 'scenario.id' cannot be empty or whitespace.")

        # 2. Scenario schema version check
        if model.scenario.schema_version not in SUPPORTED_SCENARIO_SCHEMA_VERSIONS:
            errors.append(
                f"Unsupported scenario schema_version: {model.scenario.schema_version}. "
                f"Supported versions: {sorted(SUPPORTED_SCENARIO_SCHEMA_VERSIONS)}."
            )

        # 3. Execution success consistency with verdict
        if model.outcome.verdict == "INFRASTRUCTURE_ERROR":
            if model.outcome.execution_success is not False:
                errors.append(
                    "Outcome 'execution_success' must be False when verdict is INFRASTRUCTURE_ERROR."
                )
        else:
            if model.outcome.execution_success is not True:
                errors.append(
                    f"Outcome 'execution_success' must be True when verdict is '{model.outcome.verdict}'."
                )

        # 4. Outcome consistency with evaluation
        if model.outcome.recovery_success != model.evaluation.passed:
            errors.append(
                f"Outcome 'recovery_success' ({model.outcome.recovery_success}) conflicts with "
                f"evaluation.passed ({model.evaluation.passed})."
            )

        if model.outcome.verdict != model.evaluation.verdict:
            errors.append(
                f"Outcome 'verdict' ({model.outcome.verdict}) conflicts with "
                f"evaluation.verdict ({model.evaluation.verdict})."
            )

        if model.outcome.recovery_success is True and model.outcome.verdict != "PASS":
            errors.append(
                f"Outcome 'recovery_success' cannot be True when verdict is '{model.outcome.verdict}'."
            )

        if model.outcome.recovery_success is False and model.outcome.verdict == "PASS":
            errors.append(
                "Outcome 'recovery_success' cannot be False when verdict is PASS."
            )

        # 5. Validation failure and runtime failure cannot both be true
        if model.outcome.validation_failure and model.outcome.runtime_failure:
            errors.append(
                "Outcome 'validation_failure' and 'runtime_failure' cannot both be True."
            )

        if model.outcome.verdict != "INFRASTRUCTURE_ERROR":
            if model.outcome.validation_failure or model.outcome.runtime_failure:
                errors.append(
                    f"Validation and runtime failures must be False when verdict is '{model.outcome.verdict}'."
                )
        else:
            if not (model.outcome.validation_failure or model.outcome.runtime_failure):
                errors.append(
                    "Either 'validation_failure' or 'runtime_failure' must be True when verdict is INFRASTRUCTURE_ERROR."
                )

        # 6. Authoritative side effect safety classification consistency
        side_assertion = next((a for a in model.evaluation.assertions if a.name == "side_effect_count"), None)
        has_incomplete_identity = any(
            e.commit_status == CommitStatus.COMMITTED.value and (
                e.effect_id is None or not e.effect_id.strip() or
                e.operation_id is None or not e.operation_id.strip()
            )
            for e in model.events
        )
        expected_safety = determine_side_effect_safety(
            actual_count=model.side_effects.actual_count,
            duplicate_count=model.side_effects.duplicate_count,
            expected_count=model.side_effects.expected_count,
            verdict=model.outcome.verdict,
            has_events=len(model.events) > 0,
            side_effect_assertion_passed=side_assertion.passed if side_assertion else None,
            has_incomplete_identity=has_incomplete_identity,
        )

        if model.side_effects.safety_status != expected_safety:
            errors.append(
                f"Contradictory side_effects.safety_status '{model.side_effects.safety_status}'. "
                f"Based on evidence (actual={model.side_effects.actual_count}, "
                f"expected={model.side_effects.expected_count}, "
                f"duplicates={model.side_effects.duplicate_count}, "
                f"verdict='{model.outcome.verdict}'), safety_status must be '{expected_safety}'."
            )

        if model.outcome.side_effect_safety != model.side_effects.safety_status:
            errors.append(
                f"Outcome 'side_effect_safety' ({model.outcome.side_effect_safety}) conflicts with "
                f"side_effects.safety_status ({model.side_effects.safety_status})."
            )

        expected_safe_bool = (model.side_effects.safety_status == SideEffectSafety.VERIFIED_SAFE)
        if model.side_effects.safe != expected_safe_bool:
            errors.append(
                f"side_effects.safe ({model.side_effects.safe}) must be {expected_safe_bool} "
                f"when safety_status is {model.side_effects.safety_status}."
            )

        if model.outcome.side_effect_safe != model.side_effects.safe:
            errors.append(
                f"Outcome 'side_effect_safe' ({model.outcome.side_effect_safe}) conflicts with "
                f"side_effects.safe ({model.side_effects.safe})."
            )

        # 7. Side effect counts agreement between side_effects and evaluation
        if model.side_effects.actual_count != model.evaluation.actual_side_effects:
            errors.append(
                f"side_effects.actual_count ({model.side_effects.actual_count}) conflicts with "
                f"evaluation.actual_side_effects ({model.evaluation.actual_side_effects})."
            )

        if model.side_effects.duplicate_count != model.evaluation.duplicate_effect_count:
            errors.append(
                f"side_effects.duplicate_count ({model.side_effects.duplicate_count}) conflicts with "
                f"evaluation.duplicate_effect_count ({model.evaluation.duplicate_effect_count})."
            )

        if model.side_effects.expected_count != model.evaluation.expected_side_effects:
            errors.append(
                f"side_effects.expected_count ({model.side_effects.expected_count}) conflicts with "
                f"evaluation.expected_side_effects ({model.evaluation.expected_side_effects})."
            )

        # 8. Fault injection count agreement
        if model.fault_injection.injected_fault_count != len(model.fault_injection.injected_faults):
            errors.append(
                f"fault_injection.injected_fault_count ({model.fault_injection.injected_fault_count}) "
                f"conflicts with count of injected_faults ({len(model.fault_injection.injected_faults)})."
            )

        # 9. Total events agreement
        if model.metadata.total_events != len(model.events):
            errors.append(
                f"metadata.total_events ({model.metadata.total_events}) conflicts with "
                f"event count ({len(model.events)})."
            )

        # 10. Ledger event evidence consistency with side-effects summary
        committed_events = [e for e in model.events if e.commit_status == CommitStatus.COMMITTED.value]

        # Check committed event identity evidence
        missing_op_committed = [
            e for e in committed_events if not e.operation_id or not e.operation_id.strip()
        ]
        if missing_op_committed:
            # Missing identity evidence must never allow a report to be classified as VERIFIED_SAFE
            if (
                model.side_effects.safety_status == SideEffectSafety.VERIFIED_SAFE
                or model.outcome.side_effect_safety == SideEffectSafety.VERIFIED_SAFE
                or model.side_effects.safe is True
                or model.outcome.side_effect_safe is True
            ):
                errors.append(
                    "Missing identity evidence on COMMITTED event prevents VERIFIED_SAFE classification; "
                    "safety must be UNVERIFIED."
                )

            # Duplicate verification requires operation_id:
            # - when multiple committed events occur
            # - when retries occurred (len(events) > 1 or max_retries_observed > 0)
            # - in multi-operation scenarios (not is_single_operation)
            # - when asserting clean execution or passing verdict
            duplicate_verification_required = (
                len(committed_events) > 1
                or len(model.events) > 1
                or not model.scenario.is_single_operation
                or model.outcome.verdict == "PASS"
                or model.evaluation.verdict == "PASS"
                or model.side_effects.duplicate_count == 0
            )
            if duplicate_verification_required:
                errors.append(
                    f"COMMITTED event #{missing_op_committed[0].event_id} has missing or empty operation_id "
                    f"when duplicate verification requires it to establish duplicate-effect relationships."
                )

        # Derive unique committed effect IDs strictly from COMMITTED events with non-empty effect_id
        derived_committed_effect_ids: list[str] = []
        for e in committed_events:
            if e.effect_id and e.effect_id.strip():
                eff_id = e.effect_id.strip()
                if eff_id not in derived_committed_effect_ids:
                    derived_committed_effect_ids.append(eff_id)

        derived_actual_count = len(derived_committed_effect_ids)
        derived_committed_set = set(derived_committed_effect_ids)
        reported_committed_set = set(model.side_effects.committed_effect_ids)

        # Check for duplicate entries within reported committed_effect_ids list
        if len(model.side_effects.committed_effect_ids) != len(reported_committed_set):
            errors.append(
                f"side_effects.committed_effect_ids contains duplicate identifiers: "
                f"{model.side_effects.committed_effect_ids}."
            )

        # Check 1: actual_count matches unique committed effects derived from ledger events
        if model.side_effects.actual_count != derived_actual_count:
            if model.side_effects.actual_count > 0 and len(committed_events) == 0:
                errors.append(
                    f"side_effects.actual_count is {model.side_effects.actual_count}, but no ledger events "
                    f"have commit_status == 'COMMITTED'."
                )
            else:
                errors.append(
                    f"side_effects.actual_count ({model.side_effects.actual_count}) conflicts with "
                    f"count of unique committed effects ({derived_actual_count}) derived from ledger events."
                )

        # Check 2: committed_effect_ids matches unique committed effect IDs derived from ledger events
        if reported_committed_set != derived_committed_set:
            errors.append(
                f"side_effects.committed_effect_ids ({sorted(reported_committed_set)}) does not match "
                f"committed effect IDs derived from COMMITTED ledger events ({sorted(derived_committed_set)})."
            )

        # Check 3: Non-committed events consistency
        # Non-committed events (NOT_APPLICABLE, UNCOMMITTED, ABORTED, etc.) must NOT establish side effects
        for e in model.events:
            if e.commit_status != "COMMITTED" and e.effect_id and e.effect_id.strip():
                non_committed_eff = e.effect_id.strip()
                if non_committed_eff not in derived_committed_set and non_committed_eff in reported_committed_set:
                    errors.append(
                        f"Non-committed ledger event #{e.event_id} (commit_status='{e.commit_status}', "
                        f"effect_id='{non_committed_eff}') cannot be counted as a committed effect."
                    )

        # Check 4: Duplicate effect IDs and duplicate count consistency derived from ledger events
        if missing_op_committed:
            errors.append(
                f"Cannot establish complete duplicate verification evidence: "
                f"{len(missing_op_committed)} COMMITTED event(s) lack operation_id "
                f"(Event IDs: {[e.event_id for e in missing_op_committed]}). "
                "Committed events must not be silently excluded from duplicate calculations."
            )
        else:
            op_to_effects: dict[str, set[str]] = {}
            for e in committed_events:
                if e.effect_id and e.effect_id.strip():
                    eff_id = e.effect_id.strip()
                    assert e.operation_id is not None
                    op_id = e.operation_id.strip()
                    op_to_effects.setdefault(op_id, set()).add(eff_id)

            derived_duplicate_count = sum(
                len(effs) - 1 for effs in op_to_effects.values() if len(effs) > 1
            )

            if model.side_effects.duplicate_count != derived_duplicate_count:
                errors.append(
                    f"side_effects.duplicate_count ({model.side_effects.duplicate_count}) conflicts with "
                    f"duplicate count ({derived_duplicate_count}) derived from ledger events."
                )

        if errors:
            raise ReportValidationError(
                f"Semantic validation failed for '{source_label}'",
                errors=errors,
            )

    @staticmethod
    def _format_validation_errors(val_err: ValidationError) -> list[str]:
        """Convert Pydantic validation errors into readable strings."""
        formatted: list[str] = []
        for err in val_err.errors():
            loc = ".".join(str(elem) for elem in err["loc"])
            msg = err["msg"]
            formatted.append(f"[{loc}]: {msg}")
        return formatted
