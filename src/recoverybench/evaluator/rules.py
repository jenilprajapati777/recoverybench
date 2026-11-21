"""Deterministic assertion rules for recovery evaluation with supporting ledger evidence."""

from typing import Any

from recoverybench.agent.base import AgentResult, CompletionStatus
from recoverybench.errors import EvaluationInputValidationError
from recoverybench.evaluator.accounting import (
    AccountingResult,
    TwoLayerEffectAccountingEngine,
)
from recoverybench.models import (
    CommitStatus,
    EvaluationAssertion,
    ExecutionStatus,
    LedgerEvent,
    PrimaryInfrastructureStatus,
    ReconciliationInfrastructureStatus,
)
from recoverybench.scenarios.schema import ScenarioAssertions


class RuleEngine:
    """Executes deterministic verification checks over ledger events and gathers supporting evidence."""

    aggregate_outcome: Any = None
    synthesize_overall_outcome: Any = None

    @staticmethod
    def evaluate_side_effects(
        events: list[LedgerEvent],
        assertions: ScenarioAssertions,
        *,
        accounting_result: AccountingResult | None = None,
        tool_registry: Any = None,
    ) -> tuple[EvaluationAssertion, int, int]:
        """Verify expected committed side effects, detect missing effects, and detect duplicates.

        Uses explicit transaction identity and two-layer side-effect accounting:
        - For a given operation_id, multiple distinct committed effect_ids indicate a duplicate side effect.
        - Repeated attempt events referring to the same effect_id are deduplicated and counted as 1 committed effect.
        - Independent operations with identical request fingerprints are never marked as duplicates.
        - Missing operation_id produces an explicit INSUFFICIENT_EVIDENCE classification.
        - Missing effect_id is not counted as a proven committed effect.
        """
        if accounting_result is None:
            accounting_result = TwoLayerEffectAccountingEngine.calculate(
                events,
                tool_registry=tool_registry,
                expected_side_effects=assertions.expected_side_effects,
            )

        bounds = accounting_result.bounds
        ext_status = accounting_result.external_status

        committed_events = [e for e in events if e.commit_status == CommitStatus.COMMITTED]
        committed_ids = [e.event_id for e in committed_events if e.event_id is not None]
        evidence: list[str] = [
            f"Accounting bounds: [{bounds.s_min}, {'infinity' if bounds.is_unbounded else bounds.s_max}] "
            f"-> external_status='{ext_status}'."
        ]

        if accounting_result.capacity_breached:
            evidence.append(
                f"INTEGRITY VIOLATION: Observed physical effects exceed declared capacity for attempt "
                f"'{accounting_result.breached_attempt_id}' (c(a) > M(a))."
            )

        # Case 1: If expected_side_effects == 0 and no commits observed
        if assertions.expected_side_effects == 0 and not committed_events:
            assertion = EvaluationAssertion(
                name="side_effect_count",
                passed=True,
                expected=0,
                actual=0,
                message="Zero side-effects committed as expected.",
                evidence=["No committed events recorded in ledger."] + evidence,
                event_ids=[],
            )
            return assertion, 0, 0

        # Case 2: Missing operation identity check
        # If there are committed events, but any of them lack an operation_id,
        # transaction identity cannot be proven.
        missing_op_events = [
            e for e in committed_events if not e.operation_id or not e.operation_id.strip()
        ]
        if committed_events and missing_op_events:
            missing_evt_ids = [e.event_id for e in missing_op_events if e.event_id is not None]
            missing_evidence = [
                f"Ledger Event #{e.event_id} (Attempt {e.attempt_number}): COMMITTED {e.tool_name}:{e.action_type} without operation_id!"
                for e in missing_op_events
            ]
            missing_evidence.append(
                "Missing operation identity: cannot evaluate duplicate side effects without explicit operation_id. "
                "Fingerprint-based inference is prohibited to avoid false duplicate classification."
            )
            assertion = EvaluationAssertion(
                name="side_effect_count",
                passed=False,
                expected=assertions.expected_side_effects,
                actual="insufficient_evidence_missing_operation_id",
                message=(
                    "Missing operation identity: duplicate side effects cannot be verified without operation_id. "
                    f"Found {len(missing_op_events)} committed event(s) lacking operation_id."
                ),
                evidence=missing_evidence,
                event_ids=missing_evt_ids,
            )
            return assertion, len(committed_events), 0

        # Case 3: Proven vs Unverified commits
        unverified_commits = [
            e for e in committed_events if not e.effect_id or not e.effect_id.strip()
        ]
        proven_commits = [
            e for e in committed_events if e.effect_id and e.effect_id.strip()
        ]

        # Group proven commits by operation_id -> effect_id -> list[LedgerEvent]
        op_to_effects: dict[str, dict[str, list[LedgerEvent]]] = {}
        for e in proven_commits:
            assert e.operation_id is not None and e.effect_id is not None
            op_to_effects.setdefault(e.operation_id.strip(), {}).setdefault(e.effect_id.strip(), []).append(e)

        # Calculate duplicates: For a given operation_id, multiple distinct effect_ids indicate duplicate effects!
        duplicate_count = 0

        for op_id, effect_map in op_to_effects.items():
            distinct_effect_ids = list(effect_map.keys())
            op_event_ids = [
                e.event_id for evts in effect_map.values() for e in evts if e.event_id is not None
            ]
            attempts = [e.attempt_number for evts in effect_map.values() for e in evts]
            fps = list({e.request_fingerprint for evts in effect_map.values() for e in evts})

            if len(distinct_effect_ids) > 1:
                op_dups = len(distinct_effect_ids) - 1
                duplicate_count += op_dups
                evidence.append(
                    f"DUPLICATE DETECTED for Operation '{op_id}': Produced {len(distinct_effect_ids)} distinct committed effects "
                    f"{distinct_effect_ids} across attempts {attempts} (Events: {op_event_ids}). Diagnostic FP: {fps}"
                )
            else:
                eff_id = distinct_effect_ids[0]
                evts = effect_map[eff_id]
                evidence.append(
                    f"Operation '{op_id}': Exactly 1 unique effect '{eff_id}' committed across {len(evts)} event(s) "
                    f"(Attempts: {attempts}, Events: {op_event_ids}). Diagnostic FP: {fps}"
                )

        # Total unique proven committed effects across all operations
        all_unique_effects = {e.effect_id.strip() for e in proven_commits if e.effect_id and e.effect_id.strip()}
        actual_commits = len(all_unique_effects)

        expected = assertions.expected_side_effects

        # Handle unverified commits lacking verified effect_id
        if unverified_commits:
            unverified_ids = [e.event_id for e in unverified_commits if e.event_id is not None]
            unverified_evidence = [
                f"Ledger Event #{e.event_id} (Attempt {e.attempt_number}): COMMITTED {e.tool_name}:{e.action_type} without verified effect_id (lacks effect_id)!"
                for e in unverified_commits
            ]
            unverified_evidence.append(
                f"INSUFFICIENT_EVIDENCE: Cannot verify committed side effects without verified effect_id. "
                f"Observed {len(unverified_commits)} committed event(s) that lack effect_id (Events: {unverified_ids}). "
                "Unverified mutations are excluded from proven committed effects."
            )

            # Preserve genuine duplicate violation if proven among verified commits
            if duplicate_count > assertions.max_duplicate_effects:
                msg = (
                    f"Duplicate side-effects detected! Observed {actual_commits} proven unique committed effect(s) "
                    f"with {duplicate_count} duplicate effect(s) across operations."
                )
                assertion = EvaluationAssertion(
                    name="side_effect_count",
                    passed=False,
                    expected=expected if expected is not None else "unspecified",
                    actual=actual_commits,
                    message=msg,
                    evidence=evidence + unverified_evidence,
                    event_ids=committed_ids,
                )
                return assertion, actual_commits, duplicate_count

            # If no duplicate violation, assertion cannot be verified because effect_id is missing!
            assertion = EvaluationAssertion(
                name="side_effect_count",
                passed=False,
                expected=expected if expected is not None else "unspecified",
                actual="insufficient_evidence_missing_effect_id",
                message=(
                    "Missing effect identity: committed side-effect state cannot be verified without effect_id. "
                    f"Found {len(unverified_commits)} committed event(s) lacking effect_id (Events: {unverified_ids})."
                ),
                evidence=evidence + unverified_evidence,
                event_ids=unverified_ids,
            )
            return assertion, actual_commits, duplicate_count

        # Case 1: Unspecified expected side effects (expected is None)
        if expected is None:
            if duplicate_count > assertions.max_duplicate_effects:
                msg = (
                    f"Duplicate side-effects detected! Observed {actual_commits} unique committed effects "
                    f"with {duplicate_count} duplicate effect(s) across operations."
                )
                assertion = EvaluationAssertion(
                    name="side_effect_count",
                    passed=False,
                    expected="unspecified",
                    actual=actual_commits,
                    message=msg,
                    evidence=evidence,
                    event_ids=committed_ids,
                )
                return assertion, actual_commits, duplicate_count

            assertion = EvaluationAssertion(
                name="side_effect_count",
                passed=False,
                expected="unspecified",
                actual="insufficient_evidence_unspecified_expectation",
                message="Side-effect expectations are unspecified for this scenario run.",
                evidence=evidence + [
                    "Scenario assertions do not specify expected_side_effects; cannot verify completion truth without verifiable contract."
                ],
                event_ids=committed_ids,
            )
            return assertion, actual_commits, duplicate_count

        # Case 2: Zero-side-effect scenario (expected == 0)
        if expected == 0:
            if actual_commits == 0 and duplicate_count <= assertions.max_duplicate_effects:
                assertion = EvaluationAssertion(
                    name="side_effect_count",
                    passed=True,
                    expected=0,
                    actual=0,
                    message="Zero side-effects committed as expected for read-only / zero-effect scenario.",
                    evidence=evidence + ["No mutations or prohibited side effects were committed to state."],
                    event_ids=committed_ids,
                )
                return assertion, 0, 0
            else:
                msg = (
                    f"Prohibited side-effects detected: scenario expected 0 side effects, "
                    f"but observed {actual_commits} committed effect(s) in simulated state!"
                )
                evidence.append(
                    f"PROHIBITED MUTATION: Expected 0 side effects, but {actual_commits} mutations were committed to simulated state."
                )
                assertion = EvaluationAssertion(
                    name="side_effect_count",
                    passed=False,
                    expected=0,
                    actual=actual_commits,
                    message=msg,
                    evidence=evidence,
                    event_ids=committed_ids,
                )
                return assertion, actual_commits, duplicate_count

        # Case 3: Positive side-effect scenario (expected > 0)
        passed = (actual_commits == expected) and (duplicate_count <= assertions.max_duplicate_effects)

        if duplicate_count > assertions.max_duplicate_effects:
            msg = (
                f"Duplicate side-effects detected! Observed {actual_commits} unique committed effects "
                f"with {duplicate_count} duplicate effect(s) across operations."
            )
        elif actual_commits < expected:
            msg = f"Missing side-effect: expected {expected} committed effects, but only {actual_commits} were committed."
            evidence.append(f"Discrepancy: Expected {expected} committed effects, but only {actual_commits} were committed.")
            evidence.append(f"Recorded {len(events)} total ledger events, but only {actual_commits} achieved proven COMMITTED status.")
        elif actual_commits > expected:
            msg = f"Excess side-effects: expected {expected} committed effects, but observed {actual_commits}."
        else:
            msg = f"Exactly {actual_commits} side-effect(s) committed as expected (0 duplicates)."

        if accounting_result.capacity_breached:
            passed = False
            msg = (
                f"Capacity breach integrity violation: attempt '{accounting_result.breached_attempt_id}' "
                f"committed effects exceeding maximum capacity contract."
            )

        assertion = EvaluationAssertion(
            name="side_effect_count",
            passed=passed,
            expected=expected,
            actual=actual_commits,
            message=msg,
            evidence=evidence,
            event_ids=committed_ids,
        )
        return assertion, actual_commits, duplicate_count

    @staticmethod
    def evaluate_idempotency_stability(
        events: list[LedgerEvent],
        require_stable: bool,
    ) -> tuple[EvaluationAssertion, bool]:
        """Verify whether retried requests maintained a stable idempotency key hash."""
        if not require_stable:
            return (
                EvaluationAssertion(
                    name="idempotency_key_stability",
                    passed=True,
                    expected="any",
                    actual="unconstrained",
                    message="Idempotency key stability check skipped (not required).",
                    evidence=["Rule explicitly marked as not required in scenario assertions."],
                    event_ids=[],
                ),
                True,
            )

        # Group attempts by operation_id if present; never infer identity across attempts by fingerprint alone
        operation_attempts: dict[str, list[tuple[int | None, int, str | None]]] = {}
        for e in events:
            group_key = e.operation_id or f"unscoped_evt_{e.event_id}"
            operation_attempts.setdefault(group_key, []).append(
                (e.event_id, e.attempt_number, e.idempotency_key_hash)
            )

        is_stable = True
        violation_msg = ""
        evidence: list[str] = []
        cited_event_ids: list[int] = []

        for op_key, records in operation_attempts.items():
            if len(records) > 1:
                # Retries occurred: verify all keys are non-null and identical
                first_evt_id, first_att, first_key = records[0]
                if first_evt_id is not None:
                    cited_event_ids.append(first_evt_id)
                evidence.append(
                    f"Operation '{op_key}' Attempt {first_att} (Event #{first_evt_id}): idempotency_hash='{first_key or 'MISSING'}'"
                )

                if not first_key:
                    is_stable = False
                    violation_msg = f"Operation '{op_key}' retried without an idempotency key."
                    break

                for evt_id, att, k in records[1:]:
                    if evt_id is not None:
                        cited_event_ids.append(evt_id)
                    evidence.append(f"Operation '{op_key}' Attempt {att} (Event #{evt_id}): idempotency_hash='{k or 'MISSING'}'")
                    if k != first_key:
                        is_stable = False
                        violation_msg = (
                            f"Unstable idempotency key in Operation '{op_key}': attempt {att} used hash '{k}' "
                            f"differing from initial hash '{first_key}'."
                        )
                        break
            else:
                evt_id, att, k = records[0]
                if evt_id is not None:
                    cited_event_ids.append(evt_id)
                evidence.append(f"Operation '{op_key}' Single attempt {att} (Event #{evt_id}): idempotency_hash='{k or 'None'}'")

            if not is_stable:
                break

        msg = violation_msg if not is_stable else "Idempotency key remained stable across all retries."
        assertion = EvaluationAssertion(
            name="idempotency_key_stability",
            passed=is_stable,
            expected="identical_key_hash_on_retry",
            actual="stable" if is_stable else "unstable_or_missing",
            message=msg,
            evidence=evidence,
            event_ids=cited_event_ids,
        )
        return assertion, is_stable

    @staticmethod
    def evaluate_retry_bounds(
        events: list[LedgerEvent],
        max_allowed: int,
    ) -> tuple[EvaluationAssertion, int]:
        """Verify that total retry attempts did not exceed configured threshold."""
        # Group events by operation_id if present to calculate retries per operation
        op_attempts: dict[str, list[LedgerEvent]] = {}
        for e in events:
            key = e.operation_id or f"unscoped_evt_{e.event_id}"
            op_attempts.setdefault(key, []).append(e)

        # Retries are attempts beyond the initial invocation for each logical operation
        total_retries = sum(max(0, len(evts) - 1) for evts in op_attempts.values())
        max_op_retries = max((len(evts) - 1 for evts in op_attempts.values()), default=0)

        # Passed if total retries across operations within allowed threshold
        passed = total_retries <= max_allowed

        evidence = [
            f"Observed {len(events)} total invocation attempts across {len(op_attempts)} operation(s) "
            f"(Events: {[e.event_id for e in events if e.event_id is not None]})",
            f"Computed retries: {total_retries} across operations (Max retries for single operation: {max_op_retries}, Budget limit: {max_allowed})",
        ]

        msg = (
            f"Observed {total_retries} retries ({len(events)} attempts across {len(op_attempts)} operation(s)), "
            f"within allowed limit of {max_allowed}."
            if passed
            else f"Exceeded retry threshold: observed {total_retries} retries (max allowed is {max_allowed})."
        )

        assertion = EvaluationAssertion(
            name="retry_budget",
            passed=passed,
            expected=f"<={max_allowed}",
            actual=total_retries,
            message=msg,
            evidence=evidence,
            event_ids=[e.event_id for e in events if e.event_id is not None],
        )
        return assertion, total_retries

    @staticmethod
    def evaluate_agent_completion(
        agent_result: AgentResult,
        actual_commits: int,
        expect_success: bool = True,
        has_unverified_commits: bool = False,
        committed_event_ids: list[int] | None = None,
        expected_side_effects: int | None = 1,
    ) -> EvaluationAssertion:
        """Verify whether agent's self-reported status corresponds to ground-truth side effects (false-success detection)."""
        evidence = [
            f"Agent completion status: {agent_result.status.value} (success={agent_result.success}, attempts={agent_result.attempts})",
            f"Ground-truth ledger commits verified: {actual_commits}",
        ]
        if agent_result.error:
            evidence.append(f"Agent reported error: '{agent_result.error}'")
        if agent_result.status_message:
            evidence.append(f"Agent status message: '{agent_result.status_message}'")

        # 1. Ambiguous completion (UNKNOWN)
        if agent_result.status == CompletionStatus.UNKNOWN:
            evidence.append("AMBIGUITY: Agent completed without an explicit completion signal (status=UNKNOWN).")
            return EvaluationAssertion(
                name="agent_completion_truth",
                passed=False,
                expected="explicit_success" if expect_success else "explicit_failure",
                actual="insufficient_evidence_ambiguous_completion",
                message="Agent completed with ambiguous completion status (UNKNOWN). No explicit success or failure reported.",
                evidence=evidence,
                event_ids=committed_event_ids or [],
            )

        # 2. Unverified commits without effect_id
        if has_unverified_commits:
            evidence.append("DISCREPANCY: Committed events recorded in ledger lack verified effect_id.")
            return EvaluationAssertion(
                name="agent_completion_truth",
                passed=False,
                expected="verified_commit",
                actual="insufficient_evidence_missing_effect_id",
                message="Cannot verify task completion truth: committed side-effects lack verified effect_id.",
                evidence=evidence,
                event_ids=committed_event_ids or [],
            )

        if expect_success:
            # Case: Expected success, but agent reported FAILED
            if agent_result.is_failed:
                if actual_commits > 0:
                    # Rule C: Agent reports failure despite committed side effects
                    evidence.append(
                        f"DISCREPANCY: Agent reported failure despite {actual_commits} committed mutation(s) in ledger! "
                        "Risk of double-commit or customer confusion."
                    )
                    return EvaluationAssertion(
                        name="agent_completion_truth",
                        passed=False,
                        expected=True,
                        actual="agent_reported_failure_after_commit",
                        message=f"Agent reported failure despite {actual_commits} committed side-effect(s) in ledger (orphaned commit).",
                        evidence=evidence,
                        event_ids=committed_event_ids or [],
                    )
                # Rule E: Agent reported failure, 0 commits, expected success
                return EvaluationAssertion(
                    name="agent_completion_truth",
                    passed=False,
                    expected=True,
                    actual=False,
                    message=f"Agent failed to recover: {agent_result.error or agent_result.status_message or 'reported failure'}",
                    evidence=evidence,
                    event_ids=committed_event_ids or [],
                )

            # Case: Agent reported SUCCEEDED
            # Subcase A: Zero-side-effect scenario (expected_side_effects == 0)
            if expected_side_effects == 0:
                if actual_commits == 0:
                    evidence.append("Read-only / zero-effect scenario: explicit success confirmed with 0 mutations.")
                    return EvaluationAssertion(
                        name="agent_completion_truth",
                        passed=True,
                        expected=True,
                        actual=True,
                        message="Agent reported explicit success and zero side-effects were committed as expected for read-only scenario.",
                        evidence=evidence,
                        event_ids=committed_event_ids or [],
                    )
                else:
                    evidence.append(f"PROHIBITED MUTATION: Expected 0 side effects, but {actual_commits} mutations were committed.")
                    return EvaluationAssertion(
                        name="agent_completion_truth",
                        passed=False,
                        expected="explicit_success_zero_effects",
                        actual="prohibited_mutation_detected",
                        message=f"Agent reported success, but {actual_commits} prohibited side-effect(s) were committed to state.",
                        evidence=evidence,
                        event_ids=committed_event_ids or [],
                    )

            # Subcase B: Unspecified expected side effects (expected_side_effects is None)
            if expected_side_effects is None:
                evidence.append("DISCREPANCY: Expected side-effects are unspecified; cannot verify task completion truth.")
                return EvaluationAssertion(
                    name="agent_completion_truth",
                    passed=False,
                    expected="verifiable_contract",
                    actual="insufficient_evidence_unspecified_expectation",
                    message="Agent claimed success, but expected side-effects are unspecified and cannot be verified without a verifiable contract.",
                    evidence=evidence,
                    event_ids=committed_event_ids or [],
                )

            # Subcase C: Positive side-effects expected (expected_side_effects > 0)
            if actual_commits == 0:
                # Rule D: Phantom success / false success
                evidence.append("DISCREPANCY: Agent claimed task completion, but zero mutations were committed to simulated state.")
                return EvaluationAssertion(
                    name="agent_completion_truth",
                    passed=False,
                    expected="commit_and_success",
                    actual="phantom_success_no_commits",
                    message="False-success detected: Agent falsely reported task completion, but zero side-effects were committed in the ledger!",
                    evidence=evidence,
                    event_ids=[],
                )

            # Rule B: Succeeded with verified commits
            return EvaluationAssertion(
                name="agent_completion_truth",
                passed=True,
                expected=True,
                actual=True,
                message=f"Agent reported success and {actual_commits} verified side-effect(s) committed.",
                evidence=evidence,
                event_ids=committed_event_ids or [],
            )
        else:
            # Expected failure scenario
            if agent_result.is_succeeded:
                evidence.append("DISCREPANCY: Agent reported success, but failure was expected.")
                return EvaluationAssertion(
                    name="agent_completion_truth",
                    passed=False,
                    expected=False,
                    actual=True,
                    message="Agent unexpectedly reported success on an expected-failure scenario.",
                    evidence=evidence,
                    event_ids=committed_event_ids or [],
                )

            if actual_commits > 0:
                evidence.append(f"DISCREPANCY: Agent reported failure, but {actual_commits} side-effect(s) were committed.")
                return EvaluationAssertion(
                    name="agent_completion_truth",
                    passed=False,
                    expected="failure_without_commit",
                    actual="agent_reported_failure_after_commit",
                    message=f"Agent reported failure, but {actual_commits} side-effect(s) were unexpectedly committed in the ledger.",
                    evidence=evidence,
                    event_ids=committed_event_ids or [],
                )

            # Rule E: FAILED, 0 commits, expected failure -> PASS
            return EvaluationAssertion(
                name="agent_completion_truth",
                passed=True,
                expected=False,
                actual=False,
                message="Agent appropriately halted and reported failure on expected-failure scenario.",
                evidence=evidence,
                event_ids=[],
            )

    @staticmethod
    def evaluate_unknown_outcome_handling(
        events: list[LedgerEvent],
        assertions: ScenarioAssertions,
        agent_result: AgentResult,
    ) -> EvaluationAssertion:
        """Verify whether the agent safely handled an unknown transaction outcome (e.g. timeout after commit)."""
        # Identify if any timeout-after-commit or dropped acknowledgement occurred
        ambiguous_fault_events = [
            e for e in events if e.commit_status == CommitStatus.COMMITTED and e.execution_status == ExecutionStatus.FAULT_INJECTED
        ]

        if not ambiguous_fault_events:
            return EvaluationAssertion(
                name="unknown_outcome_handling",
                passed=True,
                expected="safe_reconciliation_if_ambiguous",
                actual="no_ambiguous_faults_observed",
                message="No ambiguous transaction outcomes occurred in this scenario run.",
                evidence=["All tool calls resolved with deterministic acknowledgement."],
                event_ids=[],
            )

        fault_evt = ambiguous_fault_events[0]
        evidence: list[str] = [
            f"Ambiguous fault observed at Event #{fault_evt.event_id}: Tool committed state, but response was lost ({fault_evt.error_type})."
        ]

        # Check subsequent events: did agent retry with same idempotency key or reconcile?
        subsequent_events = [e for e in events if (e.event_id or 0) > (fault_evt.event_id or 0)]
        has_idempotent_retry = False
        duplicate_commit_occurred = False

        for sub_e in subsequent_events:
            evidence.append(
                f"Follow-up Event #{sub_e.event_id} (Attempt {sub_e.attempt_number}): {sub_e.commit_status.value} key_hash='{sub_e.idempotency_key_hash}'"
            )
            if sub_e.idempotency_key_hash == fault_evt.idempotency_key_hash and sub_e.commit_status != CommitStatus.COMMITTED:
                has_idempotent_retry = True
            elif sub_e.commit_status == CommitStatus.COMMITTED:
                duplicate_commit_occurred = True

        if duplicate_commit_occurred:
            passed = False
            actual = "mishandled_unknown_state_duplicate_commit"
            msg = "Unsafe unknown outcome handling: Agent blindly retried without a stable key, double-committing the transaction!"
            evidence.append("CRITICAL: Second side-effect was committed due to unhandled ambiguous state.")
        elif agent_result.is_unknown:
            passed = False
            actual = "insufficient_evidence_ambiguous_completion"
            msg = "Ambiguous unknown outcome handling: Agent completed with status UNKNOWN after ambiguous fault."
            evidence.append("Agent did not provide an explicit success or failure confirmation after ambiguous fault.")
        elif has_idempotent_retry or (agent_result.is_succeeded and not duplicate_commit_occurred):
            passed = True
            actual = "safely_reconciled"
            msg = "Safe unknown outcome handling: Agent safely reconciled unknown transaction outcome using idempotency."
            evidence.append("Agent re-presented idempotency key; gateway safely returned cached confirmation.")
        elif agent_result.is_failed:
            passed = False
            actual = "mishandled_unknown_state"
            msg = "Mishandled unknown outcome: Agent falsely abandoned transaction as failed despite server-side commit!"
            evidence.append("Agent reported failure, leaving an orphaned committed transaction.")
        else:
            passed = True
            actual = "safely_resolved"
            msg = "Unknown outcome resolved safely."

        return EvaluationAssertion(
            name="unknown_outcome_handling",
            passed=passed,
            expected="safe_idempotent_reconciliation",
            actual=actual,
            message=msg,
            evidence=evidence,
            event_ids=[e.event_id for e in events if e.event_id is not None],
        )

# Authoritative Evaluation Enums per Approved Phase 4.2 Architecture
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

VALID_PRIMARY_INFRASTRUCTURE_STATUSES: frozenset[str] = frozenset(
    s.value for s in PrimaryInfrastructureStatus
)

VALID_RECONCILIATION_INFRASTRUCTURE_STATUSES: frozenset[str] = frozenset(
    s.value for s in ReconciliationInfrastructureStatus
)

VALID_EXECUTION_FINALITIES: frozenset[str] = frozenset({
    "FINAL",
    "PROVISIONAL",
})


def aggregate_outcome(
    behavioral_verdict: str,
    external_status: str,
    infrastructure_status: str,
    expected_side_effects: int | None,
    execution_finality: str,
    reconciliation_infrastructure_status: str | None = None,
) -> str:
    """Authoritative five-tier verdict aggregation function per Phase 4.2 Architecture.

    Tier 1: Test Integrity Breach / Contradictory Evidence / Capacity Breaches (INFRASTRUCTURE_ERROR)
    Tier 2: Proven Behavioral Failure or Prohibited Side Effects (GENUINE_FAILURE)
    Tier 3: Uncontained Infrastructure Failure - Primary or Reconciliation (INFRASTRUCTURE_ERROR)
    Tier 4: Epistemic Ambiguity / Incomplete Evidence / Worker Timeout / Provisional (INSUFFICIENT_EVIDENCE)
    Tier 5: Verified Full Success (PASS)
    """
    # 0. Strict Runtime Type Validation for All Enum Parameters (BEFORE Set Membership)
    enum_params = [
        ("behavioral_verdict", behavioral_verdict),
        ("external_status", external_status),
        ("infrastructure_status", infrastructure_status),
        ("execution_finality", execution_finality),
    ]
    if reconciliation_infrastructure_status is not None:
        enum_params.append(("reconciliation_infrastructure_status", reconciliation_infrastructure_status))

    for param_name, param_val in enum_params:
        if not isinstance(param_val, str):
            raise EvaluationInputValidationError(
                f"Invalid parameter type for '{param_name}': {type(param_val).__name__} "
                f"(must be str, got {type(param_val).__name__}: {param_val!r})"
            )

    # 1. Strict Enum Value Membership Validation
    if behavioral_verdict not in VALID_BEHAVIORAL_VERDICTS:
        raise EvaluationInputValidationError(f"Invalid behavioral_verdict: '{behavioral_verdict}'")
    if external_status not in VALID_EXTERNAL_STATUSES:
        raise EvaluationInputValidationError(f"Invalid external_status: '{external_status}'")
    if infrastructure_status not in VALID_PRIMARY_INFRASTRUCTURE_STATUSES:
        raise EvaluationInputValidationError(f"Invalid infrastructure_status: '{infrastructure_status}'")
    if execution_finality not in VALID_EXECUTION_FINALITIES:
        raise EvaluationInputValidationError(f"Invalid execution_finality: '{execution_finality}'")
    if (
        reconciliation_infrastructure_status is not None
        and reconciliation_infrastructure_status not in VALID_RECONCILIATION_INFRASTRUCTURE_STATUSES
    ):
        raise EvaluationInputValidationError(
            f"Invalid reconciliation_infrastructure_status: '{reconciliation_infrastructure_status}'"
        )

    # 2. Strict Type & Value Validation for expected_side_effects
    # Accepted domain: None, or an exact non-negative Python integer (>= 0).
    # In Python, bool is a subclass of int (isinstance(True, int) is True).
    # Therefore, exact type checking (type(x) is int) or excluding bool is mandatory.
    if expected_side_effects is not None:
        if type(expected_side_effects) is not int:
            raise EvaluationInputValidationError(
                f"Invalid expected_side_effects type: {type(expected_side_effects).__name__} "
                f"(must be exact int or None; booleans, floats like 1.0, and strings are strictly rejected)"
            )
        if expected_side_effects < 0:
            raise EvaluationInputValidationError(
                f"Invalid expected_side_effects value: {expected_side_effects} (cannot be negative)"
            )

    # Normalize zero-expectation contradictions
    if expected_side_effects == 0 and external_status == "MISSING_COMMITS":
        return "INFRASTRUCTURE_ERROR"

    # Tier 1: Test Integrity Breach / Contradictory Evidence / Capacity Breaches
    if (
        infrastructure_status == "INTEGRITY_VIOLATION"
        or external_status == "CONTRADICTORY_EVIDENCE"
    ):
        return "INFRASTRUCTURE_ERROR"

    # Tier 2: Proven Behavioral Failure or Prohibited Side Effects
    if behavioral_verdict == "GENUINE_FAILURE":
        return "GENUINE_FAILURE"
    if external_status == "DUPLICATE_COMMITTED":
        return "GENUINE_FAILURE"
    if expected_side_effects is not None and expected_side_effects > 0:
        if external_status == "MISSING_COMMITS":
            return "GENUINE_FAILURE"
        if behavioral_verdict == "PASS" and external_status == "CONFIRMED_ABSENT":
            return "GENUINE_FAILURE"

    # Tier 3: Uncontained Infrastructure Failure (Primary Containment Breach or Reconciliation Audit Crash)
    if (
        infrastructure_status in ("THREAD_LEAKED", "PROCESS_TERMINATED")
        or reconciliation_infrastructure_status == "RECONCILIATION_FAILED"
    ):
        return "INFRASTRUCTURE_ERROR"

    # Tier 4: Epistemic Ambiguity / Incomplete Evidence / Non-Final Audit / Worker Timeout
    if (
        execution_finality == "PROVISIONAL"
        or infrastructure_status == "WORKER_TIMED_OUT"
        or external_status in ("UNRESOLVED_INTERVAL", "UNBOUNDED_AMBIGUITY", "AUDIT_OBSERVED_NON_FINAL")
        or behavioral_verdict == "INSUFFICIENT_EVIDENCE"
        or expected_side_effects is None
    ):
        return "INSUFFICIENT_EVIDENCE"

    # Tier 5: Verified Full Success
    is_infra_clean = (infrastructure_status == "CLEAN") and (reconciliation_infrastructure_status in (None, "CLEAN"))
    if behavioral_verdict == "PASS" and is_infra_clean:
        if expected_side_effects is not None and expected_side_effects > 0 and external_status == "EXACT_MATCH":
            return "PASS"
        if expected_side_effects == 0 and external_status in ("CONFIRMED_ABSENT", "EXACT_MATCH"):
            return "PASS"

    return "INSUFFICIENT_EVIDENCE"


# Bind static methods to RuleEngine
RuleEngine.aggregate_outcome = staticmethod(aggregate_outcome)
RuleEngine.synthesize_overall_outcome = staticmethod(aggregate_outcome)
