"""Pydantic data models for RecoveryBench events, evaluations, and runs."""

import hashlib
import hmac
import json
import math
import secrets
import uuid
from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from recoverybench.errors import EvaluationInputValidationError, IdempotencyCorrelationError


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
    UNCERTAIN = "UNCERTAIN"


class CapacityProvenanceTier(StrEnum):
    """Provenance authority tier for tool side-effect capacity declarations."""

    TIER_C1 = "TIER_C1"  # Developer-Declared (@mock_tool(max_effects=1)); zero completeness authority
    TIER_C2 = "TIER_C2"  # Service-Documented (API docs / SLA); advisory only
    TIER_C3 = "TIER_C3"  # Runtime-Enforced (DB unique constraint / schema); structurally constrained
    TIER_C4 = "TIER_C4"  # Independently Verified (Out-of-band audit / WAL verification); authoritative ground truth


class InventoryStatus(StrEnum):
    """Status of side-effect inventory completeness for an invocation attempt."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class ToolExecutionModel(StrEnum):
    """Execution model declared and enforced for tool handlers."""

    COROUTINE = "COROUTINE"              # Native async def coroutine
    COOPERATIVE_SYNC = "COOPERATIVE_SYNC"  # Periodically inspects cancellation token
    BOUNDED_SYNC = "BOUNDED_SYNC"        # Declared socket/I/O timeout <= declared_timeout_seconds
    UNCOOPERATIVE = "UNCOOPERATIVE"      # Blocking I/O or loop without timeout guarantees
    UNCOOPERATIVE_SYNC = UNCOOPERATIVE   # Exact alias for ADR-004 Section 6.1 line 282


class AbsentStatus(StrEnum):
    """Affirmative proof of side-effect absence."""

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNVERIFIED = "UNVERIFIED"


class IdempotencyGuarantee(StrEnum):
    """Contractual idempotency level declared by a tool."""

    NONE = "NONE"
    BEST_EFFORT = "BEST_EFFORT"
    AT_LEAST_ONCE_DEDUPLICATED = "AT_LEAST_ONCE_DEDUPLICATED"
    STRICT_EXACTLY_ONCE = "STRICT_EXACTLY_ONCE"


class IdempotencyScope(StrEnum):
    """Scope of idempotency deduplication."""

    NONE = "NONE"
    OPERATION_AND_ACTION = "OPERATION_AND_ACTION"
    REQUEST_FINGERPRINT = "REQUEST_FINGERPRINT"
    GLOBAL = "GLOBAL"


class ReplayBehavior(StrEnum):
    """Behavior of tool upon detecting an idempotent replay."""

    EXECUTE_NEW = "EXECUTE_NEW"
    RETURN_CACHED_RESULT = "RETURN_CACHED_RESULT"
    REJECT_DUPLICATE = "REJECT_DUPLICATE"


class IdempotencyContract(BaseModel):
    """Contractual and verified idempotency behavior for tool operations."""

    guarantee: IdempotencyGuarantee = Field(default=IdempotencyGuarantee.NONE)
    scope: IdempotencyScope = Field(default=IdempotencyScope.OPERATION_AND_ACTION)
    replay_behavior: ReplayBehavior = Field(default=ReplayBehavior.EXECUTE_NEW)
    retention_ttl_seconds: float = Field(default=300.0)
    verified_service_deduplication: bool = Field(
        default=False,
        description="True ONLY if verified by Tier C3/C4 structural or out-of-band evidence",
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_contract(cls, data: Any) -> Any:
        if isinstance(data, dict):
            ttl = data.get("retention_ttl_seconds", 300.0)
            if isinstance(ttl, bool) or not isinstance(ttl, int | float):
                raise IdempotencyCorrelationError(
                    f"Invalid retention_ttl_seconds type: {type(ttl).__name__}"
                )
            if ttl < 0:
                raise IdempotencyCorrelationError(
                    f"retention_ttl_seconds cannot be negative, got {ttl}"
                )
        return data


class SideEffectBounds(BaseModel):
    """Mathematical lower and upper bounds [S_min, S_max] on true external side effects.

    S_min in N_0
    S_max in N_0 U {infinity}
    0 <= S_min <= S_max
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    s_min: int = Field(description="Exact non-negative integer lower bound")
    s_max: int | float = Field(description="Exact non-negative integer or math.inf upper bound")

    def __init__(self, s_min: Any = 0, s_max: Any = 0, **data: Any) -> None:
        if "s_min" in data:
            s_min = data["s_min"]
        if "s_max" in data:
            s_max = data["s_max"]

        # Validate s_min: must be exact non-negative int
        if isinstance(s_min, bool) or not isinstance(s_min, int):
            raise EvaluationInputValidationError(
                f"s_min must be an exact non-negative int, got {type(s_min).__name__}: {s_min!r}"
            )
        if s_min < 0:
            raise EvaluationInputValidationError(f"s_min cannot be negative: {s_min}")

        # Validate s_max: exact non-negative int or math.inf
        if isinstance(s_max, bool):
            raise EvaluationInputValidationError(
                f"s_max must be an exact non-negative int or math.inf, got bool: {s_max!r}"
            )
        if s_max is math.inf or (isinstance(s_max, float) and math.isinf(s_max) and s_max > 0) or s_max == "infinity":
            s_max_val: int | float = math.inf
        elif isinstance(s_max, float):
            raise EvaluationInputValidationError(
                f"s_max cannot be a finite float ({s_max}); must be an exact int or math.inf"
            )
        elif isinstance(s_max, int):
            if s_max < 0:
                raise EvaluationInputValidationError(f"s_max cannot be negative: {s_max}")
            s_max_val = s_max
        else:
            raise EvaluationInputValidationError(
                f"s_max must be an exact int or math.inf, got {type(s_max).__name__}: {s_max!r}"
            )

        if not math.isinf(s_max_val) and s_min > s_max_val:
            raise EvaluationInputValidationError(
                f"Malformed bounds: s_min ({s_min}) cannot exceed s_max ({s_max_val})"
            )

        super().__init__(s_min=s_min, s_max=s_max_val)

    @model_validator(mode="before")
    @classmethod
    def _validate_bounds(cls, data: Any) -> Any:
        if isinstance(data, dict):
            s_min = data.get("s_min")
            s_max = data.get("s_max")
        elif isinstance(data, tuple | list) and len(data) == 2:
            s_min, s_max = data
            data = {"s_min": s_min, "s_max": s_max}
        else:
            return data

        # Validate s_min: must be exact non-negative int
        if isinstance(s_min, bool) or not isinstance(s_min, int):
            raise EvaluationInputValidationError(
                f"s_min must be an exact non-negative int, got {type(s_min).__name__}: {s_min!r}"
            )
        if s_min < 0:
            raise EvaluationInputValidationError(f"s_min cannot be negative: {s_min}")

        # Validate s_max: exact non-negative int or math.inf
        if isinstance(s_max, bool):
            raise EvaluationInputValidationError(
                f"s_max must be an exact non-negative int or math.inf, got bool: {s_max!r}"
            )
        if s_max is math.inf or (isinstance(s_max, float) and math.isinf(s_max) and s_max > 0) or s_max == "infinity":
            s_max = math.inf
        elif isinstance(s_max, float):
            raise EvaluationInputValidationError(
                f"s_max cannot be a finite float ({s_max}); must be an exact int or math.inf"
            )
        elif isinstance(s_max, int):
            if s_max < 0:
                raise EvaluationInputValidationError(f"s_max cannot be negative: {s_max}")
        else:
            raise EvaluationInputValidationError(
                f"s_max must be an exact int or math.inf, got {type(s_max).__name__}: {s_max!r}"
            )

        if not math.isinf(s_max) and s_min > s_max:
            raise EvaluationInputValidationError(
                f"Malformed bounds: s_min ({s_min}) cannot exceed s_max ({s_max})"
            )

        data["s_min"] = s_min
        data["s_max"] = s_max
        return data

    @property
    def is_exact(self) -> bool:
        """True if bounds represent an exact known scalar (S_min == S_max < infinity)."""
        return not math.isinf(self.s_max) and self.s_min == self.s_max

    @property
    def is_zero(self) -> bool:
        """True if proven exactly zero mutations [0, 0]."""
        return self.is_exact and self.s_min == 0

    @property
    def is_unbounded(self) -> bool:
        """True if upper bound is infinite [S_min, infinity)."""
        return math.isinf(self.s_max)

    def contains(self, value: Any) -> bool:
        """Return True iff value in [S_min, S_max]. Rejects non-exact-ints and bools."""
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        if value < self.s_min:
            return False
        if self.is_unbounded:
            return True
        return value <= self.s_max

    def to_list(self) -> list[int | str]:
        """Convert to Schema-compatible list format, e.g. [1, 2] or [0, 'infinity']."""
        return [self.s_min, "infinity" if self.is_unbounded else int(self.s_max)]

    def to_tuple(self) -> tuple[int, int | float]:
        return (self.s_min, self.s_max)

    def __iter__(self) -> Any:
        return iter((self.s_min, self.s_max))

    def __repr__(self) -> str:
        upper_str = "infinity" if self.is_unbounded else str(self.s_max)
        return f"SideEffectBounds([{self.s_min}, {upper_str}])"


class AttemptStateVector(BaseModel):
    """Accounting state vector for a single tool invocation attempt a in A."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    attempt_id: str = Field(description="Globally unique invocation attempt identifier")
    operation_id: str | None = Field(default=None, description="Logical operation ID")
    tool_name: str = Field(default="unknown_tool", description="Tool service name")
    action: str = Field(default="execute", description="Action name executed")
    declared_provenance: CapacityProvenanceTier = Field(
        default=CapacityProvenanceTier.TIER_C1,
        description="Provenance tier declared in tool code or event metadata",
    )
    verified_provenance: CapacityProvenanceTier = Field(
        default=CapacityProvenanceTier.TIER_C1,
        description="Authoritatively verified capacity provenance established across trust boundary",
    )
    capacity_provenance: CapacityProvenanceTier = Field(
        default=CapacityProvenanceTier.TIER_C1,
        description="Effective verified capacity provenance",
    )
    max_capacity: int | float = Field(default=math.inf, description="M(a) in N_0 U {infinity}")
    inventory_status: InventoryStatus = Field(default=InventoryStatus.UNKNOWN)
    absent_status: AbsentStatus = Field(default=AbsentStatus.UNVERIFIED)
    verified_effects: set[str] = Field(default_factory=set, description="E_verified(a)")
    unresolved_capacity: int | float = Field(default=0, description="u(a) in N_0 U {infinity}")
    request_fingerprint: str = Field(default="", description="Canonical request fingerprint")
    idempotency_key_hash: str | None = Field(default=None, description="Hashed idempotency key")
    emitted_timestamp: float | None = Field(default=None, description="Numeric timestamp of attempt")
    idempotency_contract: IdempotencyContract = Field(default_factory=IdempotencyContract)
    verified_service_deduplication: bool = Field(default=False)

    @property
    def verified_effect_count(self) -> int:
        """c(a) = |E_verified(a)|."""
        return len(self.verified_effects)


class HarnessRuntimeAttestation(BaseModel):
    """Immutable, harness-authenticated attestation of runtime capacity enforcement for Tier C3."""

    run_id: str = Field(description="Run ID certified by the harness runtime enforcement")
    tool_name: str = Field(description="Name of the tool certified by the harness")
    max_capacity: int = Field(default=1, description="Enforced max side-effect capacity")
    runtime_guarantee: str = Field(
        default="RUNTIME_UNIQUE_CONSTRAINT",
        description="Type of structural constraint enforced at runtime",
    )
    signature: str = Field(description="Cryptographic HMAC proof issued by harness runtime authority")


class ReconciliationAuditEvidence(BaseModel):
    """Harness-controlled out-of-band audit evidence establishing Tier C4 ground truth."""

    run_id: str = Field(description="Specific run audited")
    scenario_id: str = Field(description="Scenario audited")
    snapshot_id: str = Field(description="Reconciliation snapshot or canonical ledger digest")
    attempt_ids: list[str] = Field(
        default_factory=list, description="Specific invocation attempts covered by audit"
    )
    verified_effects: set[str] = Field(
        default_factory=set, description="Set of verified mutation effect IDs"
    )
    confirmed_absent: bool = Field(
        default=False, description="Affirmative proof of complete absence of mutations"
    )
    drain_delay_verified: bool = Field(
        default=True, description="Whether post-cutoff drain delay was verified"
    )
    audit_session_id: str = Field(description="Audit session identifier")
    signature: str = Field(description="Cryptographic HMAC proof issued by harness audit authority")


class AuthoritativeFinalityProof(BaseModel):
    """Proof submitted by an authoritative orchestrator asserting execution finality."""

    run_id: str = Field(description="Run ID for which finality is asserted")
    orchestrator_id: str = Field(
        default="RecoveryBenchOrchestrator", description="Identifier of authoritative orchestrator"
    )
    finality: str = Field(default="FINAL", description="Target finality asserted")
    evaluation_digest: str | None = Field(
        default=None, description="Cryptographic digest of evaluation state if bound to state"
    )
    signature: str = Field(description="HMAC proof issued by authoritative orchestrator")


class LateOutcomeRecord(BaseModel):
    """Durable late evidence record emitted by thread hooks, process supervisors, or reconciliation engines."""

    event_id: str = Field(description="Globally unique UUID assigned by originating writer")
    run_id: str = Field(description="Foreign key to primary run execution")
    operation_id: str = Field(description="Business operation identifier")
    attempt_id: str = Field(description="Invocation attempt identifier")
    writer_id: str = Field(description="THREAD_HOOK | PROCESS_SUPERVISOR | RECON_ENGINE")
    event_sequence: int = Field(ge=1, description="Monotonic sequence number per (writer_id, attempt_id)")
    event_type: str = Field(description="LATE_COMPLETION | AUDIT_SNAPSHOT | PROCESS_DEATH | DRAIN_TIMEOUT")
    payload_fingerprint: str = Field(description="SHA-256 canonical hash of normalized event payload")
    emitted_timestamp: str = Field(description="ISO-8601 writer emission timestamp")
    arrival_timestamp: str = Field(description="ISO-8601 local ingestion timestamp (excluded from fingerprint)")
    payload: dict[str, Any] = Field(default_factory=dict, description="Normalized event payload")

    @staticmethod
    def compute_payload_fingerprint(payload: dict[str, Any]) -> str:
        """Compute canonical SHA-256 hash of normalized payload with sorted keys."""
        normalized_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(normalized_json.encode("utf-8")).hexdigest()


class HarnessRuntimeAuthority:
    """Authority responsible for issuing and verifying Tier C3 runtime enforcement attestations.

    Signing authority belongs exclusively to the harness runtime enforcement component.
    """

    def __init__(self, secret_key: bytes | None = None) -> None:
        self._secret_key: bytes = secret_key or secrets.token_bytes(32)

    def attest(
        self,
        run_id: str,
        tool_name: str,
        max_capacity: int = 1,
        runtime_guarantee: str = "RUNTIME_UNIQUE_CONSTRAINT",
    ) -> HarnessRuntimeAttestation:
        if not run_id or not isinstance(run_id, str):
            raise ValueError("run_id must be a non-empty string.")
        if not tool_name or not isinstance(tool_name, str):
            raise ValueError("tool_name must be a non-empty string.")
        payload = f"C3:{run_id.strip()}:{tool_name.strip()}:{max_capacity}:{runtime_guarantee}".encode()
        sig = hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()
        return HarnessRuntimeAttestation(
            run_id=run_id.strip(),
            tool_name=tool_name.strip(),
            max_capacity=max_capacity,
            runtime_guarantee=runtime_guarantee,
            signature=sig,
        )

    def verify(
        self,
        attestation: Any,
        for_run_id: str | None = None,
        for_tool_name: str | None = None,
        max_capacity: int | None = None,
    ) -> bool:
        if not isinstance(attestation, HarnessRuntimeAttestation):
            return False
        if for_run_id is not None and attestation.run_id != for_run_id.strip():
            return False
        if for_tool_name is not None and attestation.tool_name != for_tool_name.strip():
            return False
        if max_capacity is not None and attestation.max_capacity != max_capacity:
            return False
        payload = f"C3:{attestation.run_id}:{attestation.tool_name}:{attestation.max_capacity}:{attestation.runtime_guarantee}".encode()
        expected = hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(attestation.signature, expected)


class HarnessAuditAuthority:
    """Authority responsible for issuing and verifying Tier C4 independent audit evidence.

    The audit authority is separate from ordinary tool and evaluator callers.
    """

    def __init__(self, secret_key: bytes | None = None) -> None:
        self._secret_key: bytes = secret_key or secrets.token_bytes(32)

    def issue_audit_evidence(
        self,
        run_id: str,
        scenario_id: str,
        snapshot_id: str,
        attempt_ids: list[str] | set[str],
        verified_effects: set[str] | list[str] | None = None,
        confirmed_absent: bool = False,
        drain_delay_verified: bool = True,
        audit_session_id: str | None = None,
    ) -> ReconciliationAuditEvidence:
        if not run_id or not isinstance(run_id, str):
            raise ValueError("run_id must be a non-empty string.")
        if not scenario_id or not isinstance(scenario_id, str):
            raise ValueError("scenario_id must be a non-empty string.")
        if not snapshot_id or not isinstance(snapshot_id, str):
            raise ValueError("snapshot_id must be a non-empty string.")

        sess_id = audit_session_id or f"audit_sess_{uuid.uuid4().hex[:8]}"
        attempts_list = sorted(set(attempt_ids))
        attempts_str = ",".join(attempts_list)
        effects_set = set(verified_effects) if verified_effects else set()
        effects_str = ",".join(sorted(effects_set))

        payload = (
            f"C4:{run_id.strip()}:{scenario_id.strip()}:{snapshot_id.strip()}:{attempts_str}:"
            f"{effects_str}:{confirmed_absent}:{drain_delay_verified}:{sess_id}"
        ).encode()
        sig = hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()
        return ReconciliationAuditEvidence(
            run_id=run_id.strip(),
            scenario_id=scenario_id.strip(),
            snapshot_id=snapshot_id.strip(),
            attempt_ids=attempts_list,
            verified_effects=effects_set,
            confirmed_absent=confirmed_absent,
            drain_delay_verified=drain_delay_verified,
            audit_session_id=sess_id,
            signature=sig,
        )

    def verify(
        self,
        evidence: Any,
        for_run_id: str | None = None,
        for_scenario_id: str | None = None,
        for_snapshot_id: str | None = None,
        for_attempt_id: str | None = None,
    ) -> bool:
        if not isinstance(evidence, ReconciliationAuditEvidence):
            return False
        if for_run_id is not None and evidence.run_id != for_run_id.strip():
            return False
        if for_scenario_id is not None and evidence.scenario_id != for_scenario_id.strip():
            return False
        if for_snapshot_id is not None and evidence.snapshot_id != for_snapshot_id.strip():
            return False
        if for_attempt_id is not None and for_attempt_id.strip() not in evidence.attempt_ids:
            return False

        attempts_str = ",".join(sorted(set(evidence.attempt_ids)))
        effects_str = ",".join(sorted(evidence.verified_effects))
        payload = (
            f"C4:{evidence.run_id}:{evidence.scenario_id}:{evidence.snapshot_id}:{attempts_str}:"
            f"{effects_str}:{evidence.confirmed_absent}:{evidence.drain_delay_verified}:{evidence.audit_session_id}"
        ).encode()
        expected = hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(evidence.signature, expected)


class HarnessOrchestratorAuthority:
    """Authority responsible for issuing and verifying AuthoritativeFinalityProof.

    Proof must be genuinely issued by the trusted orchestrator and bound to the exact run
    and evaluation digest.
    """

    def __init__(self, secret_key: bytes | None = None) -> None:
        self._secret_key: bytes = secret_key or secrets.token_bytes(32)

    def issue_finality_proof(
        self,
        run_id: str,
        evaluation_digest: str,
        orchestrator_id: str = "RecoveryBenchOrchestrator",
    ) -> AuthoritativeFinalityProof:
        if not run_id or not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string.")
        if not evaluation_digest or not isinstance(evaluation_digest, str) or not evaluation_digest.strip():
            raise ValueError("evaluation_digest must be a non-empty string.")
        digest_part = evaluation_digest.strip()
        payload = f"FINALITY:{run_id.strip()}:{orchestrator_id.strip()}:FINAL:{digest_part}".encode()
        sig = hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()
        return AuthoritativeFinalityProof(
            run_id=run_id.strip(),
            orchestrator_id=orchestrator_id.strip(),
            finality="FINAL",
            evaluation_digest=digest_part,
            signature=sig,
        )

    def verify(
        self,
        proof: Any,
        for_run_id: str | None = None,
        for_evaluation_digest: str | None = None,
    ) -> bool:
        if not isinstance(proof, AuthoritativeFinalityProof):
            return False
        if for_run_id is not None and proof.run_id != for_run_id.strip():
            return False
        if for_evaluation_digest is not None:
            if not proof.evaluation_digest or proof.evaluation_digest != for_evaluation_digest.strip():
                return False
        elif not proof.evaluation_digest:
            return False
        digest_part = proof.evaluation_digest
        payload = f"FINALITY:{proof.run_id}:{proof.orchestrator_id}:{proof.finality}:{digest_part}".encode()
        expected = hmac.new(self._secret_key, payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(proof.signature, expected)


def compute_evaluation_digest(
    run_id: str,
    events: Sequence[Any],
    expected_side_effects: int | None = None,
    expect_agent_success: bool = True,
    agent_status: str | None = "SUCCEEDED",
    attempts: Sequence[Any] | dict[str, Any] | None = None,
    expected_effect_ids: Sequence[str] | set[str] | None = None,
    infrastructure_status: str | None = None,
    reconciliation_infrastructure_status: str | None = None,
    reconciliation_status: str | None = None,
    reconciliation_audit: Any | None = None,
    fingerprints: Sequence[str] | dict[str, Any] | None = None,
    verified_effects: Sequence[str] | set[str] | None = None,
    capacity_bounds: Any | None = None,
) -> str:
    """Compute a canonical, deterministic SHA-256 digest of EVERY evaluation-affecting input.

    Covers:
    - Execution identification: run_id
    - Ledger events: event_id, event_type, timestamp, tool_name, action, operation_id,
      attempt_id, commit_status, execution_status, effect_id, idempotency_key,
      evidence_reference, and request_fingerprint.
    - Attempts: AttemptStateVector attributes (attempt_id, operation_id, tool_name, action,
      declared_provenance, verified_provenance, capacity_provenance, max_capacity,
      inventory_status, absent_status, verified_effects, unresolved_capacity,
      request_fingerprint, idempotency_key_hash, verified_service_deduplication)
      or canonical summaries derived from events.
    - Effects: expected_side_effects, expected_effect_ids, verified effect sets.
    - Capacity bounds: [S_min, S_max] or canonical bound representations.
    - Fingerprints: request fingerprints, payload hashes, event fingerprints.
    - Infrastructure and reconciliation evidence:
      primary_infrastructure_status, reconciliation_infrastructure_status,
      reconciliation_status, and full reconciliation_audit evidence.
    - Behavioral expectations and outcomes: expect_agent_success, agent_status.
    """
    clean_run_id = (run_id or "").strip()
    status_str = str(agent_status) if agent_status is not None else "SUCCEEDED"
    expect_success_bool = bool(expect_agent_success)

    exp_side_effects = (
        expected_side_effects
        if (expected_side_effects is not None and not isinstance(expected_side_effects, bool))
        else None
    )

    exp_effect_ids = sorted(list(expected_effect_ids)) if expected_effect_ids else None

    # Canonical verified effects
    if verified_effects is not None:
        canonical_verified_effects = sorted(list(verified_effects))
    else:
        canonical_verified_effects = sorted(
            list(
                {
                    str(e.effect_id)
                    for e in events
                    if (
                        getattr(e, "commit_status", None) == CommitStatus.COMMITTED
                        or (
                            hasattr(getattr(e, "commit_status", None), "value")
                            and e.commit_status.value == CommitStatus.COMMITTED.value
                        )
                    )
                    and getattr(e, "effect_id", None)
                }
            )
        )

    # Canonical capacity bounds
    bounds_canonical = None
    if capacity_bounds is not None:
        s_min = getattr(
            capacity_bounds,
            "s_min",
            capacity_bounds[0] if isinstance(capacity_bounds, (tuple, list)) else None,
        )
        s_max = getattr(
            capacity_bounds,
            "s_max",
            capacity_bounds[1] if isinstance(capacity_bounds, (tuple, list)) and len(capacity_bounds) > 1 else None,
        )
        if s_max is not None and isinstance(s_max, float) and math.isinf(s_max):
            s_max = "infinity"
        bounds_canonical = {"s_min": s_min, "s_max": s_max}

    infra_val = (
        infrastructure_status.value
        if hasattr(infrastructure_status, "value")
        else str(infrastructure_status)
    ) if infrastructure_status is not None else None

    recon_infra_val = (
        reconciliation_infrastructure_status.value
        if hasattr(reconciliation_infrastructure_status, "value")
        else str(reconciliation_infrastructure_status)
    ) if reconciliation_infrastructure_status is not None else None

    recon_stat_val = (
        reconciliation_status.value
        if hasattr(reconciliation_status, "value")
        else str(reconciliation_status)
    ) if reconciliation_status is not None else None

    # Canonical audit dictionary
    audit_dict = None
    if reconciliation_audit is not None:
        raw_effs = getattr(reconciliation_audit, "verified_effects", []) or []
        audit_dict = {
            "audit_id": getattr(reconciliation_audit, "audit_id", None),
            "run_id": getattr(reconciliation_audit, "run_id", None),
            "snapshot_id": getattr(reconciliation_audit, "snapshot_id", None),
            "attempt_ids": sorted(list(getattr(reconciliation_audit, "attempt_ids", []) or [])),
            "verified_effects": sorted(list(raw_effs)),
            "confirmed_absent": bool(getattr(reconciliation_audit, "confirmed_absent", False)),
            "drain_delay_verified": bool(getattr(reconciliation_audit, "drain_delay_verified", True)),
            "audit_session_id": getattr(reconciliation_audit, "audit_session_id", None),
            "pending_writes": getattr(reconciliation_audit, "pending_writes", None),
            "capacity_exhausted": getattr(reconciliation_audit, "capacity_exhausted", None),
            "reconciliation_status": str(getattr(reconciliation_audit, "reconciliation_status", "")),
            "signature": getattr(reconciliation_audit, "signature", None),
        }

    all_fingerprints: set[str] = set()

    # Canonical events
    events_canonical = []
    for e in events:
        c_stat = getattr(e, "commit_status", None)
        c_val = (c_stat.value if hasattr(c_stat, "value") else str(c_stat)) if c_stat is not None else None

        ex_stat = getattr(e, "execution_status", None)
        ex_val = (ex_stat.value if hasattr(ex_stat, "value") else str(ex_stat)) if ex_stat is not None else None

        fp = getattr(e, "request_fingerprint", None) or ""
        if fp:
            all_fingerprints.add(str(fp))

        act = getattr(e, "action_type", None) or getattr(e, "action", "") or ""
        ts = getattr(e, "simulated_timestamp", None) or getattr(e, "timestamp", "") or ""
        ev_type = getattr(e, "event_type", "") or ""

        event_dict = {
            "event_id": getattr(e, "event_id", None),
            "event_type": str(ev_type),
            "timestamp": str(ts),
            "tool_name": str(getattr(e, "tool_name", "") or ""),
            "action": str(act),
            "operation_id": getattr(e, "operation_id", None),
            "attempt_id": getattr(e, "attempt_id", None),
            "commit_status": c_val,
            "execution_status": ex_val,
            "effect_id": getattr(e, "effect_id", None),
            "idempotency_key_hash": getattr(e, "idempotency_key_hash", None) or getattr(e, "idempotency_key", None),
            "evidence_reference": getattr(e, "evidence_reference", None),
            "fingerprint": str(fp),
        }
        events_canonical.append(event_dict)

    # Canonical attempts
    attempts_canonical = []
    if attempts is not None:
        if isinstance(attempts, dict):
            raw_attempts = list(attempts.values())
        else:
            raw_attempts = list(attempts)

        for a in sorted(raw_attempts, key=lambda item: str(getattr(item, "attempt_id", ""))):
            max_cap = getattr(a, "max_capacity", None)
            if max_cap is not None and isinstance(max_cap, float) and math.isinf(max_cap):
                max_cap = "infinity"

            unres_cap = getattr(a, "unresolved_capacity", None)
            if unres_cap is not None and isinstance(unres_cap, float) and math.isinf(unres_cap):
                unres_cap = "infinity"

            afp = getattr(a, "request_fingerprint", None) or ""
            if afp:
                all_fingerprints.add(str(afp))

            a_dict = {
                "attempt_id": str(getattr(a, "attempt_id", "") or ""),
                "operation_id": getattr(a, "operation_id", None),
                "tool_name": str(getattr(a, "tool_name", "") or ""),
                "action": str(getattr(a, "action", "") or ""),
                "declared_provenance": str(getattr(a, "declared_provenance", "")),
                "verified_provenance": str(getattr(a, "verified_provenance", "")),
                "capacity_provenance": str(getattr(a, "capacity_provenance", "")),
                "max_capacity": max_cap,
                "inventory_status": str(getattr(a, "inventory_status", "")),
                "absent_status": str(getattr(a, "absent_status", "")),
                "verified_effects": sorted(list(getattr(a, "verified_effects", []) or [])),
                "unresolved_capacity": unres_cap,
                "request_fingerprint": str(afp),
                "idempotency_key_hash": getattr(a, "idempotency_key_hash", None),
                "verified_service_deduplication": bool(getattr(a, "verified_service_deduplication", False)),
            }
            attempts_canonical.append(a_dict)
    else:
        # Group by attempt_id from events
        attempts_map: dict[str, dict[str, Any]] = {}
        for e in events:
            aid = getattr(e, "attempt_id", None) or f"synthetic_attempt_{getattr(e, 'event_id', 0)}"
            if aid not in attempts_map:
                attempts_map[aid] = {
                    "attempt_id": aid,
                    "tool_name": str(getattr(e, "tool_name", "") or ""),
                    "action": str(getattr(e, "action_type", None) or getattr(e, "action", "") or ""),
                    "operation_id": getattr(e, "operation_id", None),
                    "event_ids": [],
                    "commit_statuses": [],
                    "effect_ids": [],
                    "fingerprints": [],
                }
            attempts_map[aid]["event_ids"].append(getattr(e, "event_id", None))
            c_stat = getattr(e, "commit_status", None)
            attempts_map[aid]["commit_statuses"].append(
                (c_stat.value if hasattr(c_stat, "value") else str(c_stat)) if c_stat is not None else None
            )
            eff = getattr(e, "effect_id", None)
            if eff:
                attempts_map[aid]["effect_ids"].append(eff)
            efp = getattr(e, "request_fingerprint", None)
            if efp:
                attempts_map[aid]["fingerprints"].append(efp)

        for aid in sorted(attempts_map.keys()):
            attempts_canonical.append(attempts_map[aid])

    # Canonical fingerprints
    if fingerprints is not None:
        if isinstance(fingerprints, dict):
            for v in fingerprints.values():
                if v:
                    all_fingerprints.add(str(v))
        else:
            for item in fingerprints:
                if item:
                    all_fingerprints.add(str(item))

    canonical_obj = {
        "run_id": clean_run_id,
        "agent_status": status_str,
        "expect_agent_success": expect_success_bool,
        "expected_side_effects": exp_side_effects,
        "expected_effect_ids": exp_effect_ids,
        "verified_effects": canonical_verified_effects,
        "capacity_bounds": bounds_canonical,
        "infrastructure_status": infra_val,
        "reconciliation_infrastructure_status": recon_infra_val,
        "reconciliation_status": recon_stat_val,
        "reconciliation_audit": audit_dict,
        "events": events_canonical,
        "attempts": attempts_canonical,
        "fingerprints": sorted(list(all_fingerprints)),
    }

    dumped = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()



class PrimaryInfrastructureStatus(StrEnum):
    """Status of primary worker containment and test harness integrity."""

    CLEAN = "CLEAN"
    WORKER_TIMED_OUT = "WORKER_TIMED_OUT"
    THREAD_LEAKED = "THREAD_LEAKED"
    PROCESS_TERMINATED = "PROCESS_TERMINATED"
    INTEGRITY_VIOLATION = "INTEGRITY_VIOLATION"


class ReconciliationInfrastructureStatus(StrEnum):
    """Status of post-cutoff out-of-band reconciliation audit."""

    CLEAN = "CLEAN"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"


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
    execution_finality: str = Field(
        default="FINAL",
        description="Execution finality: 'FINAL' or 'PROVISIONAL'",
    )
    isolation_mode: str = Field(
        default="trusted",
        description="Execution isolation mode: 'isolated' (Docker container) or 'trusted' (in-process)",
    )
    behavioral_verdict: str | None = Field(default=None, description="Behavioral classification")
    side_effect_status: str | None = Field(default=None, description="External side-effect status")
    primary_infrastructure_status: str | None = Field(default=None, description="Primary containment status")
    reconciliation_infrastructure_status: str | None = Field(default=None, description="Reconciliation status")
    overall_outcome: str | None = Field(default=None, description="Overall run outcome")
    bounds: list[int | str] | None = Field(default=None, description="Two-layer accounting effect bounds")
    uncertain_count: int = Field(default=0, description="Count of uncertain side effects")
    unresolved_workers: int = Field(default=0, description="Count of unresolved tool workers")


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
