"""Pydantic data models for RecoveryBench events, evaluations, and runs."""

import hashlib
import json
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ExecutionStatus(StrEnum):
    """Status of the tool execution attempt."""

    SUCCESS = "SUCCESS"
    FAULT_INJECTED = "FAULT_INJECTED"
    ERROR = "ERROR"


class CommitStatus(StrEnum):
    """State of the underlying side effect mutation."""

    COMMITTED = "COMMITTED"
    ABORTED = "ABORTED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class EvaluationVerdict(StrEnum):
    """High-level classification of test evaluation."""

    PASS = "PASS"
    GENUINE_FAILURE = "GENUINE_FAILURE"
    INFRASTRUCTURE_ERROR = "INFRASTRUCTURE_ERROR"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


def hash_token(value: str | None) -> str | None:
    """Generate a SHA-256 hash of an idempotency key or identifier for privacy.

    Returns the first 16 characters of the hex digest, or None if input is empty.
    """
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def compute_request_fingerprint(tool_name: str, action_type: str, payload: dict[str, Any]) -> str:
    """Compute a canonical SHA-256 fingerprint for a request payload.

    Ignores non-business metadata like request IDs or dynamic retry counts.
    """
    # Canonicalize dictionary with sorted keys
    cleaned_payload = {
        k: v
        for k, v in payload.items()
        if k not in ("idempotency_key", "request_id", "operation_id", "attempt_id")
    }
    serialized = json.dumps(
        {"tool": tool_name, "action": action_type, "payload": cleaned_payload},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class LedgerEvent(BaseModel):
    """An immutable event recorded in the append-only Effect Ledger."""

    event_id: int | None = Field(default=None, description="Monotonic sequence number")
    run_id: str = Field(description="Unique ID of the test run")
    scenario_id: str = Field(description="Identifier of scenario under test")
    tool_name: str = Field(description="Name of the tool called")
    action_type: str = Field(description="Action executed, e.g. charge, refund")
    operation_id: str | None = Field(default=None, description="Logical business operation identifier")
    attempt_id: str | None = Field(default=None, description="Unique invocation attempt identifier")
    effect_id: str | None = Field(default=None, description="Ground-truth persistent mutation identifier")
    idempotency_key_hash: str | None = Field(default=None, description="SHA-256 hash of idempotency key")
    request_fingerprint: str = Field(description="Deterministic fingerprint of business arguments")
    attempt_number: int = Field(ge=1, description="1-based attempt sequence number")
    execution_status: ExecutionStatus = Field(description="Result of invocation")
    commit_status: CommitStatus = Field(description="Whether side effect persisted")
    simulated_timestamp: str = Field(description="ISO-8601 simulated timestamp")
    error_type: str | None = Field(default=None, description="Name of injected or raised error")
    evidence_reference: str | None = Field(default=None, description="Transaction ID or receipt ref")
    details: dict[str, Any] = Field(default_factory=dict, description="Sanitized metadata")


class EvaluationAssertion(BaseModel):
    """Result of a single deterministic assertion rule."""

    name: str = Field(description="Name of assertion")
    passed: bool = Field(description="True if assertion passed")
    expected: Any = Field(description="Expected value or condition")
    actual: Any = Field(description="Actual observed value")
    message: str = Field(description="Explanation of verdict")
    evidence: list[str] = Field(default_factory=list, description="Concrete ledger evidence supporting this verdict")
    event_ids: list[int] = Field(default_factory=list, description="IDs of ledger events cited as evidence")


class EvaluationResult(BaseModel):
    """Comprehensive recovery evaluation report."""

    verdict: EvaluationVerdict = Field(description="Overall verdict classification")
    passed: bool = Field(description="True if all assertions passed")
    assertions: list[EvaluationAssertion] = Field(default_factory=list)
    expected_side_effects: int | None = Field(default=1)
    actual_side_effects: int = Field(default=0)
    duplicate_effect_count: int = Field(default=0)
    idempotency_key_stable: bool = Field(default=True)
    max_retries_observed: int = Field(default=0)
    duration_ms: float = Field(default=0.0)
    failure_reasons: list[str] = Field(default_factory=list)
    evidence_summary: dict[str, list[str]] = Field(
        default_factory=dict, description="Summary of evidence cited across all assertions"
    )
    isolation_mode: str = Field(
        default="trusted",
        description="Execution isolation mode: 'isolated' (Docker container) or 'trusted' (in-process)",
    )


class RunSummary(BaseModel):
    """Summary of an executed test run."""

    run_id: str
    scenario_id: str
    agent_id: str
    passed: bool
    verdict: EvaluationVerdict
    total_events: int
    committed_effects: int
    duplicate_effects: int
    created_at: str
    duration_ms: float
    isolation_mode: str = Field(
        default="trusted",
        description="Execution isolation mode: 'isolated' (Docker container) or 'trusted' (in-process)",
    )
