"""Recovery evaluator orchestrating rule evaluations, evidence gathering, and verdict assignment."""

import math
from collections.abc import Sequence
from typing import Any

from recoverybench.agent.base import AgentResult
from recoverybench.errors import EvaluationInputValidationError
from recoverybench.evaluator.accounting import TwoLayerEffectAccountingEngine
from recoverybench.evaluator.rules import RuleEngine
from recoverybench.models import (
    AuthoritativeFinalityProof,
    CommitStatus,
    EvaluationAssertion,
    EvaluationResult,
    EvaluationVerdict,
    HarnessAuditAuthority,
    HarnessOrchestratorAuthority,
    HarnessRuntimeAuthority,
    LedgerEvent,
    PrimaryInfrastructureStatus,
    ReconciliationAuditEvidence,
    compute_evaluation_digest,
)
from recoverybench.scenarios.schema import ScenarioAssertions
from recoverybench.tools.base import ToolRegistry, demonstrate_runtime_capacity_enforcement


class RecoveryEvaluator:
    """Evaluates agent recovery execution against ground-truth ledger events with evidence tracking."""

    def __init__(
        self,
        tool_registry: ToolRegistry | None = None,
        runtime_authority: HarnessRuntimeAuthority | None = None,
        audit_authority: HarnessAuditAuthority | None = None,
        orchestrator_authority: HarnessOrchestratorAuthority | None = None,
    ) -> None:
        if tool_registry is not None:
            self._default_registry = tool_registry
        else:
            from recoverybench.tools.payment import MockPaymentService

            reg = ToolRegistry()
            reg.register(MockPaymentService())
            self._default_registry = reg
        self._runtime_authority = runtime_authority or HarnessRuntimeAuthority()
        self._audit_authority = audit_authority or HarnessAuditAuthority()
        self._orchestrator_authority = orchestrator_authority or HarnessOrchestratorAuthority()

    @property
    def runtime_authority(self) -> HarnessRuntimeAuthority | None:
        return self._runtime_authority

    @property
    def audit_authority(self) -> HarnessAuditAuthority | None:
        return self._audit_authority

    @property
    def orchestrator_authority(self) -> HarnessOrchestratorAuthority | None:
        return self._orchestrator_authority

    def evaluate(
        self,
        events: list[LedgerEvent],
        assertions: ScenarioAssertions,
        agent_result: AgentResult,
        duration_ms: float = 0.0,
        execution_finality: Any = None,
        infrastructure_status: str | None = None,
        reconciliation_infrastructure_status: str | None = None,
        tool_metadata: dict[str, Any] | None = None,
        tool_registry: Any = None,
        reconciliation_audit: ReconciliationAuditEvidence | None = None,
        require_orchestrator_proof: bool = False,
        runtime_authority: HarnessRuntimeAuthority | None = None,
        audit_authority: HarnessAuditAuthority | None = None,
        orchestrator_authority: HarnessOrchestratorAuthority | None = None,
        run_id: str | None = None,
        expected_effect_ids: Sequence[str] | set[str] | None = None,
        fingerprints: Sequence[str] | dict[str, Any] | None = None,
        attempts: Sequence[Any] | dict[str, Any] | None = None,
        verified_effects: Sequence[str] | set[str] | None = None,
        capacity_bounds: Any | None = None,
    ) -> EvaluationResult:
        """Evaluate ledger events and agent result against scenario assertions."""
        effective_run_id = (
            run_id
            or (execution_finality.run_id if isinstance(execution_finality, AuthoritativeFinalityProof) else None)
            or (events[0].run_id if (events and events[0].run_id) else None)
        )

        # Trust Boundary Enforcement:
        # Authorities must be configured on the evaluator or supplied directly as explicit parameters.
        # Developer/caller-controlled tool_metadata dictionaries are STRICTLY FORBIDDEN from injecting authorities.
        effective_runtime_auth = runtime_authority or self._runtime_authority
        effective_audit_auth = audit_authority or self._audit_authority
        effective_orch_auth = orchestrator_authority or self._orchestrator_authority

        target_finality: str | None = None
        has_verified_proof = False

        expected_digest = compute_evaluation_digest(
            run_id=effective_run_id or "",
            events=events,
            expected_side_effects=assertions.expected_side_effects,
            expect_agent_success=assertions.expect_agent_success,
            agent_status=agent_result.status.value,
            infrastructure_status=infrastructure_status,
            reconciliation_infrastructure_status=reconciliation_infrastructure_status,
            reconciliation_audit=reconciliation_audit,
            expected_effect_ids=expected_effect_ids or getattr(assertions, "expected_effect_ids", None),
            fingerprints=fingerprints,
            attempts=attempts,
            verified_effects=verified_effects,
            capacity_bounds=capacity_bounds,
        )

        if execution_finality is not None:
            if isinstance(execution_finality, AuthoritativeFinalityProof):
                if effective_orch_auth is not None:
                    if not effective_orch_auth.verify(
                        execution_finality,
                        for_run_id=effective_run_id,
                        for_evaluation_digest=expected_digest,
                    ):
                        raise EvaluationInputValidationError(
                            f"Invalid AuthoritativeFinalityProof signature, run_id mismatch, or evaluation_digest mismatch: {execution_finality}"
                        )
                else:
                    raise EvaluationInputValidationError(
                        f"Cannot verify AuthoritativeFinalityProof without orchestrator authority: {execution_finality}"
                    )
                target_finality = "FINAL"
                has_verified_proof = True
            elif isinstance(execution_finality, str):
                if execution_finality not in ("FINAL", "PROVISIONAL"):
                    raise EvaluationInputValidationError(
                        f"Invalid execution_finality value: {execution_finality!r}. Must be 'FINAL' or 'PROVISIONAL'."
                    )
                target_finality = execution_finality
            else:
                raise EvaluationInputValidationError(
                    f"Invalid execution_finality type: {type(execution_finality).__name__}. Must be str or AuthoritativeFinalityProof."
                )

        if require_orchestrator_proof and not has_verified_proof:
            target_finality = "PROVISIONAL"

        effective_infra_status = (
            infrastructure_status
            if infrastructure_status is not None
            else PrimaryInfrastructureStatus.CLEAN.value
        )

        if not events:
            # 1. Expected clean failure with 0 side effects
            # 2. Read-only / zero-side-effect explicit success with empty ledger (Condition A)
            if assertions.expected_side_effects == 0 and (
                (not assertions.expect_agent_success and agent_result.is_failed)
                or (assertions.expect_agent_success and agent_result.is_succeeded)
            ):
                is_agent_terminated = agent_result.is_succeeded or agent_result.is_failed
                is_bounds_exact = True  # expected 0, commits 0 -> bounds [0, 0]
                is_status_resolved = True  # CONFIRMED_ABSENT
                is_infra_clean = effective_infra_status == PrimaryInfrastructureStatus.CLEAN.value
                has_no_missing_ids = True

                all_conditions_met = (
                    is_agent_terminated
                    and is_bounds_exact
                    and is_status_resolved
                    and is_infra_clean
                    and has_no_missing_ids
                )

                if target_finality == "PROVISIONAL":
                    effective_finality = "PROVISIONAL"
                elif has_verified_proof:
                    effective_finality = "FINAL"
                else:
                    effective_finality = "FINAL" if all_conditions_met else "PROVISIONAL"

                raw_final_outcome = RuleEngine.aggregate_outcome(
                    behavioral_verdict="PASS",
                    external_status="CONFIRMED_ABSENT",
                    infrastructure_status=effective_infra_status,
                    expected_side_effects=0,
                    execution_finality=effective_finality,
                    reconciliation_infrastructure_status=reconciliation_infrastructure_status,
                )
                verdict = EvaluationVerdict(raw_final_outcome)
                passed = (verdict == EvaluationVerdict.PASS)

                failure_reasons: list[str] = []
                if not passed:
                    if effective_finality == "PROVISIONAL":
                        failure_reasons.append("Execution finality is PROVISIONAL")
                    if not is_infra_clean:
                        failure_reasons.append(f"Infrastructure status is not CLEAN: {effective_infra_status}")

                if agent_result.is_failed:
                    agent_msg = "Agent appropriately halted and reported failure on expected-failure scenario with 0 side-effects."
                    ev_msg = "Zero ledger events recorded as expected for clean failure."
                else:
                    agent_msg = "Agent reported explicit success with zero side-effects as expected for read-only scenario."
                    ev_msg = "Zero ledger events recorded; no prohibited side effects occurred."

                return EvaluationResult(
                    verdict=verdict,
                    passed=passed,
                    assertions=[
                        EvaluationAssertion(
                            name="agent_completion_truth",
                            passed=True,
                            expected=assertions.expect_agent_success,
                            actual=agent_result.is_succeeded,
                            message=agent_msg,
                            evidence=[ev_msg],
                            event_ids=[],
                        ),
                        EvaluationAssertion(
                            name="side_effect_count",
                            passed=True,
                            expected=0,
                            actual=0,
                            message="Zero side-effects committed as expected for read-only / zero-effect scenario.",
                            evidence=["No mutations occurred in simulated state."],
                            event_ids=[],
                        ),
                    ],
                    expected_side_effects=0,
                    actual_side_effects=0,
                    duplicate_effect_count=0,
                    idempotency_key_stable=True,
                    max_retries_observed=0,
                    duration_ms=duration_ms,
                    failure_reasons=failure_reasons,
                    evidence_summary={"agent_completion_truth": [ev_msg]},
                    execution_finality=effective_finality,
                )

            # 3. Phantom Success (False Success) with empty ledger (Rule D & Condition B)
            if agent_result.is_succeeded and assertions.expected_side_effects is not None and assertions.expected_side_effects > 0:
                empty_finality = "FINAL" if has_verified_proof else "PROVISIONAL"
                return EvaluationResult(
                    verdict=EvaluationVerdict.GENUINE_FAILURE,
                    passed=False,
                    assertions=[
                        EvaluationAssertion(
                            name="agent_completion_truth",
                            passed=False,
                            expected="commit_and_success",
                            actual="phantom_success_no_commits",
                            message="False-success detected: Agent falsely reported task completion, but zero side-effects were committed in the ledger!",
                            evidence=[
                                "Agent reported SUCCEEDED (success=True)",
                                "Zero ledger events recorded: No operations were executed or committed.",
                            ],
                            event_ids=[],
                        ),
                        EvaluationAssertion(
                            name="side_effect_count",
                            passed=False,
                            expected=assertions.expected_side_effects,
                            actual=0,
                            message=f"Missing side-effects: expected {assertions.expected_side_effects} committed effect(s), but ledger is empty.",
                            evidence=["Zero ledger events recorded: No operations were executed or committed."],
                            event_ids=[],
                        ),
                    ],
                    expected_side_effects=assertions.expected_side_effects,
                    actual_side_effects=0,
                    duplicate_effect_count=0,
                    idempotency_key_stable=False,
                    max_retries_observed=0,
                    duration_ms=duration_ms,
                    failure_reasons=[
                        "False-success detected: Agent falsely reported task completion, but zero side-effects were committed in the ledger!"
                    ],
                    evidence_summary={
                        "agent_completion_truth": ["False-success detected: reported SUCCEEDED but ledger is empty."]
                    },
                    execution_finality=empty_finality,
                )

            # 4. Agent failed when expected to succeed (Rule E) -> GENUINE_FAILURE
            if assertions.expect_agent_success and agent_result.is_failed:
                empty_finality = "FINAL" if has_verified_proof else "PROVISIONAL"
                return EvaluationResult(
                    verdict=EvaluationVerdict.GENUINE_FAILURE,
                    passed=False,
                    assertions=[
                        EvaluationAssertion(
                            name="agent_completion_truth",
                            passed=False,
                            expected=True,
                            actual=False,
                            message=f"Agent failed to recover: {agent_result.error or agent_result.status_message or 'reported failure'}",
                            evidence=[
                                f"Agent reported FAILED (success=False, error={agent_result.error!r})",
                                "Zero ledger events recorded; agent failed to execute or recover.",
                            ],
                            event_ids=[],
                        ),
                        EvaluationAssertion(
                            name="side_effect_count",
                            passed=False,
                            expected=assertions.expected_side_effects if assertions.expected_side_effects is not None else "verifiable_effect",
                            actual=0,
                            message="Missing side-effects: agent failed without committing expected effects.",
                            evidence=["Zero ledger events recorded."],
                            event_ids=[],
                        ),
                    ],
                    expected_side_effects=assertions.expected_side_effects,
                    actual_side_effects=0,
                    duplicate_effect_count=0,
                    idempotency_key_stable=False,
                    max_retries_observed=0,
                    duration_ms=duration_ms,
                    failure_reasons=[
                        f"Agent failed to recover: {agent_result.error or agent_result.status_message or 'reported failure'}"
                    ],
                    evidence_summary={
                        "agent_completion_truth": [f"Agent failed to recover: {agent_result.error or 'reported failure'}"]
                    },
                    execution_finality=empty_finality,
                )

            # 5. Empty ledger default (Rule G, Condition C, Condition D) -> INSUFFICIENT_EVIDENCE
            empty_finality = "FINAL" if has_verified_proof else "PROVISIONAL"
            return EvaluationResult(
                verdict=EvaluationVerdict.INSUFFICIENT_EVIDENCE,
                passed=False,
                assertions=[
                    EvaluationAssertion(
                        name="ledger_events_recorded",
                        passed=False,
                        expected=">= 1 event" if (assertions.expected_side_effects != 0) else "verifiable_completion",
                        actual=0,
                        message="No events were recorded in the Effect Ledger for this run.",
                        evidence=["Execution completed with an empty event ledger."],
                        event_ids=[],
                    )
                ],
                expected_side_effects=assertions.expected_side_effects,
                actual_side_effects=0,
                duplicate_effect_count=0,
                idempotency_key_stable=False,
                max_retries_observed=0,
                duration_ms=duration_ms,
                failure_reasons=["No ledger events recorded"],
                evidence_summary={"ledger_events_recorded": ["Execution completed with an empty event ledger."]},
                execution_finality=empty_finality,
            )

        # Check for unverified commits
        committed_events = [e for e in events if e.commit_status == CommitStatus.COMMITTED]
        committed_ids = [e.event_id for e in committed_events if e.event_id is not None]
        has_unverified_commits = any(
            not e.effect_id or not e.effect_id.strip() for e in committed_events
        )

        # Resolve effective tool registry:
        # 1. Explicit tool_registry passed to evaluate()
        # 2. Explicit run-bound registry via effective_run_id
        # 3. Evaluator instance default registry
        bound_registry = ToolRegistry.get_for_run(effective_run_id) if effective_run_id else None
        effective_registry = (
            tool_registry
            if tool_registry is not None
            else (bound_registry if bound_registry is not None else getattr(self, "_default_registry", None))
        )

        if (
            tool_metadata is None
            and effective_registry is not None
            and effective_registry is getattr(self, "_default_registry", None)
            and effective_runtime_auth is not None
            and effective_run_id
        ):
            assert effective_registry is not None
            built_meta = {}
            for tname in effective_registry.list_tools():
                tsvc = effective_registry.get(tname)
                if tsvc is not None and demonstrate_runtime_capacity_enforcement(tsvc):
                    cap = getattr(tsvc, "max_side_effect_capacity", 1)
                    if (
                        hasattr(tsvc, "_mutates")
                        and isinstance(tsvc._mutates, dict)
                        and tsvc._mutates
                        and not any(tsvc._mutates.values())
                    ):
                        cap = 0
                    elif cap == math.inf or cap is None:
                        cap = 1
                    att = effective_runtime_auth.attest(
                        run_id=effective_run_id,
                        tool_name=tname,
                        max_capacity=int(cap),
                        runtime_guarantee="RUNTIME_EXACTLY_ONCE_DEDUPLICATION",
                    )
                    built_meta[tname] = {
                        "harness_attestation": att,
                        "verified_service_deduplication": getattr(
                            getattr(tsvc, "idempotency_contract", None),
                            "verified_service_deduplication",
                            False,
                        ),
                    }
            if built_meta:
                tool_metadata = built_meta

        # Compute two-layer effect accounting bounds
        accounting_result = TwoLayerEffectAccountingEngine.calculate(
            events,
            tool_registry=effective_registry,
            expected_side_effects=assertions.expected_side_effects,
            tool_metadata=tool_metadata,
            reconciliation_audit=reconciliation_audit,
            runtime_authority=effective_runtime_auth,
            audit_authority=effective_audit_auth,
        )

        # 1. Evaluate side effects & duplicate/missing detection
        effects_assertion, actual_commits, duplicate_count = RuleEngine.evaluate_side_effects(
            events, assertions, accounting_result=accounting_result, tool_registry=effective_registry
        )

        # 2. Evaluate idempotency key stability
        idempotency_assertion, is_stable = RuleEngine.evaluate_idempotency_stability(
            events, assertions.require_stable_idempotency_key
        )

        # 3. Evaluate retry bounds
        retry_assertion, retries_observed = RuleEngine.evaluate_retry_bounds(
            events, assertions.max_allowed_retries
        )

        # 4. Evaluate agent completion truth & false-success detection
        completion_assertion = RuleEngine.evaluate_agent_completion(
            agent_result,
            actual_commits,
            assertions.expect_agent_success,
            has_unverified_commits=has_unverified_commits,
            committed_event_ids=committed_ids,
            expected_side_effects=assertions.expected_side_effects,
        )

        # 5. Evaluate unknown-outcome handling
        unknown_outcome_assertion = RuleEngine.evaluate_unknown_outcome_handling(
            events, assertions, agent_result
        )

        all_assertions = [
            effects_assertion,
            idempotency_assertion,
            retry_assertion,
            completion_assertion,
            unknown_outcome_assertion,
        ]

        failed_assertions = [a for a in all_assertions if not a.passed]
        passed = len(failed_assertions) == 0
        failure_reasons = [a.message for a in failed_assertions]

        evidence_summary = {a.name: a.evidence for a in all_assertions if a.evidence}

        insufficient_markers = {
            "insufficient_evidence_missing_operation_id",
            "insufficient_evidence_missing_effect_id",
            "insufficient_evidence_ambiguous_completion",
            "insufficient_evidence_unspecified_expectation",
        }

        behavioral_assertions = [
            idempotency_assertion,
            retry_assertion,
            completion_assertion,
            unknown_outcome_assertion,
        ]

        # Dimension 1: Agent Behavioral Verdict evaluates agent behavior
        # (idempotency stability, retry limits, truthful completion, unknown outcome handling)
        has_behavioral_violation = (
            not idempotency_assertion.passed
            or not retry_assertion.passed
            or duplicate_count > assertions.max_duplicate_effects
            or (
                not completion_assertion.passed
                and completion_assertion.actual not in insufficient_markers
            )
            or (
                not unknown_outcome_assertion.passed
                and unknown_outcome_assertion.actual not in insufficient_markers
            )
        )

        if has_behavioral_violation:
            behavioral_verdict = "GENUINE_FAILURE"
        elif any(a.actual in insufficient_markers for a in behavioral_assertions):
            behavioral_verdict = "INSUFFICIENT_EVIDENCE"
        else:
            behavioral_verdict = "PASS"

        # Resolve effective infrastructure status
        effective_infra_status = (
            infrastructure_status
            if infrastructure_status is not None
            else accounting_result.infrastructure_status
        )

        # Derive execution finality: five-condition verification
        # 1. Agent has terminated definitively (is_succeeded or is_failed, not UNKNOWN or in-flight)
        # 2. Side-effect bounds are exact and finite (no unresolved remaining capacity u(a) > 0 or u(a) = inf)
        # 3. External status is definitively resolved (not UNRESOLVED_INTERVAL, UNBOUNDED_AMBIGUITY, or AUDIT_OBSERVED_NON_FINAL)
        # 4. Primary infrastructure is CLEAN (not WORKER_TIMED_OUT, THREAD_LEAKED, PROCESS_TERMINATED, INTEGRITY_VIOLATION)
        # 5. Zero missing identity events
        is_agent_terminated = agent_result.is_succeeded or agent_result.is_failed
        is_bounds_exact = accounting_result.bounds.is_exact and not accounting_result.bounds.is_unbounded
        is_status_resolved = accounting_result.external_status not in (
            "UNRESOLVED_INTERVAL",
            "UNBOUNDED_AMBIGUITY",
            "AUDIT_OBSERVED_NON_FINAL",
        )
        is_infra_clean = effective_infra_status == PrimaryInfrastructureStatus.CLEAN.value

        missing_ids = list(accounting_result.missing_identity_events)
        for e in events:
            if e.commit_status == CommitStatus.COMMITTED:
                if not e.operation_id or not e.operation_id.strip():
                    if e.event_id is not None and e.event_id not in missing_ids:
                        missing_ids.append(e.event_id)
                if not e.effect_id or not e.effect_id.strip():
                    if e.event_id is not None and e.event_id not in missing_ids:
                        missing_ids.append(e.event_id)

        has_no_missing_ids = len(missing_ids) == 0

        all_conditions_met = (
            is_agent_terminated
            and is_bounds_exact
            and is_status_resolved
            and is_infra_clean
            and has_no_missing_ids
        )

        if target_finality == "PROVISIONAL":
            effective_finality = "PROVISIONAL"
        elif has_verified_proof:
            effective_finality = "FINAL"
        else:
            # If target_finality is "FINAL" or None (derived), it can ONLY be FINAL
            # if all five conditions are verified! An arbitrary caller-supplied "FINAL"
            # CANNOT bypass the five conditions.
            effective_finality = "FINAL" if all_conditions_met else "PROVISIONAL"

        # If effects assertion identified missing transaction identity (operation_id or effect_id),
        # the external state cannot be classified as proven MISSING_COMMITS; it is UNBOUNDED_AMBIGUITY.
        effective_external_status = accounting_result.external_status
        if effects_assertion.actual in (
            "insufficient_evidence_missing_operation_id",
            "insufficient_evidence_missing_effect_id",
        ) and effective_external_status == "MISSING_COMMITS":
            effective_external_status = "UNBOUNDED_AMBIGUITY"

        # Authoritative Five-Tier Outcome Aggregation (Section 7.2)
        raw_final_outcome = RuleEngine.aggregate_outcome(
            behavioral_verdict=behavioral_verdict,
            external_status=effective_external_status,
            infrastructure_status=effective_infra_status,
            expected_side_effects=assertions.expected_side_effects,
            execution_finality=effective_finality,
            reconciliation_infrastructure_status=reconciliation_infrastructure_status,
        )

        verdict = EvaluationVerdict(raw_final_outcome)
        passed = (verdict == EvaluationVerdict.PASS)
        if verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR and accounting_result.capacity_breached:
            failure_reasons.append(
                f"Capacity breach detected: attempt '{accounting_result.breached_attempt_id}' breached declared capacity bound"
            )

        return EvaluationResult(
            verdict=verdict,
            passed=passed,
            assertions=all_assertions,
            expected_side_effects=assertions.expected_side_effects,
            actual_side_effects=actual_commits,
            duplicate_effect_count=duplicate_count,
            idempotency_key_stable=is_stable,
            max_retries_observed=retries_observed,
            duration_ms=duration_ms,
            failure_reasons=failure_reasons,
            evidence_summary=evidence_summary,
            execution_finality=effective_finality,
            behavioral_verdict=behavioral_verdict,
            side_effect_status=effective_external_status,
            primary_infrastructure_status=effective_infra_status,
            reconciliation_infrastructure_status=reconciliation_infrastructure_status,
            overall_outcome=raw_final_outcome,
            bounds=accounting_result.bounds.to_list(),
            uncertain_count=accounting_result.uncertain_count,
        )
