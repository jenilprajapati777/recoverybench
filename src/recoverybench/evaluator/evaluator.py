"""Recovery evaluator orchestrating rule evaluations, evidence gathering, and verdict assignment."""

from recoverybench.agent.base import AgentResult
from recoverybench.evaluator.rules import RuleEngine
from recoverybench.models import (
    CommitStatus,
    EvaluationAssertion,
    EvaluationResult,
    EvaluationVerdict,
    LedgerEvent,
)
from recoverybench.scenarios.schema import ScenarioAssertions


class RecoveryEvaluator:
    """Evaluates agent recovery execution against ground-truth ledger events with evidence tracking."""

    def evaluate(
        self,
        events: list[LedgerEvent],
        assertions: ScenarioAssertions,
        agent_result: AgentResult,
        duration_ms: float = 0.0,
    ) -> EvaluationResult:
        """Evaluate ledger events and agent result against scenario assertions."""
        if not events:
            # 1. Expected clean failure with 0 side effects
            if assertions.expected_side_effects == 0 and not assertions.expect_agent_success and agent_result.is_failed:
                return EvaluationResult(
                    verdict=EvaluationVerdict.PASS,
                    passed=True,
                    assertions=[
                        EvaluationAssertion(
                            name="agent_completion_truth",
                            passed=True,
                            expected=False,
                            actual=False,
                            message="Agent appropriately halted and reported failure on expected-failure scenario with 0 side-effects.",
                            evidence=["Zero ledger events recorded as expected for clean failure."],
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
                    failure_reasons=[],
                    evidence_summary={"agent_completion_truth": ["Clean expected failure with no mutations."]},
                )

            # 2. Read-only / zero-side-effect explicit success with empty ledger (Condition A)
            if assertions.expected_side_effects == 0 and assertions.expect_agent_success and agent_result.is_succeeded:
                return EvaluationResult(
                    verdict=EvaluationVerdict.PASS,
                    passed=True,
                    assertions=[
                        EvaluationAssertion(
                            name="agent_completion_truth",
                            passed=True,
                            expected=True,
                            actual=True,
                            message="Agent reported explicit success with zero side-effects as expected for read-only scenario.",
                            evidence=["Zero ledger events recorded; no prohibited side effects occurred."],
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
                    failure_reasons=[],
                    evidence_summary={"agent_completion_truth": ["Explicit success with 0 side effects as expected."]},
                )

            # 3. Phantom Success (False Success) with empty ledger (Rule D & Condition B)
            if agent_result.is_succeeded and assertions.expected_side_effects is not None and assertions.expected_side_effects > 0:
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
                )

            # 4. Agent failed when expected to succeed (Rule E) -> GENUINE_FAILURE
            if assertions.expect_agent_success and agent_result.is_failed:
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
                )

            # 5. Empty ledger default (Rule G, Condition C, Condition D) -> INSUFFICIENT_EVIDENCE
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
            )

        # Check for unverified commits
        committed_events = [e for e in events if e.commit_status == CommitStatus.COMMITTED]
        committed_ids = [e.event_id for e in committed_events if e.event_id is not None]
        has_unverified_commits = any(
            not e.effect_id or not e.effect_id.strip() for e in committed_events
        )

        # 1. Evaluate side effects & duplicate/missing detection
        effects_assertion, actual_commits, duplicate_count = RuleEngine.evaluate_side_effects(
            events, assertions
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

        if passed:
            verdict = EvaluationVerdict.PASS
        elif any(a.actual in insufficient_markers for a in all_assertions):
            # Preserve genuine failure classifications when independent evidence proves a violation:
            # - Idempotency key stability violation
            # - Retry budget exceeded
            # - Duplicate side-effects proven among verified effects
            # - Agent explicitly reported failure after a commit (Rule C)
            # - Agent falsely reported success without commit (Rule D)
            # - Missing expected effects or mishandled unknown states
            has_independent_violation = (
                not idempotency_assertion.passed
                or not retry_assertion.passed
                or (
                    not completion_assertion.passed
                    and completion_assertion.actual not in insufficient_markers
                )
                or duplicate_count > assertions.max_duplicate_effects
                or (
                    not effects_assertion.passed
                    and effects_assertion.actual not in insufficient_markers
                )
                or (
                    not unknown_outcome_assertion.passed
                    and unknown_outcome_assertion.actual not in insufficient_markers
                )
            )
            if has_independent_violation:
                verdict = EvaluationVerdict.GENUINE_FAILURE
            else:
                verdict = EvaluationVerdict.INSUFFICIENT_EVIDENCE
        else:
            verdict = EvaluationVerdict.GENUINE_FAILURE

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
        )
