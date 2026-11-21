"""Targeted tests for Phase 4.2 infrastructure enums and outcome aggregation precedence."""

import sys

import pytest

from recoverybench.errors import EvaluationInputValidationError
from recoverybench.evaluator.rules import (
    VALID_BEHAVIORAL_VERDICTS,
    VALID_EXECUTION_FINALITIES,
    VALID_EXTERNAL_STATUSES,
    VALID_PRIMARY_INFRASTRUCTURE_STATUSES,
    VALID_RECONCILIATION_INFRASTRUCTURE_STATUSES,
    RuleEngine,
    aggregate_outcome,
)
from recoverybench.models import (
    PrimaryInfrastructureStatus,
    ReconciliationInfrastructureStatus,
)


class TestInfrastructureEnums:
    """Verify authoritative enum definitions and their exact members."""

    def test_primary_infrastructure_status_members(self) -> None:
        expected = {
            "CLEAN",
            "WORKER_TIMED_OUT",
            "THREAD_LEAKED",
            "PROCESS_TERMINATED",
            "INTEGRITY_VIOLATION",
        }
        actual = {s.value for s in PrimaryInfrastructureStatus}
        assert actual == expected
        assert len(PrimaryInfrastructureStatus) == 5
        assert VALID_PRIMARY_INFRASTRUCTURE_STATUSES == frozenset(expected)

    def test_reconciliation_infrastructure_status_members(self) -> None:
        expected = {
            "CLEAN",
            "RECONCILIATION_FAILED",
        }
        actual = {s.value for s in ReconciliationInfrastructureStatus}
        assert actual == expected
        assert len(ReconciliationInfrastructureStatus) == 2
        assert VALID_RECONCILIATION_INFRASTRUCTURE_STATUSES == frozenset(expected)


class TestInputTypeValidation:
    """Verify strict runtime type checking before enum set membership evaluation."""

    @pytest.mark.parametrize(
        "invalid_value",
        [
            ["PASS"],
            {"verdict": "PASS"},
            123,
            True,
            False,
            None,
        ],
    )
    def test_behavioral_verdict_rejects_non_string(self, invalid_value: object) -> None:
        with pytest.raises(EvaluationInputValidationError, match="Invalid parameter type for 'behavioral_verdict'"):
            aggregate_outcome(
                behavioral_verdict=invalid_value,  # type: ignore[arg-type]
                external_status="EXACT_MATCH",
                infrastructure_status="CLEAN",
                expected_side_effects=1,
                execution_finality="FINAL",
            )

    @pytest.mark.parametrize(
        "invalid_value",
        [
            ["EXACT_MATCH"],
            {"status": "EXACT_MATCH"},
            123,
            True,
            False,
            None,
        ],
    )
    def test_external_status_rejects_non_string(self, invalid_value: object) -> None:
        with pytest.raises(EvaluationInputValidationError, match="Invalid parameter type for 'external_status'"):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status=invalid_value,  # type: ignore[arg-type]
                infrastructure_status="CLEAN",
                expected_side_effects=1,
                execution_finality="FINAL",
            )

    @pytest.mark.parametrize(
        "invalid_value",
        [
            ["CLEAN"],
            {"status": "CLEAN"},
            123,
            True,
            False,
            None,
        ],
    )
    def test_infrastructure_status_rejects_non_string(self, invalid_value: object) -> None:
        with pytest.raises(EvaluationInputValidationError, match="Invalid parameter type for 'infrastructure_status'"):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status="EXACT_MATCH",
                infrastructure_status=invalid_value,  # type: ignore[arg-type]
                expected_side_effects=1,
                execution_finality="FINAL",
            )

    @pytest.mark.parametrize(
        "invalid_value",
        [
            ["FINAL"],
            {"finality": "FINAL"},
            123,
            True,
            False,
            None,
        ],
    )
    def test_execution_finality_rejects_non_string(self, invalid_value: object) -> None:
        with pytest.raises(EvaluationInputValidationError, match="Invalid parameter type for 'execution_finality'"):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status="EXACT_MATCH",
                infrastructure_status="CLEAN",
                expected_side_effects=1,
                execution_finality=invalid_value,  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize(
        "invalid_value",
        [
            ["CLEAN"],
            {"status": "CLEAN"},
            123,
            True,
            False,
        ],
    )
    def test_reconciliation_status_rejects_non_string_non_none(self, invalid_value: object) -> None:
        with pytest.raises(
            EvaluationInputValidationError, match="Invalid parameter type for 'reconciliation_infrastructure_status'"
        ):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status="EXACT_MATCH",
                infrastructure_status="CLEAN",
                expected_side_effects=1,
                execution_finality="FINAL",
                reconciliation_infrastructure_status=invalid_value,  # type: ignore[arg-type]
            )

    def test_reconciliation_status_accepts_none(self) -> None:
        result = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
            reconciliation_infrastructure_status=None,
        )
        assert result == "PASS"


class TestEnumMembershipValidation:
    """Verify strict rejection of unrecognized string values across all enum domains."""

    def test_invalid_behavioral_verdict_string(self) -> None:
        with pytest.raises(EvaluationInputValidationError, match="Invalid behavioral_verdict: 'PROVISIONAL'"):
            aggregate_outcome(
                behavioral_verdict="PROVISIONAL",
                external_status="EXACT_MATCH",
                infrastructure_status="CLEAN",
                expected_side_effects=1,
                execution_finality="FINAL",
            )

    def test_invalid_external_status_string(self) -> None:
        with pytest.raises(EvaluationInputValidationError, match="Invalid external_status: 'UNKNOWN_STATUS'"):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status="UNKNOWN_STATUS",
                infrastructure_status="CLEAN",
                expected_side_effects=1,
                execution_finality="FINAL",
            )

    def test_primary_infrastructure_rejects_reconciliation_failed(self) -> None:
        with pytest.raises(
            EvaluationInputValidationError, match="Invalid infrastructure_status: 'RECONCILIATION_FAILED'"
        ):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status="EXACT_MATCH",
                infrastructure_status="RECONCILIATION_FAILED",
                expected_side_effects=1,
                execution_finality="FINAL",
            )

    def test_reconciliation_infrastructure_rejects_primary_containment_statuses(self) -> None:
        for invalid_recon in ("PROCESS_TERMINATED", "THREAD_LEAKED", "INTEGRITY_VIOLATION", "WORKER_TIMED_OUT"):
            with pytest.raises(
                EvaluationInputValidationError,
                match=f"Invalid reconciliation_infrastructure_status: '{invalid_recon}'",
            ):
                aggregate_outcome(
                    behavioral_verdict="PASS",
                    external_status="EXACT_MATCH",
                    infrastructure_status="CLEAN",
                    expected_side_effects=1,
                    execution_finality="FINAL",
                    reconciliation_infrastructure_status=invalid_recon,
                )

    def test_invalid_execution_finality_string(self) -> None:
        with pytest.raises(EvaluationInputValidationError, match="Invalid execution_finality: 'PENDING'"):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status="EXACT_MATCH",
                infrastructure_status="CLEAN",
                expected_side_effects=1,
                execution_finality="PENDING",
            )


class TestExpectedSideEffectsValidation:
    """Verify strict type and boundary checking for expected_side_effects."""

    @pytest.mark.parametrize("valid_exp", [None, 0, 1, 2, 100, sys.maxsize])
    def test_valid_expected_side_effects(self, valid_exp: int | None) -> None:
        result = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="CONFIRMED_ABSENT",
            infrastructure_status="CLEAN",
            expected_side_effects=valid_exp,
            execution_finality="FINAL",
        )
        assert result in ("PASS", "GENUINE_FAILURE", "INSUFFICIENT_EVIDENCE")

    @pytest.mark.parametrize(
        "invalid_exp",
        [
            True,  # bool subclass of int
            False,
            1.0,  # float
            0.0,
            "1",  # string
            "0",
            [1],  # list
            {"count": 1},  # dict
        ],
    )
    def test_rejects_non_exact_int_types(self, invalid_exp: object) -> None:
        with pytest.raises(EvaluationInputValidationError, match="Invalid expected_side_effects type:"):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status="EXACT_MATCH",
                infrastructure_status="CLEAN",
                expected_side_effects=invalid_exp,  # type: ignore[arg-type]
                execution_finality="FINAL",
            )

    @pytest.mark.parametrize("negative_exp", [-1, -2, -100])
    def test_rejects_negative_integers(self, negative_exp: int) -> None:
        with pytest.raises(EvaluationInputValidationError, match="cannot be negative"):
            aggregate_outcome(
                behavioral_verdict="PASS",
                external_status="EXACT_MATCH",
                infrastructure_status="CLEAN",
                expected_side_effects=negative_exp,
                execution_finality="FINAL",
            )


class TestZeroExpectationContradiction:
    """Verify detection of zero-expectation contradictions."""

    def test_zero_expected_with_missing_commits_returns_infrastructure_error(self) -> None:
        result = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="MISSING_COMMITS",
            infrastructure_status="CLEAN",
            expected_side_effects=0,
            execution_finality="FINAL",
        )
        assert result == "INFRASTRUCTURE_ERROR"


class TestPrecedenceTiers:
    """Verify the approved 5-tier evaluation precedence."""

    def test_tier_1_integrity_violation_overrides_all(self) -> None:
        # Capacity breach / falsified declaration mapped to INTEGRITY_VIOLATION
        for bv in VALID_BEHAVIORAL_VERDICTS:
            result = aggregate_outcome(
                behavioral_verdict=bv,
                external_status="UNBOUNDED_AMBIGUITY",
                infrastructure_status="INTEGRITY_VIOLATION",
                expected_side_effects=None,
                execution_finality="FINAL",
            )
            assert result == "INFRASTRUCTURE_ERROR"

    def test_tier_1_contradictory_evidence_overrides_all(self) -> None:
        result = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="CONTRADICTORY_EVIDENCE",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert result == "INFRASTRUCTURE_ERROR"

    def test_tier_2_behavioral_failure_immutability(self) -> None:
        # Behavioral GENUINE_FAILURE overrides Tier 3 infrastructure failures and Tier 4 ambiguity
        result = aggregate_outcome(
            behavioral_verdict="GENUINE_FAILURE",
            external_status="EXACT_MATCH",
            infrastructure_status="PROCESS_TERMINATED",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert result == "GENUINE_FAILURE"

    def test_tier_2_prohibited_side_effects(self) -> None:
        # Duplicate committed
        result_dup = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="DUPLICATE_COMMITTED",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert result_dup == "GENUINE_FAILURE"

        # Missing commits when expected > 0
        result_missing = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="MISSING_COMMITS",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert result_missing == "GENUINE_FAILURE"

        # Phantom success (PASS with CONFIRMED_ABSENT when expected > 0)
        result_phantom = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="CONFIRMED_ABSENT",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert result_phantom == "GENUINE_FAILURE"

    def test_tier_3_primary_containment_breach(self) -> None:
        # Thread leaked
        res_leak = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="THREAD_LEAKED",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert res_leak == "INFRASTRUCTURE_ERROR"

        # Process terminated
        res_proc = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="PROCESS_TERMINATED",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert res_proc == "INFRASTRUCTURE_ERROR"

    def test_tier_3_reconciliation_failure(self) -> None:
        res_recon = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
            reconciliation_infrastructure_status="RECONCILIATION_FAILED",
        )
        assert res_recon == "INFRASTRUCTURE_ERROR"

    def test_tier_3_t09_primary_failure_retained_despite_clean_reconciliation(self) -> None:
        # T-09: Uncontained worker killed (PROCESS_TERMINATED); reconciliation proves commit committed (CLEAN)
        # Outcome MUST remain INFRASTRUCTURE_ERROR (Invariant 6 non-erasure)
        result = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="PROCESS_TERMINATED",
            expected_side_effects=1,
            execution_finality="FINAL",
            reconciliation_infrastructure_status="CLEAN",
        )
        assert result == "INFRASTRUCTURE_ERROR"

    def test_tier_3_t20_compound_failure(self) -> None:
        # T-20: Primary process terminated AND reconciliation audit crashes
        result = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="UNRESOLVED_INTERVAL",
            infrastructure_status="PROCESS_TERMINATED",
            expected_side_effects=1,
            execution_finality="FINAL",
            reconciliation_infrastructure_status="RECONCILIATION_FAILED",
        )
        assert result == "INFRASTRUCTURE_ERROR"

    def test_tier_4_worker_timeout_in_tier_4_not_tier_3(self) -> None:
        # WORKER_TIMED_OUT is epistemic ambiguity (Tier 4), NOT a physical containment failure (Tier 3)
        result = aggregate_outcome(
            behavioral_verdict="INSUFFICIENT_EVIDENCE",
            external_status="UNRESOLVED_INTERVAL",
            infrastructure_status="WORKER_TIMED_OUT",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert result == "INSUFFICIENT_EVIDENCE"

    def test_tier_4_epistemic_ambiguity(self) -> None:
        # Provisional execution
        res_prov = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="PROVISIONAL",
        )
        assert res_prov == "INSUFFICIENT_EVIDENCE"

        # Unresolved interval
        res_unresolved = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="UNRESOLVED_INTERVAL",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert res_unresolved == "INSUFFICIENT_EVIDENCE"

        # Unbounded ambiguity
        res_unbounded = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="UNBOUNDED_AMBIGUITY",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert res_unbounded == "INSUFFICIENT_EVIDENCE"

        # Audit observed non final
        res_nonfinal = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="AUDIT_OBSERVED_NON_FINAL",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert res_nonfinal == "INSUFFICIENT_EVIDENCE"

        # Omitted expected side effects
        res_none_exp = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="CLEAN",
            expected_side_effects=None,
            execution_finality="FINAL",
        )
        assert res_none_exp == "INSUFFICIENT_EVIDENCE"

    def test_tier_5_verified_full_success(self) -> None:
        # Positive expectation exact match
        res_pos = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert res_pos == "PASS"

        # Zero expectation confirmed absent
        res_zero_absent = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="CONFIRMED_ABSENT",
            infrastructure_status="CLEAN",
            expected_side_effects=0,
            execution_finality="FINAL",
        )
        assert res_zero_absent == "PASS"

        # Zero expectation exact match
        res_zero_exact = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="CLEAN",
            expected_side_effects=0,
            execution_finality="FINAL",
        )
        assert res_zero_exact == "PASS"

        # Clean reconciliation preserves success (T-08)
        res_recon_clean = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status="EXACT_MATCH",
            infrastructure_status="CLEAN",
            expected_side_effects=1,
            execution_finality="FINAL",
            reconciliation_infrastructure_status="CLEAN",
        )
        assert res_recon_clean == "PASS"

    def test_rule_engine_aliases(self) -> None:
        assert RuleEngine.aggregate_outcome is aggregate_outcome
        assert RuleEngine.synthesize_overall_outcome is aggregate_outcome


def expected_outcome_oracle(
    behavioral_verdict: str,
    external_status: str,
    infrastructure_status: str,
    expected_side_effects: int | None,
    execution_finality: str,
    reconciliation_infrastructure_status: str | None = None,
) -> str:
    """Independent expected-outcome oracle implementing the approved Phase 4.2 five-tier precedence hierarchy.

    Zero-Expectation Contradiction: expected_side_effects == 0 and external_status == 'MISSING_COMMITS' -> INFRASTRUCTURE_ERROR
    Tier 1: primary INTEGRITY_VIOLATION or external CONTRADICTORY_EVIDENCE -> INFRASTRUCTURE_ERROR
    Tier 2: behavioral GENUINE_FAILURE, DUPLICATE_COMMITTED, MISSING_COMMITS (if expected > 0), or phantom success -> GENUINE_FAILURE
    Tier 3: primary THREAD_LEAKED / PROCESS_TERMINATED, or reconciliation RECONCILIATION_FAILED -> INFRASTRUCTURE_ERROR
    Tier 4: PROVISIONAL finality, WORKER_TIMED_OUT, UNRESOLVED_INTERVAL, UNBOUNDED_AMBIGUITY, AUDIT_OBSERVED_NON_FINAL,
            INSUFFICIENT_EVIDENCE behavioral verdict, or None expected_side_effects -> INSUFFICIENT_EVIDENCE
    Tier 5: Clean PASS with EXACT_MATCH (expected > 0) or CONFIRMED_ABSENT / EXACT_MATCH (expected == 0) -> PASS
    Fail-closed fallback -> INSUFFICIENT_EVIDENCE
    """
    # 0. Zero-expectation contradiction normalization
    if expected_side_effects == 0 and external_status == "MISSING_COMMITS":
        return "INFRASTRUCTURE_ERROR"

    # Tier 1: Test Integrity Breach / Contradictory Evidence
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

    # Tier 3: Uncontained Infrastructure Failure (Primary or Reconciliation)
    if (
        infrastructure_status in ("THREAD_LEAKED", "PROCESS_TERMINATED")
        or reconciliation_infrastructure_status == "RECONCILIATION_FAILED"
    ):
        return "INFRASTRUCTURE_ERROR"

    # Tier 4: Epistemic Ambiguity / Incomplete Evidence / Worker Timeout / Provisional
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


class TestPartitionedCartesianSpace:
    """Verify that all 1,440 partitioned representative cases evaluate deterministically against the analytical oracle."""

    def test_primary_partition_720_cases(self) -> None:
        exp_classes = [0, 1, None]
        count = 0
        for bv in VALID_BEHAVIORAL_VERDICTS:
            for es in VALID_EXTERNAL_STATUSES:
                for pis in VALID_PRIMARY_INFRASTRUCTURE_STATUSES:
                    for exp in exp_classes:
                        for fin in VALID_EXECUTION_FINALITIES:
                            expected = expected_outcome_oracle(
                                behavioral_verdict=bv,
                                external_status=es,
                                infrastructure_status=pis,
                                expected_side_effects=exp,
                                execution_finality=fin,
                                reconciliation_infrastructure_status=None,
                            )
                            actual = aggregate_outcome(
                                behavioral_verdict=bv,
                                external_status=es,
                                infrastructure_status=pis,
                                expected_side_effects=exp,
                                execution_finality=fin,
                                reconciliation_infrastructure_status=None,
                            )
                            assert actual == expected, (
                                f"Oracle mismatch for primary combination ({bv}, {es}, {pis}, {exp}, {fin}): "
                                f"actual '{actual}' != expected '{expected}'"
                            )
                            count += 1
        assert count == 720

    def test_reconciled_partition_720_cases(self) -> None:
        exp_classes = [0, 1, None]
        count = 0
        for bv in VALID_BEHAVIORAL_VERDICTS:
            for es in VALID_EXTERNAL_STATUSES:
                for pis in VALID_PRIMARY_INFRASTRUCTURE_STATUSES:
                    for exp in exp_classes:
                        for ris in VALID_RECONCILIATION_INFRASTRUCTURE_STATUSES:
                            expected = expected_outcome_oracle(
                                behavioral_verdict=bv,
                                external_status=es,
                                infrastructure_status=pis,
                                expected_side_effects=exp,
                                execution_finality="FINAL",
                                reconciliation_infrastructure_status=ris,
                            )
                            actual = aggregate_outcome(
                                behavioral_verdict=bv,
                                external_status=es,
                                infrastructure_status=pis,
                                expected_side_effects=exp,
                                execution_finality="FINAL",
                                reconciliation_infrastructure_status=ris,
                            )
                            assert actual == expected, (
                                f"Oracle mismatch for reconciled combination ({bv}, {es}, {pis}, {exp}, FINAL, {ris}): "
                                f"actual '{actual}' != expected '{expected}'"
                            )
                            count += 1
        assert count == 720
