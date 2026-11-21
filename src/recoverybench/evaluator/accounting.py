"""Two-layer side-effect accounting engine, capacity provenance enforcement, and safe idempotency correlation.

Implements Sections 1.1-1.6, 2, and 3.1-3.6 of ADR-004-SAFETY-ARCHITECTURE-CORRECTED.
"""

import math
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from recoverybench.errors import (
    CapacityBreachIntegrityError,
)
from recoverybench.models import (
    AbsentStatus,
    AttemptStateVector,
    CapacityProvenanceTier,
    CommitStatus,
    ExecutionStatus,
    HarnessAuditAuthority,
    HarnessRuntimeAttestation,
    HarnessRuntimeAuthority,
    IdempotencyContract,
    InventoryStatus,
    LedgerEvent,
    PrimaryInfrastructureStatus,
    ReconciliationAuditEvidence,
    SideEffectBounds,
)
from recoverybench.tools.base import ToolRegistry


def derive_external_status(
    bounds: SideEffectBounds,
    expected_side_effects: int | None,
    contradictory_evidence: bool = False,
    has_idempotency_violation: bool = False,
    duplicate_count: int = 0,
) -> str:
    """Deterministically derive external side-effect status from calculated bounds and expectation.

    Supported classifications:
    - EXACT_MATCH: Proven exact side-effect count equals expected positive count.
    - CONFIRMED_ABSENT: Proven zero mutations [0, 0] when 0 expected (or unspecified).
    - DUPLICATE_COMMITTED: Proven side effects strictly exceed expectation (or > 0 when 0 expected),
      or multiple mutations occurred under mutated idempotency keys / expired TTL.
    - MISSING_COMMITS: S_max < expected (or [0, 0] when expected > 0).
    - UNRESOLVED_INTERVAL: Finite non-degenerate interval bracketing/overlapping expectation.
    - UNBOUNDED_AMBIGUITY: Upper bound is infinite [S_min, infinity).
    - CONTRADICTORY_EVIDENCE: Explicit contradictory client/server evidence.
    """
    if contradictory_evidence:
        return "CONTRADICTORY_EVIDENCE"

    if has_idempotency_violation:
        return "DUPLICATE_COMMITTED"

    if bounds.is_unbounded:
        return "UNBOUNDED_AMBIGUITY"

    if expected_side_effects == 0:
        if bounds.s_min == 0 and bounds.s_max == 0:
            return "CONFIRMED_ABSENT"
        if bounds.s_min > 0:
            return "DUPLICATE_COMMITTED"
        return "UNRESOLVED_INTERVAL"

    if expected_side_effects is None:
        if bounds.is_exact:
            return "CONFIRMED_ABSENT" if bounds.s_min == 0 else "EXACT_MATCH"
        return "UNRESOLVED_INTERVAL"

    # expected_side_effects > 0
    if bounds.is_exact:
        if bounds.s_min == expected_side_effects:
            return "EXACT_MATCH"
        if bounds.s_min > expected_side_effects:
            return "DUPLICATE_COMMITTED"
        return "MISSING_COMMITS"

    # Finite non-degenerate interval (s_min < s_max)
    if bounds.s_min > expected_side_effects:
        return "DUPLICATE_COMMITTED"
    if bounds.s_max < expected_side_effects:
        return "MISSING_COMMITS"

    return "UNRESOLVED_INTERVAL"


class AccountingResult(BaseModel):
    """Authoritative result of two-layer side-effect accounting."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    bounds: SideEffectBounds
    external_status: str
    verified_effects: set[str]
    attempts: dict[str, AttemptStateVector]
    unresolved_by_attempt: dict[str, int | float]
    equivalence_classes: dict[str, list[str]]
    unresolved_by_class: dict[str, int | float]
    tightening_applied: dict[str, bool]
    capacity_breached: bool = False
    breached_attempt_id: str | None = None
    infrastructure_status: str = PrimaryInfrastructureStatus.CLEAN.value
    duplicate_count: int = 0
    missing_identity_events: list[int] = Field(default_factory=list)
    contradictory_evidence: bool = False

    @property
    def uncertain_count(self) -> int:
        """Count of unresolved/uncertain attempts or remaining uncertain capacity."""
        return sum(1 for v in self.unresolved_by_attempt.values() if v > 0)


class TwoLayerEffectAccountingEngine:
    """Evaluates two-layer side-effect accounting, capacity provenance, and safe idempotency correlation.

    Layer 1: Unique invocation attempts A (keyed by attempt_id).
    Layer 2: Ground-truth effects set E_verified = Union_a E_verified(a).
    """

    @classmethod
    def calculate(
        cls,
        events: list[LedgerEvent],
        tool_registry: ToolRegistry | None = None,
        expected_side_effects: int | None = None,
        tool_metadata: dict[str, Any] | None = None,
        contradictory_evidence: bool = False,
        raise_on_breach: bool = False,
        reconciliation_audit: ReconciliationAuditEvidence | None = None,
        runtime_authority: HarnessRuntimeAuthority | None = None,
        audit_authority: HarnessAuditAuthority | None = None,
    ) -> AccountingResult:
        """Calculate side-effect bounds [S_min, S_max] and derive external status.

        Args:
            events: Append-only ledger events from primary execution.
            tool_registry: Optional registry of active tool services.
            expected_side_effects: Expected persistent mutation count.
            tool_metadata: Optional dictionary of per-tool capacity/idempotency metadata.
            contradictory_evidence: Whether explicit contradictory client/server evidence exists.
            raise_on_breach: If True, raises CapacityBreachIntegrityError on c(a) > M(a).
            reconciliation_audit: Optional out-of-band audit evidence for Tier C4.
            runtime_authority: Optional harness runtime authority for verifying Tier C3 attestations.
            audit_authority: Optional harness audit authority for verifying Tier C4 evidence.
        """
        tool_meta = tool_metadata or {}
        missing_identity_events: list[int] = []

        # Trust Boundary Enforcement:
        # Authorities must be passed directly by the trusted harness caller.
        # Developer-controlled or caller-supplied tool_metadata dictionaries are STRICTLY FORBIDDEN
        # from injecting or overriding signing/verification authorities.
        effective_runtime_auth = runtime_authority
        effective_audit_auth = audit_authority

        # 1. Layer 1 Partitioning by attempt_id
        attempts_events: dict[str, list[LedgerEvent]] = {}
        for idx, e in enumerate(events):
            raw_aid = e.attempt_id
            if raw_aid and raw_aid.strip():
                aid = raw_aid.strip()
            else:
                aid = f"synthetic_attempt_{e.event_id if e.event_id is not None else idx}"
                if e.event_id is not None:
                    missing_identity_events.append(e.event_id)
            attempts_events.setdefault(aid, []).append(e)

        # 2. Process each attempt: capacity provenance, inventory, absence, u(a)
        attempts: dict[str, AttemptStateVector] = {}
        unresolved_by_attempt: dict[str, int | float] = {}
        capacity_breached = False
        breached_attempt_id: str | None = None

        for aid, a_evts in attempts_events.items():
            first_evt = a_evts[0]
            tool_name = first_evt.tool_name or "unknown_tool"
            action = first_evt.action_type or "execute"
            op_id = (
                first_evt.operation_id.strip()
                if (first_evt.operation_id and first_evt.operation_id.strip())
                else None
            )

            # Resolve tool contract & capacity configuration
            tool_service = tool_registry.get(tool_name) if tool_registry else None
            meta_for_tool = tool_meta.get(tool_name, {})
            if not meta_for_tool and any(
                k in tool_meta
                for k in (
                    "runtime_enforced",
                    "c3_enforced",
                    "verified_provenance",
                    "verified_audit",
                    "max_side_effect_capacity",
                )
            ):
                meta_for_tool = tool_meta

            # Resolve declared capacity provenance tier (untrusted developer/event claim)
            raw_declared = (
                first_evt.details.get("capacity_provenance")
                or meta_for_tool.get("declared_provenance")
                or meta_for_tool.get("capacity_provenance")
                or getattr(tool_service, "declared_provenance", None)
                or getattr(tool_service, "capacity_provenance", None)
            )
            if raw_declared is not None:
                if isinstance(raw_declared, str):
                    try:
                        declared_prov = CapacityProvenanceTier(raw_declared)
                    except ValueError:
                        declared_prov = CapacityProvenanceTier.TIER_C1
                elif isinstance(raw_declared, CapacityProvenanceTier):
                    declared_prov = raw_declared
                else:
                    declared_prov = CapacityProvenanceTier.TIER_C1
            else:
                declared_prov = CapacityProvenanceTier.TIER_C1

            # Trust Boundary Enforcement:
            # A developer declaration in Python code (@mock_tool) or event details
            # (e.g. details={"capacity_provenance": "TIER_C3"} or "TIER_C4") has ZERO authority.
            # Verified provenance defaults strictly to TIER_C1.
            verified_prov = CapacityProvenanceTier.TIER_C1

            # 1. Tier C3 (Runtime-Enforced Constraints) Trust Boundary:
            # Established ONLY by a valid, harness-issued HarnessRuntimeAttestation on the tool service
            # or in tool_metadata, verified by the harness runtime authority.
            # An ordinary ToolService, spoofed tool name (e.g. "payment_gateway"), ordinary service
            # attributes (is_runtime_enforced), developer enums (verified_provenance=TIER_C3), ordinary
            # metadata dictionaries claiming C3, or event details claiming C3 are STRICTLY UNTRUSTED
            # and remain TIER_C1.
            c3_attestation: HarnessRuntimeAttestation | None = None
            raw_att = None
            if tool_service is not None:
                raw_att = getattr(tool_service, "harness_attestation", None)

            if raw_att is None and isinstance(meta_for_tool, dict):
                raw_att = meta_for_tool.get("harness_attestation")

            if isinstance(raw_att, HarnessRuntimeAttestation):
                run_id_for_att = first_evt.run_id if first_evt.run_id else None
                if effective_runtime_auth is not None and effective_runtime_auth.verify(
                    raw_att,
                    for_run_id=run_id_for_att,
                    for_tool_name=tool_name,
                    max_capacity=raw_att.max_capacity,
                ):
                    c3_attestation = raw_att

            if c3_attestation is not None:
                verified_prov = CapacityProvenanceTier.TIER_C3

            # 2. Tier C4 (Independently Verified Evidence / Audit) Trust Boundary:
            # Established ONLY by a valid ReconciliationAuditEvidence issued by the harness audit authority.
            # Unauthenticated writer_id ("RECON_ENGINE"), event booleans (audit_verified, drain_delay_verified,
            # reconciliation_complete), or caller dictionary metadata are STRICTLY UNTRUSTED and left at C1/C2.
            valid_c4_audit: ReconciliationAuditEvidence | None = None
            raw_audit = reconciliation_audit
            if raw_audit is None and isinstance(meta_for_tool, dict):
                raw_audit = meta_for_tool.get("reconciliation_audit") or meta_for_tool.get("harness_audit")

            if isinstance(raw_audit, ReconciliationAuditEvidence):
                run_id_for_att = first_evt.run_id if first_evt.run_id else None
                scenario_id_for_att = first_evt.scenario_id if first_evt.scenario_id else None
                if effective_audit_auth is not None and effective_audit_auth.verify(
                    raw_audit,
                    for_run_id=run_id_for_att,
                    for_scenario_id=scenario_id_for_att,
                    for_attempt_id=aid,
                ):
                    valid_c4_audit = raw_audit

            if valid_c4_audit is not None:
                verified_prov = CapacityProvenanceTier.TIER_C4

            # Effective provenance is verified_prov
            prov = verified_prov

            # Resolve max capacity M(a)
            # Under Tier C1 or C2, capacity claims are unverified: M(a) = math.inf
            if prov in (CapacityProvenanceTier.TIER_C1, CapacityProvenanceTier.TIER_C2):
                max_capacity = math.inf
            elif c3_attestation is not None:
                max_capacity = c3_attestation.max_capacity
            elif valid_c4_audit is not None:
                max_capacity = len(valid_c4_audit.verified_effects) if not valid_c4_audit.confirmed_absent else 0
            else:
                max_capacity = math.inf

            # Collect Layer 1 verified effects for this attempt: E_verified(a)
            verified_effects_a: set[str] = set()
            for e in a_evts:
                if (
                    e.commit_status == CommitStatus.COMMITTED
                    and e.effect_id
                    and e.effect_id.strip()
                ):
                    verified_effects_a.add(e.effect_id.strip())

            c_a = len(verified_effects_a)

            # Determine absence status: Absent(a)
            # Under Tier C1 or Tier C2, a developer declaration or boolean flag (e.g. mutates=False)
            # has zero completeness authority and CANNOT establish AbsentStatus.TRUE.
            # Must be backed by Tier C3 (structural constraint) or Tier C4 (independent verification).
            has_trusted_absence = (
                (valid_c4_audit is not None and valid_c4_audit.confirmed_absent)
                or (
                    prov in (CapacityProvenanceTier.TIER_C3, CapacityProvenanceTier.TIER_C4)
                    and any(
                        e.details.get("absent_status") == "TRUE"
                        or e.details.get("verified_absence") is True
                        for e in a_evts
                    )
                )
            )
            if c_a == 0 and has_trusted_absence and prov in (
                CapacityProvenanceTier.TIER_C3,
                CapacityProvenanceTier.TIER_C4,
            ):
                absent_status = AbsentStatus.TRUE
            elif c_a > 0:
                absent_status = AbsentStatus.FALSE
            else:
                absent_status = AbsentStatus.UNVERIFIED

            # Determine authoritative inventory completeness: InventoryStatus(a)
            # Condition 2a: Verified receipt with Tier C3/C4 contract
            has_receipt = any(
                e.details.get("receipt_complete") is True
                or (
                    (bool(e.evidence_reference and e.evidence_reference.strip()) or bool(e.effect_id and e.effect_id.strip()))
                    and e.commit_status == CommitStatus.COMMITTED
                    and c_a == max_capacity
                )
                for e in a_evts
            )
            receipt_complete = has_receipt and prov in (
                CapacityProvenanceTier.TIER_C3,
                CapacityProvenanceTier.TIER_C4,
            )

            # Condition 2b: Exact bounded saturation with Tier C3/C4
            bounded_saturation = (
                prov in (CapacityProvenanceTier.TIER_C3, CapacityProvenanceTier.TIER_C4)
                and max_capacity < math.inf
                and c_a == max_capacity
            )

            # Condition 2c: Authoritative Tier 4 external reconciliation audit
            # Established ONLY by valid harness-issued ReconciliationAuditEvidence.
            audit_complete = valid_c4_audit is not None

            # Deduplicated idempotent replay successfully returning cached result without mutations,
            # verified under Tier C3/C4
            is_successful_dedup_replay = (
                c_a == 0
                and prov in (CapacityProvenanceTier.TIER_C3, CapacityProvenanceTier.TIER_C4)
                and any(
                    e.commit_status == CommitStatus.NOT_APPLICABLE
                    and e.execution_status == ExecutionStatus.SUCCESS
                    and (e.details.get("idempotent_replay") is True or bool(e.effect_id))
                    for e in a_evts
                )
            )

            # Verified clean execution of a non-mutating/read-only action (NOT_APPLICABLE).
            # Under Tier C1 or C2, NOT_APPLICABLE + SUCCESS CANNOT establish completeness!
            # Must be backed by verified Tier C3/C4 runtime contract.
            is_verified_read_only = (
                c_a == 0
                and prov in (CapacityProvenanceTier.TIER_C3, CapacityProvenanceTier.TIER_C4)
                and any(
                    e.commit_status == CommitStatus.NOT_APPLICABLE
                    and e.execution_status == ExecutionStatus.SUCCESS
                    for e in a_evts
                )
            )

            if (
                receipt_complete
                or bounded_saturation
                or audit_complete
                or is_successful_dedup_replay
                or is_verified_read_only
            ):
                inventory_status = InventoryStatus.COMPLETE
            elif c_a > 0:
                inventory_status = InventoryStatus.PARTIAL
            else:
                inventory_status = InventoryStatus.UNKNOWN

            # Check for Capacity Breach: c(a) > M(a)
            if max_capacity < math.inf and c_a > max_capacity:
                capacity_breached = True
                breached_attempt_id = aid
                # Invalidate capacity bound to infinity per Section 3.4
                max_capacity = math.inf
                u_a: int | float = math.inf
            else:
                # Calculate unresolved remaining capacity u(a) per Section 3.3
                if absent_status == AbsentStatus.TRUE or inventory_status == InventoryStatus.COMPLETE:
                    u_a = 0
                elif max_capacity == math.inf or prov in (
                    CapacityProvenanceTier.TIER_C1,
                    CapacityProvenanceTier.TIER_C2,
                ):
                    u_a = math.inf
                elif prov in (CapacityProvenanceTier.TIER_C3, CapacityProvenanceTier.TIER_C4):
                    u_a = max(0, max_capacity - c_a)
                else:
                    u_a = math.inf

            # Timestamp parsing
            timestamp_val: float | None = None
            raw_ts = first_evt.details.get("timestamp") or first_evt.simulated_timestamp
            if isinstance(raw_ts, int | float) and not isinstance(raw_ts, bool):
                timestamp_val = float(raw_ts)
            elif isinstance(raw_ts, str):
                try:
                    timestamp_val = float(raw_ts)
                except ValueError:
                    try:
                        timestamp_val = datetime.fromisoformat(raw_ts).timestamp()
                    except Exception:
                        timestamp_val = None

            # Resolve idempotency contract & verified service deduplication
            # A developer supplying a boolean verified_service_deduplication=True on Tier C1/C2
            # does NOT establish independent verification. It MUST be backed by Tier C3/C4.
            raw_contract = (
                first_evt.details.get("idempotency_contract")
                or meta_for_tool.get("idempotency_contract")
                or (getattr(tool_service, "idempotency_contract", None))
            )
            if isinstance(raw_contract, IdempotencyContract):
                attempt_contract = raw_contract
            elif isinstance(raw_contract, dict):
                attempt_contract = IdempotencyContract.model_validate(raw_contract)
            else:
                attempt_contract = IdempotencyContract()

            raw_dedup_claim = (
                attempt_contract.verified_service_deduplication is True
                or first_evt.details.get("verified_service_deduplication") is True
            )
            verified_dedup = raw_dedup_claim and prov in (
                CapacityProvenanceTier.TIER_C3,
                CapacityProvenanceTier.TIER_C4,
            )

            state_vec = AttemptStateVector(
                attempt_id=aid,
                operation_id=op_id,
                tool_name=tool_name,
                action=action,
                declared_provenance=declared_prov,
                verified_provenance=verified_prov,
                capacity_provenance=prov,
                max_capacity=max_capacity,
                inventory_status=inventory_status,
                absent_status=absent_status,
                verified_effects=verified_effects_a,
                unresolved_capacity=u_a,
                request_fingerprint=first_evt.request_fingerprint or "",
                idempotency_key_hash=first_evt.idempotency_key_hash,
                emitted_timestamp=timestamp_val,
                idempotency_contract=attempt_contract,
                verified_service_deduplication=verified_dedup,
            )
            attempts[aid] = state_vec
            unresolved_by_attempt[aid] = u_a

        # 3. Layer 2: Global Ground-Truth Effects Set & S_min
        all_verified_effects: set[str] = set()
        for a_vec in attempts.values():
            all_verified_effects.update(a_vec.verified_effects)

        top_c4_audit = reconciliation_audit or (
            tool_meta.get("reconciliation_audit")
            if isinstance(tool_meta, dict)
            else None
        )
        if (
            isinstance(top_c4_audit, ReconciliationAuditEvidence)
            and effective_audit_auth is not None
            and effective_audit_auth.verify(
                top_c4_audit,
                for_run_id=events[0].run_id if events else None,
                for_scenario_id=events[0].scenario_id if events else None,
            )
            and top_c4_audit.verified_effects
        ):
            all_verified_effects.update(top_c4_audit.verified_effects)
        s_min = len(all_verified_effects)

        # 4. Safe Idempotency Correlation & Tightening
        equivalence_classes: dict[str, list[str]] = {}
        for aid, a_vec in attempts.items():
            if a_vec.operation_id and a_vec.idempotency_key_hash:
                cls_key = f"{a_vec.operation_id}:{a_vec.idempotency_key_hash}"
            else:
                cls_key = f"singleton_{aid}"
            equivalence_classes.setdefault(cls_key, []).append(aid)

        unresolved_by_class: dict[str, int | float] = {}
        tightening_applied: dict[str, bool] = {}

        for cls_id, a_ids in equivalence_classes.items():
            if len(a_ids) == 1:
                aid = a_ids[0]
                unresolved_by_class[cls_id] = unresolved_by_attempt[aid]
                tightening_applied[cls_id] = False
                continue

            class_attempts = [attempts[aid] for aid in a_ids]

            # Evaluate the SIX concurrent conditions for safe idempotency tightening
            # Condition 1: Same Operation & Idempotency Scope (including action)
            first_op = class_attempts[0].operation_id
            first_act = class_attempts[0].action
            first_tool = class_attempts[0].tool_name
            c1_pass = (
                bool(first_op)
                and all(a.operation_id == first_op for a in class_attempts)
                and all(a.action == first_act for a in class_attempts)
                and all(a.tool_name == first_tool for a in class_attempts)
            )

            # Condition 2: Identical Canonical Request Fingerprint
            first_fp = class_attempts[0].request_fingerprint
            c2_pass = bool(first_fp) and all(a.request_fingerprint == first_fp for a in class_attempts)

            # Condition 3: Identical Effective Idempotency Key Hash
            first_kh = class_attempts[0].idempotency_key_hash
            c3_pass = bool(first_kh) and all(a.idempotency_key_hash == first_kh for a in class_attempts)

            # Condition 4: Retention Window within Verified TTL
            ttl = min(a.idempotency_contract.retention_ttl_seconds for a in class_attempts)
            timestamps = [a.emitted_timestamp for a in class_attempts]
            if any(ts is None for ts in timestamps) or ttl < 0:
                c4_pass = False
            else:
                valid_ts = [float(ts) for ts in timestamps if ts is not None]
                delta_t = max(valid_ts) - min(valid_ts)
                c4_pass = delta_t <= ttl

            # Condition 5: Independently Verified Service Deduplication (Tier C3 or C4)
            has_verified_dedup = all(a.verified_service_deduplication for a in class_attempts)
            has_c3_or_c4 = all(
                a.capacity_provenance in (CapacityProvenanceTier.TIER_C3, CapacityProvenanceTier.TIER_C4)
                for a in class_attempts
            )
            c5_pass = has_verified_dedup and has_c3_or_c4

            # Condition 6: No Payload Mutation, Parameter Divergence, or Key Expiry
            # Since C2 checks identical fingerprint and C4 checks TTL, confirm zero divergence
            c6_pass = c2_pass and c4_pass

            # Safe tightening rule
            if c1_pass and c2_pass and c3_pass and c4_pass and c5_pass and c6_pass:
                # Tighten: across an idempotent class with verified deduplication,
                # total effects cannot exceed max capacity of the operation.
                unresolved_vals = [unresolved_by_attempt[aid] for aid in a_ids]
                if any(math.isinf(v) for v in unresolved_vals):
                    unresolved_by_class[cls_id] = math.inf
                else:
                    class_max_cap = max(attempts[aid].max_capacity for aid in a_ids)
                    class_verified_count = len(
                        set().union(*(attempts[aid].verified_effects for aid in a_ids))
                    )
                    if class_max_cap < math.inf:
                        unresolved_by_class[cls_id] = max(0, int(class_max_cap - class_verified_count))
                    else:
                        unresolved_by_class[cls_id] = max(unresolved_vals)
                tightening_applied[cls_id] = True
            else:
                # Conservative fallback: u(K) = sum(u(a))
                unresolved_vals = [unresolved_by_attempt[aid] for aid in a_ids]
                if any(math.isinf(v) for v in unresolved_vals):
                    unresolved_by_class[cls_id] = math.inf
                else:
                    unresolved_by_class[cls_id] = sum(unresolved_vals)
                tightening_applied[cls_id] = False

        # 5. Compute Upper Bound S_max
        if capacity_breached or any(math.isinf(u) for u in unresolved_by_class.values()):
            s_max: int | float = math.inf
        else:
            s_max = s_min + int(sum(unresolved_by_class.values()))

        bounds = SideEffectBounds(s_min=s_min, s_max=s_max)

        # 6. Duplicate Detection Across Operations
        op_to_effects: dict[str, set[str]] = {}
        for a_vec in attempts.values():
            if a_vec.operation_id:
                op_to_effects.setdefault(a_vec.operation_id, set()).update(a_vec.verified_effects)

        duplicate_count = 0
        for distinct_effs in op_to_effects.values():
            if len(distinct_effs) > 1:
                duplicate_count += len(distinct_effs) - 1

        has_idempotency_violation = False
        for _cls_id, a_ids in equivalence_classes.items():
            if len(a_ids) > 1:
                class_attempts = [attempts[aid] for aid in a_ids]
                first_fp = class_attempts[0].request_fingerprint
                if any(a.request_fingerprint != first_fp for a in class_attempts):
                    has_idempotency_violation = True

        # 7. Derive External Status & Infrastructure Status
        ext_status = derive_external_status(
            bounds=bounds,
            expected_side_effects=expected_side_effects,
            contradictory_evidence=contradictory_evidence,
            has_idempotency_violation=has_idempotency_violation,
            duplicate_count=duplicate_count,
        )

        infra_status = (
            PrimaryInfrastructureStatus.INTEGRITY_VIOLATION.value
            if capacity_breached
            else PrimaryInfrastructureStatus.CLEAN.value
        )

        if capacity_breached and raise_on_breach:
            raise CapacityBreachIntegrityError(
                f"Capacity breach detected for attempt '{breached_attempt_id}': "
                f"observed physical effects exceed maximum capacity contract."
            )

        return AccountingResult(
            bounds=bounds,
            external_status=ext_status,
            verified_effects=all_verified_effects,
            attempts=attempts,
            unresolved_by_attempt=unresolved_by_attempt,
            equivalence_classes=equivalence_classes,
            unresolved_by_class=unresolved_by_class,
            tightening_applied=tightening_applied,
            capacity_breached=capacity_breached,
            breached_attempt_id=breached_attempt_id,
            infrastructure_status=infra_status,
            duplicate_count=duplicate_count,
            missing_identity_events=missing_identity_events,
            contradictory_evidence=contradictory_evidence,
        )
