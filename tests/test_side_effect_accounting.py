"""Comprehensive tests for two-layer side-effect accounting, capacity provenance, and safe idempotency correlation."""

import math
import uuid
from typing import Any, cast

import pytest

from recoverybench.agent.base import AgentResult, CompletionStatus
from recoverybench.errors import (
    CapacityBreachIntegrityError,
    EvaluationInputValidationError,
    IdempotencyCorrelationError,
)
from recoverybench.evaluator.accounting import (
    TwoLayerEffectAccountingEngine,
    derive_external_status,
)
from recoverybench.evaluator.evaluator import RecoveryEvaluator
from recoverybench.evaluator.rules import RuleEngine, aggregate_outcome
from recoverybench.models import (
    AbsentStatus,
    AuthoritativeFinalityProof,
    CapacityProvenanceTier,
    CommitStatus,
    EvaluationVerdict,
    ExecutionStatus,
    HarnessAuditAuthority,
    HarnessOrchestratorAuthority,
    HarnessRuntimeAttestation,
    HarnessRuntimeAuthority,
    IdempotencyContract,
    IdempotencyGuarantee,
    IdempotencyScope,
    InventoryStatus,
    LedgerEvent,
    PrimaryInfrastructureStatus,
    ReconciliationAuditEvidence,
    SideEffectBounds,
    compute_evaluation_digest,
)
from recoverybench.scenarios.schema import ScenarioAssertions
from recoverybench.tools.base import ToolRegistry, ToolResult, ToolService

TEST_RUNTIME_AUTH = HarnessRuntimeAuthority()
TEST_AUDIT_AUTH = HarnessAuditAuthority()
TEST_ORCH_AUTH = HarnessOrchestratorAuthority()

_orig_calculate = TwoLayerEffectAccountingEngine.calculate


def _test_calculate(*args: Any, **kwargs: Any) -> Any:
    if "runtime_authority" not in kwargs:
        kwargs["runtime_authority"] = TEST_RUNTIME_AUTH
    if "audit_authority" not in kwargs:
        kwargs["audit_authority"] = TEST_AUDIT_AUTH
    return _orig_calculate(*args, **kwargs)


TwoLayerEffectAccountingEngine.calculate = _test_calculate  # type: ignore[method-assign]


def make_c3_meta(
    events: list[LedgerEvent],
    tool_name: str,
    max_capacity: int = 1,
    runtime_guarantee: str = 'RUNTIME_UNIQUE_CONSTRAINT',
    **extra: Any,
) -> dict[str, Any]:
    run_id = events[0].run_id if events else 'default_test_run'
    att = TEST_RUNTIME_AUTH.attest(
        run_id=run_id,
        tool_name=tool_name,
        max_capacity=max_capacity,
        runtime_guarantee=runtime_guarantee,
    )
    meta: dict[str, Any] = {'harness_attestation': att, **extra}
    return {
        tool_name: meta,
    }


# ==============================================================================
# 1. Numerical Bounds Validation Tests
# ==============================================================================


class TestSideEffectBoundsValidation:
    """Verify strict validation and behavior of SideEffectBounds."""

    def test_valid_finite_bounds(self) -> None:
        bounds = SideEffectBounds(s_min=1, s_max=2)
        assert bounds.s_min == 1
        assert bounds.s_max == 2
        assert not bounds.is_exact
        assert not bounds.is_zero
        assert not bounds.is_unbounded
        assert bounds.to_list() == [1, 2]
        assert bounds.to_tuple() == (1, 2)
        assert bounds.contains(1)
        assert bounds.contains(2)
        assert not bounds.contains(0)
        assert not bounds.contains(3)

    def test_valid_exact_bounds(self) -> None:
        bounds = SideEffectBounds(s_min=1, s_max=1)
        assert bounds.is_exact
        assert not bounds.is_zero
        assert not bounds.is_unbounded
        assert bounds.contains(1)
        assert not bounds.contains(2)

    def test_valid_zero_bounds(self) -> None:
        bounds = SideEffectBounds(s_min=0, s_max=0)
        assert bounds.is_exact
        assert bounds.is_zero
        assert not bounds.is_unbounded
        assert bounds.contains(0)
        assert not bounds.contains(1)

    def test_valid_infinite_bounds(self) -> None:
        bounds = SideEffectBounds(s_min=0, s_max=math.inf)
        assert not bounds.is_exact
        assert not bounds.is_zero
        assert bounds.is_unbounded
        assert bounds.to_list() == [0, "infinity"]
        assert bounds.contains(0)
        assert bounds.contains(100)
        assert bounds.contains(1000000)
        assert not bounds.contains(-1)

    def test_from_tuple_or_list(self) -> None:
        b1 = SideEffectBounds.model_validate((1, 3))
        assert b1.s_min == 1
        assert b1.s_max == 3
        b2 = SideEffectBounds.model_validate([0, "infinity"])
        assert b2.s_min == 0
        assert b2.is_unbounded

    @pytest.mark.parametrize("invalid_min", [True, False, 1.0, 0.0, -1, "1", [1]])
    def test_rejects_invalid_s_min(self, invalid_min: Any) -> None:
        with pytest.raises(EvaluationInputValidationError):
            SideEffectBounds(s_min=invalid_min, s_max=10)

    @pytest.mark.parametrize("invalid_max", [True, False, 1.0, 0.0, -1, "1", [1]])
    def test_rejects_invalid_s_max(self, invalid_max: Any) -> None:
        with pytest.raises(EvaluationInputValidationError):
            SideEffectBounds(s_min=0, s_max=invalid_max)

    def test_rejects_inverted_bounds(self) -> None:
        with pytest.raises(EvaluationInputValidationError, match="cannot exceed"):
            SideEffectBounds(s_min=3, s_max=2)

    def test_contains_rejects_booleans_and_non_ints(self) -> None:
        bounds = SideEffectBounds(s_min=0, s_max=5)
        assert not bounds.contains(True)
        assert not bounds.contains(False)
        assert not bounds.contains(1.5)
        assert not bounds.contains("2")


# ==============================================================================
# 2. Section 3.6: The Six Canonical Worked Examples
# ==============================================================================


class TestSection36CanonicalExamples:
    """Implement all six canonical worked examples from architecture Section 3.6."""

    def test_example_1_commit_followed_by_exception(self) -> None:
        """Example 1: Commit followed by exception.

        Attempt a1: M(a1)=1 (Tier C3). Commits eff_01, then raises RuntimeError on telemetry.
        c(a1)=1, M(a1)=1 -> Saturated -> u(a1) = 0. Bounds: [1, 1].
        If retried as a2 without idempotency (u(a2)=1): Bounds: [1, 2].
        """
        events_a1 = [
            LedgerEvent(
                event_id=1,
                run_id="run_ex1",
                scenario_id="ex1",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_1",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_1",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                error_type="RuntimeError",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]
        tool_meta = make_c3_meta(events_a1, "payment_svc", max_capacity=1)
        res_a1 = TwoLayerEffectAccountingEngine.calculate(events_a1, expected_side_effects=1, tool_metadata=tool_meta)
        assert res_a1.bounds.s_min == 1
        assert res_a1.bounds.s_max == 1
        assert res_a1.bounds.is_exact
        assert res_a1.external_status == "EXACT_MATCH"

        # Retried as a2 without verified idempotency (u(a2)=1)
        events_retry = events_a1 + [
            LedgerEvent(
                event_id=2,
                run_id="run_ex1",
                scenario_id="ex1",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_1",
                attempt_id="a2",
                attempt_number=2,
                effect_id=None,
                request_fingerprint="fp_1",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="105.0",
                details={
                    "capacity_provenance": "TIER_C3",
                    "max_capacity": 1,
                    "verified_service_deduplication": False,
                },
            )
        ]
        res_retry = TwoLayerEffectAccountingEngine.calculate(events_retry, expected_side_effects=1, tool_metadata=tool_meta)
        assert res_retry.bounds.s_min == 1
        assert res_retry.bounds.s_max == 2
        assert res_retry.external_status == "UNRESOLVED_INTERVAL"

    def test_example_2_partial_multi_effect_commit(self) -> None:
        """Example 2: Partial multi-effect commit.

        Attempt a1: Batch insert of 3 items (M(a1)=3, Tier C3).
        Commits 2 records (eff_01, eff_02), then database deadlock aborts.
        c(a1)=2, u(a1)=1. Bounds: [2, 3].
        """
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_ex2",
                scenario_id="ex2",
                tool_name="batch_db",
                action_type="insert_batch",
                operation_id="op_batch",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_batch",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 3},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_ex2",
                scenario_id="ex2",
                tool_name="batch_db",
                action_type="insert_batch",
                operation_id="op_batch",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_02",
                request_fingerprint="fp_batch",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.1",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 3},
            ),
            LedgerEvent(
                event_id=3,
                run_id="run_ex2",
                scenario_id="ex2",
                tool_name="batch_db",
                action_type="insert_batch",
                operation_id="op_batch",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_batch",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.2",
                error_type="DeadlockDetectedError",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 3},
            ),
        ]
        tool_meta = make_c3_meta(events, "batch_db", max_capacity=3)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=3, tool_metadata=tool_meta)
        assert res.bounds.s_min == 2
        assert res.bounds.s_max == 3
        assert res.external_status == "UNRESOLVED_INTERVAL"

    def test_example_3_timeout_with_unresolved_worker(self) -> None:
        """Example 3: Timeout with unresolved worker.

        Prior commit a0 committed eff_00.
        Attempt a1: M(a1)=1. Worker blocks; timeout 50ms expires.
        For a1: c(a1)=0, u(a1)=1.
        Bounds: S in [1, 2]. Unique attempt_id prevents double-counting the worker.
        """
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_ex3",
                scenario_id="ex3",
                tool_name="order_service",
                action_type="create",
                operation_id="op_prior",
                attempt_id="a0",
                attempt_number=1,
                effect_id="eff_00",
                request_fingerprint="fp_prior",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_ex3",
                scenario_id="ex3",
                tool_name="order_service",
                action_type="create",
                operation_id="op_new",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_new",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="101.0",
                error_type="ToolTimeoutError",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "order_service", max_capacity=1)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=tool_meta)
        assert res.bounds.s_min == 1
        assert res.bounds.s_max == 2
        assert res.external_status == "UNRESOLVED_INTERVAL"

    def test_example_4_unverified_non_mutating_declaration(self) -> None:
        """Example 4: Non-mutating declaration without proof.

        Attempt a1: @mock_tool(mutates=False) (Tier C1). Raises ConnectionError.
        Absent(a1) = UNVERIFIED, M(a1) = infinity -> u(a1) = infinity.
        Bounds: [0, infinity) -> UNBOUNDED_AMBIGUITY.
        """
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_ex4",
                scenario_id="ex4",
                tool_name="readonly_query",
                action_type="query",
                operation_id="op_read",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_query",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                error_type="ConnectionError",
                details={"capacity_provenance": "TIER_C1", "mutates": False},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        vec = res.attempts["a1"]
        assert vec.capacity_provenance == CapacityProvenanceTier.TIER_C1
        assert vec.absent_status == AbsentStatus.UNVERIFIED
        assert vec.inventory_status == InventoryStatus.UNKNOWN
        assert math.isinf(vec.max_capacity)
        assert math.isinf(vec.unresolved_capacity)
        assert math.isinf(res.unresolved_by_attempt["a1"])
        assert res.bounds.s_min == 0
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_example_5_verified_absence(self) -> None:
        """Example 5: Verified absence.

        Attempt a1: input validation rejects negative amount locally before I/O.
        Absent(a1) = TRUE, c(a1)=0, u(a1)=0.
        Bounds: [0, 0] -> CONFIRMED_ABSENT.
        """
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_ex5",
                scenario_id="ex5",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_val",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_neg",
                execution_status=ExecutionStatus.FAULT_INJECTED,
                commit_status=CommitStatus.ABORTED,
                simulated_timestamp="100.0",
                error_type="ValueError",
                details={
                    "capacity_provenance": "TIER_C3",
                    "max_capacity": 1,
                    "verified_absence": True,
                    "absent_status": "TRUE",
                },
            )
        ]
        tool_meta = make_c3_meta(events, "payment_svc", max_capacity=1)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0, tool_metadata=tool_meta)
        assert res.bounds.s_min == 0
        assert res.bounds.s_max == 0
        assert res.bounds.is_zero
        assert res.external_status == "CONFIRMED_ABSENT"

    def test_example_6_overlapping_worker_and_reconciliation(self) -> None:
        """Example 6: Multiple attempts with overlapping worker & reconciliation.

        Attempt a1 (M=1) times out; thread leaked (u(a1)=1).
        Attempt a2 retries with same key, returns cached eff_01 (c(a2)=1, u(a2)=0).
        Pre-reconciliation: S in [1, 2].
        Post-hoc reconciliation audits database, confirms exactly 1 record eff_01 exists
        and zero pending writes remain (u(a1)=0).
        Post-reconciliation: S in [1, 1].
        """
        # Pre-reconciliation events
        events_pre = [
            LedgerEvent(
                event_id=1,
                run_id="run_ex6",
                scenario_id="ex6",
                tool_name="order_svc",
                action_type="create",
                operation_id="op_6",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_6",
                idempotency_key_hash="key_hash_6",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_ex6",
                scenario_id="ex6",
                tool_name="order_svc",
                action_type="create",
                operation_id="op_6",
                attempt_id="a2",
                attempt_number=2,
                effect_id="eff_01",
                request_fingerprint="fp_6",
                idempotency_key_hash="key_hash_6",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="102.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events_pre, "order_svc", max_capacity=1)
        res_pre = TwoLayerEffectAccountingEngine.calculate(events_pre, expected_side_effects=1, tool_metadata=tool_meta)
        assert res_pre.bounds.s_min == 1
        assert res_pre.bounds.s_max == 2
        assert res_pre.external_status == "UNRESOLVED_INTERVAL"

        # Post-reconciliation: Authoritative Audit confirms a1 has no unrecorded mutations
        audit_evidence = TEST_AUDIT_AUTH.issue_audit_evidence(
            run_id="run_ex6",
            scenario_id="ex6",
            snapshot_id="snap_ex6",
            attempt_ids=["a1"],
            verified_effects=["eff_01"],
        )
        tool_meta["audit_authority"] = TEST_AUDIT_AUTH
        res_post = TwoLayerEffectAccountingEngine.calculate(
            events_pre,
            expected_side_effects=1,
            tool_metadata=tool_meta,
            reconciliation_audit=audit_evidence,
            audit_authority=TEST_AUDIT_AUTH,
        )
        assert res_post.bounds.s_min == 1
        assert res_post.bounds.s_max == 1
        assert res_post.bounds.is_exact
        assert res_post.external_status == "EXACT_MATCH"


# ==============================================================================
# 3. Capacity Provenance Tiers & Authoritative Inventory Tests
# ==============================================================================


class TestCapacityProvenanceAndInventory:
    """Verify strict provenance tier semantics and inventory completeness rules."""

    @pytest.mark.parametrize("tier", [CapacityProvenanceTier.TIER_C1, CapacityProvenanceTier.TIER_C2])
    def test_c1_and_c2_unverified_tiers_yield_unbounded_ambiguity(self, tier: CapacityProvenanceTier) -> None:
        """C1 developer declarations and C2 documented limits cannot establish completeness."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c1",
                scenario_id="c1",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_c1",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": tier.value, "max_capacity": 1},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    @pytest.mark.parametrize("tier", [CapacityProvenanceTier.TIER_C3, CapacityProvenanceTier.TIER_C4])
    def test_c3_and_c4_bounded_tiers_yield_finite_bounds(self, tier: CapacityProvenanceTier) -> None:
        """C3 runtime-enforced and C4 independently-verified tiers establish bounded capacity."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c3",
                scenario_id="c3",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_c3",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": tier.value, "max_capacity": 2},
            )
        ]
        if tier == CapacityProvenanceTier.TIER_C3:
            tool_meta = make_c3_meta(events, "svc", max_capacity=2)
            res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=tool_meta)
            assert res.bounds.s_min == 0
            assert res.bounds.s_max == 2
        else:
            audit = TEST_AUDIT_AUTH.issue_audit_evidence(
                run_id="run_c3",
                scenario_id="c3",
                snapshot_id="snap_c4",
                attempt_ids=["a1"],
                verified_effects=["eff_01", "eff_02"],
            )
            res = TwoLayerEffectAccountingEngine.calculate(
                events, expected_side_effects=2, reconciliation_audit=audit, audit_authority=TEST_AUDIT_AUTH
            )
            assert res.bounds.s_min == 2
            assert res.bounds.s_max == 2
        assert not res.bounds.is_unbounded

    def test_authoritative_complete_inventory_condition_2a_receipt(self) -> None:
        """Condition 2a: Verified receipt with Tier C3/C4 contract guarantees completeness."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_2a",
                scenario_id="2a",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_2a",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={
                    "capacity_provenance": "TIER_C3",
                    "max_capacity": 5,
                    "receipt_complete": True,
                },
            )
        ]
        tool_meta = make_c3_meta(events, "svc", max_capacity=5)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=tool_meta)
        assert res.bounds.s_min == 1
        assert res.bounds.s_max == 1
        assert res.bounds.is_exact
        assert res.external_status == "EXACT_MATCH"

    def test_authoritative_complete_inventory_condition_2b_saturation(self) -> None:
        """Condition 2b: Exact bounded saturation with Tier C3/C4 guarantees u(a) = 0."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_2b",
                scenario_id="2b",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_2b",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C4", "max_capacity": 1},
            )
        ]
        tool_meta = make_c3_meta(events, "svc", max_capacity=1)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=tool_meta)
        assert res.bounds.s_min == 1
        assert res.bounds.s_max == 1
        assert res.bounds.is_exact

    def test_handler_cannot_manufacture_absence_when_commits_exist(self) -> None:
        """AbsentStatus.TRUE is rejected if physical commits are proven."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_false_abs",
                scenario_id="fa",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_fa",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={
                    "absent_status": "TRUE",
                    "verified_absence": True,
                },
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        assert res.attempts["a1"].absent_status == AbsentStatus.FALSE
        assert res.bounds.s_min == 1

    def test_developer_boolean_cannot_establish_absence_under_tier_c1(self) -> None:
        """A developer passing verified_absence=True under Tier C1 cannot manufacture AbsentStatus.TRUE."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c1_abs",
                scenario_id="c1_abs",
                tool_name="svc",
                action_type="query",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_c1_abs",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={
                    "capacity_provenance": "TIER_C1",
                    "verified_absence": True,
                    "absent_status": "TRUE",
                },
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        assert res.attempts["a1"].absent_status == AbsentStatus.UNVERIFIED
        assert res.bounds.s_min == 0
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_developer_boolean_cannot_establish_receipt_completeness_under_tier_c1(self) -> None:
        """A developer passing receipt_complete=True under Tier C1 cannot establish InventoryStatus.COMPLETE."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c1_rcpt",
                scenario_id="c1_rcpt",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_c1_rcpt",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={
                    "capacity_provenance": "TIER_C1",
                    "receipt_complete": True,
                    "max_capacity": 1,
                },
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        assert res.attempts["a1"].inventory_status != InventoryStatus.COMPLETE
        assert res.bounds.s_min == 1
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_developer_boolean_cannot_establish_audit_completeness_under_tier_c1(self) -> None:
        """A developer passing audit_complete=True under Tier C1 cannot establish InventoryStatus.COMPLETE."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c1_audit",
                scenario_id="c1_audit",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_c1_audit",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={
                    "capacity_provenance": "TIER_C1",
                    "audit_complete": True,
                },
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        assert res.attempts["a1"].inventory_status != InventoryStatus.COMPLETE
        assert res.bounds.s_min == 0
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_developer_boolean_cannot_establish_verified_deduplication_under_tier_c1(self) -> None:
        """A developer passing verified_service_deduplication=True under Tier C1 has it forced to False."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c1_dedup",
                scenario_id="c1_dedup",
                tool_name="svc",
                action_type="act",
                operation_id="op_c1",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_c1",
                idempotency_key_hash="hash_c1",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={
                    "capacity_provenance": "TIER_C1",
                    "verified_service_deduplication": True,
                },
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_c1_dedup",
                scenario_id="c1_dedup",
                tool_name="svc",
                action_type="act",
                operation_id="op_c1",
                attempt_id="a2",
                attempt_number=2,
                effect_id=None,
                request_fingerprint="fp_c1",
                idempotency_key_hash="hash_c1",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="101.0",
                details={
                    "capacity_provenance": "TIER_C1",
                    "verified_service_deduplication": True,
                },
            ),
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        assert res.attempts["a1"].verified_service_deduplication is False
        assert res.attempts["a2"].verified_service_deduplication is False
        assert res.tightening_applied["op_c1:hash_c1"] is False


# ==============================================================================
# 4. Capacity Breach Handling & Fail-Safe Promotion Tests
# ==============================================================================


class TestCapacityBreachHandling:
    """Verify Section 3.4 capacity breach detection, fail-safe promotion, and integrity status."""

    def test_capacity_breach_promotes_bounds_and_flags_integrity_violation(self) -> None:
        """c(a) > M(a) promotes M(a)=inf, u(a)=inf, and flags INTEGRITY_VIOLATION."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_breach",
                scenario_id="breach",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_brk",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_breach",
                scenario_id="breach",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_02",  # Second effect exceeds max_capacity=1!
                request_fingerprint="fp_brk",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.1",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "svc", max_capacity=1)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=tool_meta)
        assert res.capacity_breached is True
        assert res.breached_attempt_id == "a1"
        assert res.bounds.s_min == 2
        assert res.bounds.is_unbounded
        assert res.infrastructure_status == PrimaryInfrastructureStatus.INTEGRITY_VIOLATION.value
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

        # Verify Slice 1 Tier 1 precedence: INTEGRITY_VIOLATION always evaluates to INFRASTRUCTURE_ERROR
        outcome = aggregate_outcome(
            behavioral_verdict="PASS",
            external_status=res.external_status,
            infrastructure_status=res.infrastructure_status,
            expected_side_effects=1,
            execution_finality="FINAL",
        )
        assert outcome == "INFRASTRUCTURE_ERROR"

    def test_capacity_breach_raise_on_breach_option(self) -> None:
        """raise_on_breach=True raises CapacityBreachIntegrityError."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_breach",
                scenario_id="breach",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_brk",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_breach",
                scenario_id="breach",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_02",
                request_fingerprint="fp_brk",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.1",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "svc", max_capacity=1)
        with pytest.raises(CapacityBreachIntegrityError, match="Capacity breach detected"):
            TwoLayerEffectAccountingEngine.calculate(events, raise_on_breach=True, tool_metadata=tool_meta)


# ==============================================================================
# 5. Safe Idempotency Correlation Tests (6 Positive & 6 Negative Conditions)
# ==============================================================================


class TestSafeIdempotencyCorrelation:
    """Verify Section 3.5: All 6 conditions must hold for tightening; fallback to conservative summation otherwise."""

    @property
    def tool_meta(self) -> dict[str, Any]:
        att = TEST_RUNTIME_AUTH.attest(run_id="run_idem", tool_name="pay_tool", max_capacity=1)
        return {
            "pay_tool": {
                "harness_attestation": att,
                "verified_service_deduplication": True,
            },
            "runtime_authority": TEST_RUNTIME_AUTH,
        }

    def _build_valid_idempotent_pair(self) -> list[LedgerEvent]:
        """Build a pair of retry attempts satisfying ALL SIX idempotency conditions."""
        contract = IdempotencyContract(
            guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
            scope=IdempotencyScope.OPERATION_AND_ACTION,
            retention_ttl_seconds=300.0,
            verified_service_deduplication=True,
        )
        return [
            LedgerEvent(
                event_id=1,
                run_id="run_idem",
                scenario_id="idem",
                tool_name="pay_tool",
                action_type="charge",
                operation_id="op_charge_100",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_canonical_json_123",
                idempotency_key_hash="hash_key_abc",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="10.0",
                details={
                    "capacity_provenance": "TIER_C3",
                    "max_capacity": 1,
                    "verified_service_deduplication": True,
                    "idempotency_contract": contract,
                },
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_idem",
                scenario_id="idem",
                tool_name="pay_tool",
                action_type="charge",
                operation_id="op_charge_100",
                attempt_id="a2",
                attempt_number=2,
                effect_id=None,
                request_fingerprint="fp_canonical_json_123",
                idempotency_key_hash="hash_key_abc",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="20.0",  # delta_t = 10.0 <= 300.0 TTL
                details={
                    "capacity_provenance": "TIER_C3",
                    "max_capacity": 1,
                    "verified_service_deduplication": True,
                    "idempotency_contract": contract,
                },
            ),
        ]

    def test_positive_tightening_when_all_6_conditions_verified(self) -> None:
        """When all 6 conditions are concurrently met, u(K) = max(u(a)) = 1."""
        events = self._build_valid_idempotent_pair()
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=self.tool_meta)
        cls_key = "op_charge_100:hash_key_abc"
        assert res.tightening_applied[cls_key] is True
        assert res.unresolved_by_class[cls_key] == 1
        # Bounds: S in [0, 1] instead of [0, 2]
        assert res.bounds.s_min == 0
        assert res.bounds.s_max == 1

    def test_negative_1_scope_or_action_mismatch_fallback(self) -> None:
        """Condition 1 failure: Differing actions prevent tightening -> sum u(a) = 2."""
        events = self._build_valid_idempotent_pair()
        events[1].action_type = "refund"
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=self.tool_meta)
        cls_key = "op_charge_100:hash_key_abc"
        assert res.tightening_applied[cls_key] is False
        assert res.unresolved_by_class[cls_key] == 2
        assert res.bounds.s_max == 2

    def test_negative_2_divergent_request_fingerprint_fallback(self) -> None:
        """Condition 2 failure: Divergent payload fingerprints prevent tightening -> sum u(a) = 2."""
        events = self._build_valid_idempotent_pair()
        events[1].request_fingerprint = "fp_canonical_json_DIFFERENT"
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=self.tool_meta)
        cls_key = "op_charge_100:hash_key_abc"
        assert res.tightening_applied[cls_key] is False
        assert res.unresolved_by_class[cls_key] == 2
        assert res.bounds.s_max == 2

    def test_negative_3_differing_key_hash_fallback(self) -> None:
        """Condition 3 failure: Differing key hashes partition into different classes -> sum u(a) = 2."""
        events = self._build_valid_idempotent_pair()
        events[1].idempotency_key_hash = "hash_key_DIFFERENT"
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=self.tool_meta)
        assert res.bounds.s_max == 2

    def test_negative_4_ttl_expired_fallback(self) -> None:
        """Condition 4 failure: Delta t exceeds retention TTL -> sum u(a) = 2."""
        events = self._build_valid_idempotent_pair()
        # Delta t = 400.0 > 300.0 TTL
        events[1].simulated_timestamp = "410.0"
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=self.tool_meta)
        cls_key = "op_charge_100:hash_key_abc"
        assert res.tightening_applied[cls_key] is False
        assert res.unresolved_by_class[cls_key] == 2
        assert res.bounds.s_max == 2

    def test_negative_5_unverified_service_deduplication_fallback(self) -> None:
        """Condition 5 failure: Developer declares STRICT_EXACTLY_ONCE but service deduplication unverified."""
        events = self._build_valid_idempotent_pair()
        contract_unverified = IdempotencyContract(
            guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
            verified_service_deduplication=False,
        )
        for e in events:
            e.details["verified_service_deduplication"] = False
            e.details["idempotency_contract"] = contract_unverified
        # Harness metadata specifies verified_service_deduplication is False
        tool_meta_unverified = {
            "pay_tool": {
                "harness_attestation": TEST_RUNTIME_AUTH.attest(
                    run_id="run_idem", tool_name="pay_tool", max_capacity=1
                ),
                "verified_service_deduplication": False,
                "max_side_effect_capacity": 1,
            },
            "runtime_authority": TEST_RUNTIME_AUTH,
        }
        res = TwoLayerEffectAccountingEngine.calculate(
            events, expected_side_effects=1, tool_metadata=tool_meta_unverified
        )
        cls_key = "op_charge_100:hash_key_abc"
        assert res.tightening_applied[cls_key] is False
        assert res.unresolved_by_class[cls_key] == 2

    def test_negative_5_tier_c1_cannot_support_verified_deduplication(self) -> None:
        """Condition 5 failure: Tier C1 declared provenance cannot satisfy Condition 5."""
        events = self._build_valid_idempotent_pair()
        for e in events:
            e.details["capacity_provenance"] = "TIER_C1"
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        cls_key = "op_charge_100:hash_key_abc"
        assert res.tightening_applied[cls_key] is False

    def test_ttl_boundary_exact_match_tightens(self) -> None:
        """Delta t == TTL satisfies Condition 4 (closed interval)."""
        events = self._build_valid_idempotent_pair()
        # Exact TTL window: 10.0 to 310.0 -> delta_t == 300.0
        events[1].simulated_timestamp = "310.0"
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=self.tool_meta)
        cls_key = "op_charge_100:hash_key_abc"
        assert res.tightening_applied[cls_key] is True
        assert res.bounds.s_max == 1

    def test_negative_ttl_rejected_by_contract(self) -> None:
        """Negative TTL in contract raises IdempotencyCorrelationError."""
        with pytest.raises(IdempotencyCorrelationError, match="cannot be negative"):
            IdempotencyContract(retention_ttl_seconds=-10.0)

    def test_negative_6_payload_mutation_under_same_key_fallback(self) -> None:
        """Condition 6 failure: Payload mutated under same idempotency key -> sum u(a) = 2."""
        events = self._build_valid_idempotent_pair()
        # Mutated payload produces divergent fingerprint
        events[1].request_fingerprint = "fp_mutated_payload_456"
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=self.tool_meta)
        cls_key = "op_charge_100:hash_key_abc"
        assert res.tightening_applied[cls_key] is False
        assert res.unresolved_by_class[cls_key] == 2
        assert res.bounds.s_max == 2

    def test_missing_correlation_evidence_prevents_tightening(self) -> None:
        """Attempt lacking operation_id or key_hash is not correlated."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_missing",
                scenario_id="missing",
                tool_name="pay_tool",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_1",
                idempotency_key_hash=None,
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="10.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_missing",
                scenario_id="missing",
                tool_name="pay_tool",
                action_type="charge",
                attempt_id="a2",
                attempt_number=2,
                effect_id=None,
                request_fingerprint="fp_1",
                idempotency_key_hash=None,
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="20.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "pay_tool", max_capacity=1)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=tool_meta)
        assert res.bounds.s_max == 2


# ==============================================================================
# 6. External Status Derivation Boundary Tests
# ==============================================================================


class TestExternalStatusDerivation:
    """Verify deterministic derivation of external_status under all boundary conditions."""

    def test_zero_expected_and_zero_bounds_is_confirmed_absent(self) -> None:
        b = SideEffectBounds(s_min=0, s_max=0)
        assert derive_external_status(b, expected_side_effects=0) == "CONFIRMED_ABSENT"

    def test_positive_expected_and_zero_bounds_is_missing_commits(self) -> None:
        """[0, 0] with positive expected count is MISSING_COMMITS, not confirmed absent!"""
        b = SideEffectBounds(s_min=0, s_max=0)
        assert derive_external_status(b, expected_side_effects=1) == "MISSING_COMMITS"

    def test_exact_positive_match(self) -> None:
        b = SideEffectBounds(s_min=1, s_max=1)
        assert derive_external_status(b, expected_side_effects=1) == "EXACT_MATCH"

    def test_exact_count_above_expected_is_duplicate_committed(self) -> None:
        b = SideEffectBounds(s_min=2, s_max=2)
        assert derive_external_status(b, expected_side_effects=1) == "DUPLICATE_COMMITTED"

    def test_bounds_upper_below_expected_is_missing_commits(self) -> None:
        b = SideEffectBounds(s_min=0, s_max=1)
        assert derive_external_status(b, expected_side_effects=2) == "MISSING_COMMITS"

    def test_infinite_upper_bound_is_unbounded_ambiguity(self) -> None:
        b = SideEffectBounds(s_min=1, s_max=math.inf)
        assert derive_external_status(b, expected_side_effects=1) == "UNBOUNDED_AMBIGUITY"
        assert derive_external_status(b, expected_side_effects=0) == "UNBOUNDED_AMBIGUITY"
        assert derive_external_status(b, expected_side_effects=None) == "UNBOUNDED_AMBIGUITY"

    def test_finite_non_degenerate_interval_is_unresolved_interval(self) -> None:
        b = SideEffectBounds(s_min=0, s_max=2)
        assert derive_external_status(b, expected_side_effects=1) == "UNRESOLVED_INTERVAL"

    def test_contradictory_evidence_flag(self) -> None:
        b = SideEffectBounds(s_min=1, s_max=1)
        assert (
            derive_external_status(b, expected_side_effects=1, contradictory_evidence=True)
            == "CONTRADICTORY_EVIDENCE"
        )


# ==============================================================================
# 7. Deduplication & Transaction Identity Tests
# ==============================================================================


class TestEffectDeduplicationAcrossAttempts:
    """Verify Layer 2 deduplication of identical effect IDs across attempts."""

    def test_identical_effect_ids_merge_via_set_union(self) -> None:
        """Attempts a1 and a2 emitting same effect_id 'eff_dup' yield S_min = 1."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_union",
                scenario_id="union",
                tool_name="tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_dup",
                request_fingerprint="fp_dup",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_union",
                scenario_id="union",
                tool_name="tool",
                action_type="act",
                attempt_id="a2",
                attempt_number=2,
                effect_id="eff_dup",
                request_fingerprint="fp_dup",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="101.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "tool", max_capacity=1)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=tool_meta)
        assert res.bounds.s_min == 1
        assert res.bounds.s_max == 1
        assert res.verified_effects == {"eff_dup"}
        assert res.duplicate_count == 0

    def test_distinct_effect_ids_yield_multiple_commits_and_duplicates(self) -> None:
        """Attempts a1 and a2 emitting distinct effect IDs under same operation yield duplicate."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_distinct",
                scenario_id="distinct",
                tool_name="tool",
                action_type="act",
                operation_id="op_single",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_dist",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_distinct",
                scenario_id="distinct",
                tool_name="tool",
                action_type="act",
                operation_id="op_single",
                attempt_id="a2",
                attempt_number=2,
                effect_id="eff_02",
                request_fingerprint="fp_dist",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="101.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        tool_meta = make_c3_meta(events, "tool", max_capacity=1)
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1, tool_metadata=tool_meta)
        assert res.bounds.s_min == 2
        assert res.duplicate_count == 1
        assert res.external_status == "DUPLICATE_COMMITTED"


# ==============================================================================
# 8. Evaluator & RuleEngine Accounting Integration Tests
# ==============================================================================


class TestEvaluatorAccountingIntegration:
    """Verify end-to-end integration of TwoLayerEffectAccountingEngine with RuleEngine and RecoveryEvaluator."""

    def test_rule_engine_evaluate_side_effects_integrates_accounting_bounds(self) -> None:
        """RuleEngine.evaluate_side_effects incorporates accounting bounds and external status in evidence."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_int",
                scenario_id="sc_int",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_int_1",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_int_1",
                request_fingerprint="fp_int",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]
        assertions = ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
            max_allowed_retries=1,
            expect_agent_success=True,
        )
        tool_meta = make_c3_meta(events, "payment_svc", max_capacity=1)
        acct = TwoLayerEffectAccountingEngine.calculate(
            events,
            expected_side_effects=assertions.expected_side_effects,
            tool_metadata=tool_meta,
        )
        assertion, actual_commits, duplicate_count = RuleEngine.evaluate_side_effects(
            events, assertions, accounting_result=acct
        )
        assert assertion.passed is True
        assert actual_commits == 1
        assert duplicate_count == 0
        # Check that evidence contains accounting bounds and external status
        evidence_text = " ".join(assertion.evidence)
        assert "Accounting bounds: [1, 1]" in evidence_text
        assert "external_status='EXACT_MATCH'" in evidence_text

    def test_recovery_evaluator_escalates_capacity_breach_to_infrastructure_error(self) -> None:
        """RecoveryEvaluator.evaluate() escalates capacity breach to EvaluationVerdict.INFRASTRUCTURE_ERROR."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_breach_eval",
                scenario_id="sc_breach",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_b1",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_b1",
                request_fingerprint="fp_b",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_breach_eval",
                scenario_id="sc_breach",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_b1",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_b2",  # Second effect under max_capacity=1 breaches!
                request_fingerprint="fp_b",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.1",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            ),
        ]
        assertions = ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
            max_allowed_retries=1,
            expect_agent_success=True,
        )
        agent_result = AgentResult(
            success=True,
            data={"status": "completed"},
        )

        tool_meta = make_c3_meta(events, "payment_svc", max_capacity=1)
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        result = evaluator.evaluate(events, assertions, agent_result, tool_metadata=tool_meta)

        assert result.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR
        assert result.passed is False
        assert any("Capacity breach detected: attempt 'a1'" in r for r in result.failure_reasons)

    def test_recovery_evaluator_evaluates_exact_match_pass(self) -> None:
        """RecoveryEvaluator.evaluate() successfully passes when accounting bounds match expectations."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_pass",
                scenario_id="sc_pass",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_p1",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_p1",
                request_fingerprint="fp_p",
                idempotency_key_hash="key_p1",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]
        assertions = ScenarioAssertions(
            expected_side_effects=1,
            max_duplicate_effects=0,
            require_stable_idempotency_key=True,
            max_allowed_retries=1,
            expect_agent_success=True,
        )
        agent_result = AgentResult(
            success=True,
            data={"status": "completed"},
        )

        tool_meta = make_c3_meta(events, "payment_svc", max_capacity=1)
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        result = evaluator.evaluate(events, assertions, agent_result, tool_metadata=tool_meta)

        assert result.verdict == EvaluationVerdict.PASS
        assert result.passed is True
        assert result.actual_side_effects == 1
        assert result.duplicate_effect_count == 0


# ==============================================================================
# 9. Adversarial Authority Enforcement & Trust Boundary Tests (Blocker 1)
# ==============================================================================


class TestAdversarialAuthorityEnforcement:
    """Verify that untrusted developer declarations cannot promote evidence to Tier C3/C4."""

    def test_adversarial_developer_cannot_promote_to_c3_via_event_details_string(self) -> None:
        """A developer passing details={'capacity_provenance': 'TIER_C3'} without harness backing is rejected."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_c3",
                scenario_id="adv_c3",
                tool_name="untrusted_mock",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_adv",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.declared_provenance == CapacityProvenanceTier.TIER_C3
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert vec.capacity_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.max_capacity)
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_adversarial_developer_cannot_promote_to_c4_via_event_details_string(self) -> None:
        """A developer passing details={'capacity_provenance': 'TIER_C4'} without harness backing is rejected."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_c4",
                scenario_id="adv_c4",
                tool_name="untrusted_mock",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_adv",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C4", "max_capacity": 1},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.declared_provenance == CapacityProvenanceTier.TIER_C4
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert vec.capacity_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.max_capacity)
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_developer_cannot_forge_c4_audit_completeness_via_event_details(self) -> None:
        """Worker event with reconciliation_complete=True but without RECON_ENGINE origin is rejected."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_audit",
                scenario_id="adv_audit",
                tool_name="worker_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_adv",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={
                    "capacity_provenance": "TIER_C4",
                    "reconciliation_complete": True,
                    "audit_complete": True,
                },
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert vec.inventory_status == InventoryStatus.UNKNOWN
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_developer_cannot_forge_receipt_completeness_via_event_details_boolean(self) -> None:
        """receipt_complete=True in event details on Tier C1/C2 cannot establish complete inventory."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_rcpt",
                scenario_id="adv_rcpt",
                tool_name="untrusted_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_01",
                request_fingerprint="fp_adv",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "receipt_complete": True, "max_capacity": 1},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert vec.inventory_status == InventoryStatus.PARTIAL
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_developer_cannot_forge_absence_via_event_details_boolean(self) -> None:
        """verified_absence=True on an unverified tool cannot establish AbsentStatus.TRUE."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_abs",
                scenario_id="adv_abs",
                tool_name="untrusted_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_adv",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
                details={
                    "capacity_provenance": "TIER_C4",
                    "verified_absence": True,
                    "absent_status": "TRUE",
                },
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        vec = res.attempts["a1"]
        assert vec.absent_status == AbsentStatus.UNVERIFIED
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_developer_cannot_promote_deduplication_via_details_boolean(self) -> None:
        """verified_service_deduplication=True without trusted harness backing cannot enable tightening."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_dedup",
                scenario_id="adv_dedup",
                tool_name="untrusted_tool",
                action_type="act",
                operation_id="op_adv",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_adv",
                idempotency_key_hash="hash_adv",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="10.0",
                details={"capacity_provenance": "TIER_C3", "verified_service_deduplication": True},
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_adv_dedup",
                scenario_id="adv_dedup",
                tool_name="untrusted_tool",
                action_type="act",
                operation_id="op_adv",
                attempt_id="a2",
                attempt_number=2,
                effect_id=None,
                request_fingerprint="fp_adv",
                idempotency_key_hash="hash_adv",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="20.0",
                details={"capacity_provenance": "TIER_C3", "verified_service_deduplication": True},
            ),
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=1)
        cls_key = "op_adv:hash_adv"
        assert res.tightening_applied[cls_key] is False
        assert math.isinf(res.unresolved_by_class[cls_key])

    # --------------------------------------------------------------------------
    # Section A: C3 Trust Boundary Adversarial Tests
    # --------------------------------------------------------------------------

    def test_adversarial_ordinary_registered_tool_service_remains_c1(self) -> None:
        """An ordinary ToolService registered in the registry without runtime attestation remains Tier C1."""
        class OrdinaryTool(ToolService):
            name = "ordinary_tool"

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        reg = ToolRegistry()
        reg.register(OrdinaryTool())
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_ord",
                scenario_id="sc_ord",
                tool_name="ordinary_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_ord",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_registry=reg, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.max_capacity)
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_adversarial_spoofed_payment_gateway_name_remains_c1(self) -> None:
        """A custom tool spoofing the name 'payment_gateway' or 'MockPaymentService' remains Tier C1."""
        class SpoofedPaymentTool(ToolService):
            name = "payment_gateway"

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        reg = ToolRegistry()
        reg.register(SpoofedPaymentTool())
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_spoof",
                scenario_id="sc_spoof",
                tool_name="payment_gateway",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_spoof",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_registry=reg, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.max_capacity)
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_forged_is_runtime_enforced_attribute_remains_c1(self) -> None:
        """A tool with is_runtime_enforced=True attribute cannot establish Tier C3 authority."""
        class ForgedAttributeTool(ToolService):
            name = "forged_attr_tool"
            is_runtime_enforced = True

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        reg = ToolRegistry()
        reg.register(ForgedAttributeTool())
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_attr",
                scenario_id="sc_attr",
                tool_name="forged_attr_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_attr",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_registry=reg, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.max_capacity)
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_forged_verified_provenance_enum_remains_c1(self) -> None:
        """A tool with verified_provenance=TIER_C3 attribute cannot establish Tier C3 authority."""
        class ForgedEnumTool(ToolService):
            name = "forged_enum_tool"
            verified_provenance = CapacityProvenanceTier.TIER_C3

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        reg = ToolRegistry()
        reg.register(ForgedEnumTool())
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_enum",
                scenario_id="sc_enum",
                tool_name="forged_enum_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_enum",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_registry=reg, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.max_capacity)
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_ordinary_metadata_dict_claiming_c3_remains_c1(self) -> None:
        """An ordinary caller metadata dict claiming C3 cannot establish Tier C3 authority."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_adv_meta",
                scenario_id="sc_meta",
                tool_name="meta_claimed_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_meta",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        tool_meta = {
            "meta_claimed_tool": {
                "runtime_enforced": True,
                "c3_enforced": True,
                "capacity_provenance": "TIER_C3",
                "verified_provenance": CapacityProvenanceTier.TIER_C3,
            }
        }
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_metadata=tool_meta, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.max_capacity)
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_adversarial_c3_direct_attempts_to_mint_attestation_rejected(self) -> None:
        """Direct attempts to instantiate HarnessRuntimeAttestation with unverified signature fail verification."""
        forged_att = HarnessRuntimeAttestation(
            run_id="run_direct_mint",
            tool_name="forged_tool",
            max_capacity=1,
            runtime_guarantee="RUNTIME_UNIQUE_CONSTRAINT",
            signature="forged_unauthorized_hmac_signature",
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_direct_mint",
                scenario_id="sc_mint",
                tool_name="forged_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_mint",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        tool_meta = {
            "forged_tool": {"harness_attestation": forged_att},
            "runtime_authority": TEST_RUNTIME_AUTH,
        }
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_metadata=tool_meta, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_c3_reusing_valid_attestation_for_another_tool_rejected(self) -> None:
        """A valid attestation issued for tool_a attached to tool_b is rejected and remains Tier C1."""
        valid_att_tool_a = TEST_RUNTIME_AUTH.attest(
            run_id="run_cross_tool", tool_name="tool_a", max_capacity=1
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_cross_tool",
                scenario_id="sc_tool",
                tool_name="tool_b",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_tool",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        tool_meta = {
            "tool_b": {"harness_attestation": valid_att_tool_a},
            "runtime_authority": TEST_RUNTIME_AUTH,
        }
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_metadata=tool_meta, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_c3_reusing_evidence_for_another_run_rejected(self) -> None:
        """A valid attestation issued for run_alpha attached to run_beta is rejected and remains Tier C1."""
        valid_att_run_alpha = TEST_RUNTIME_AUTH.attest(
            run_id="run_alpha", tool_name="tool_svc", max_capacity=1
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_beta",
                scenario_id="sc_run",
                tool_name="tool_svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_run",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        tool_meta = {
            "tool_svc": {"harness_attestation": valid_att_run_alpha},
            "runtime_authority": TEST_RUNTIME_AUTH,
        }
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_metadata=tool_meta, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_c3_capacity_mismatch_breach(self) -> None:
        """When physical mutations exceed certified C3 capacity (c(a) > M(a)), integrity breach is raised."""
        att = TEST_RUNTIME_AUTH.attest(run_id="run_breach_mismatch", tool_name="svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_breach_mismatch",
                scenario_id="sc_br",
                tool_name="svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_br",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_breach_mismatch",
                scenario_id="sc_br",
                tool_name="svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=2,
                effect_id="eff_2",
                request_fingerprint="fp_br",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="105.0",
            ),
        ]
        tool_meta = {"svc": {"harness_attestation": att}, "runtime_authority": TEST_RUNTIME_AUTH}
        res = TwoLayerEffectAccountingEngine.calculate(
            events, expected_side_effects=1, tool_metadata=tool_meta, raise_on_breach=False
        )
        assert res.capacity_breached is True
        assert res.infrastructure_status == PrimaryInfrastructureStatus.INTEGRITY_VIOLATION.value
        assert res.bounds.is_unbounded

        with pytest.raises(CapacityBreachIntegrityError):
            TwoLayerEffectAccountingEngine.calculate(
                events, expected_side_effects=1, tool_metadata=tool_meta, raise_on_breach=True
            )

    def test_genuine_c3_established_only_via_valid_harness_attestation(self) -> None:
        """A valid HarnessRuntimeAttestation establishes authentic Tier C3 runtime enforcement."""
        att = TEST_RUNTIME_AUTH.attest(run_id="run_gen_c3", tool_name="genuine_c3_tool", max_capacity=2)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_gen_c3",
                scenario_id="sc_gen",
                tool_name="genuine_c3_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_gen",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        tool_meta = {"genuine_c3_tool": {"harness_attestation": att}, "runtime_authority": TEST_RUNTIME_AUTH}
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_metadata=tool_meta, expected_side_effects=1)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C3
        assert vec.max_capacity == 2
        assert vec.unresolved_capacity == 2
        assert res.bounds.s_max == 2

    # --------------------------------------------------------------------------
    # Section B: C4 Trust Boundary Adversarial Tests
    # --------------------------------------------------------------------------

    def test_adversarial_forged_recon_engine_writer_id_remains_c1(self) -> None:
        """Forged writer_id='RECON_ENGINE' in event details without valid audit evidence remains Tier C1."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_forged_recon",
                scenario_id="sc_forged",
                tool_name="untrusted_recon",
                action_type="audit",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_r",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
                details={"writer_id": "RECON_ENGINE"},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_forged_audit_flags_in_event_details_remain_c1(self) -> None:
        """Forged audit booleans in event details remain Tier C1 and preserve conservative bounds."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_forged_flags",
                scenario_id="sc_flags",
                tool_name="untrusted_recon",
                action_type="audit",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_r",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
                details={
                    "writer_id": "RECON_ENGINE",
                    "audit_verified": True,
                    "drain_delay_verified": True,
                    "reconciliation_complete": True,
                },
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert vec.inventory_status == InventoryStatus.UNKNOWN
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_adversarial_caller_metadata_dict_claiming_c4_remains_c1(self) -> None:
        """Caller dictionary metadata claiming verified_audit=True remains Tier C1."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_forged_meta_c4",
                scenario_id="sc_meta_c4",
                tool_name="untrusted_recon",
                action_type="audit",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_r",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="100.0",
            )
        ]
        tool_meta = {
            "untrusted_recon": {
                "verified_audit": True,
                "verified_provenance": "TIER_C4",
                "capacity_provenance": "TIER_C4",
            }
        }
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_metadata=tool_meta, expected_side_effects=0)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_adversarial_c4_public_evidence_minting_attempts_rejected(self) -> None:
        """Constructing ReconciliationAuditEvidence with unauthenticated signature is rejected as Tier C1."""
        forged_audit = ReconciliationAuditEvidence(
            run_id="run_mint_audit",
            scenario_id="sc_mint",
            snapshot_id="snap_1",
            attempt_ids=["a1"],
            verified_effects={"eff_minted"},
            signature="forged_audit_signature_xyz",
            audit_session_id="sess_fake",
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_mint_audit",
                scenario_id="sc_mint",
                tool_name="recon_svc",
                action_type="audit",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_minted",
                request_fingerprint="fp_mint",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(
            events, reconciliation_audit=forged_audit, audit_authority=TEST_AUDIT_AUTH, expected_side_effects=1
        )
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1

    def test_adversarial_c4_cross_run_evidence_reuse_rejected(self) -> None:
        """Valid audit evidence for run_alpha passed to evaluate events of run_beta is rejected."""
        valid_audit_alpha = TEST_AUDIT_AUTH.issue_audit_evidence(
            run_id="run_alpha",
            scenario_id="sc_cross",
            snapshot_id="snap_1",
            attempt_ids=["a1"],
            verified_effects=["eff_alpha"],
        )
        events_beta = [
            LedgerEvent(
                event_id=1,
                run_id="run_beta",
                scenario_id="sc_cross",
                tool_name="recon_svc",
                action_type="audit",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_alpha",
                request_fingerprint="fp_b",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(
            events_beta, reconciliation_audit=valid_audit_alpha, audit_authority=TEST_AUDIT_AUTH, expected_side_effects=1
        )
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1

    def test_adversarial_c4_cross_snapshot_evidence_reuse_rejected(self) -> None:
        """Audit evidence verifying snapshot_1 fails verification when validated against snapshot_2."""
        audit_snap_1 = TEST_AUDIT_AUTH.issue_audit_evidence(
            run_id="run_snap",
            scenario_id="sc_snap",
            snapshot_id="snap_1",
            attempt_ids=["a1"],
            verified_effects=["eff_1"],
        )
        assert not TEST_AUDIT_AUTH.verify(
            audit_snap_1, for_run_id="run_snap", for_scenario_id="sc_snap", for_snapshot_id="snap_2"
        )

    def test_adversarial_c4_missing_attempt_or_effect_coverage_remains_c1(self) -> None:
        """Audit evidence covering only attempt a1 leaves attempt a2 uncovered at Tier C1."""
        audit_only_a1 = TEST_AUDIT_AUTH.issue_audit_evidence(
            run_id="run_partial_cov",
            scenario_id="sc_part",
            snapshot_id="snap_1",
            attempt_ids=["a1"],
            verified_effects=["eff_1"],
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_partial_cov",
                scenario_id="sc_part",
                tool_name="recon_svc",
                action_type="audit",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_p",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_partial_cov",
                scenario_id="sc_part",
                tool_name="recon_svc",
                action_type="audit",
                attempt_id="a2",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_p2",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="105.0",
            ),
        ]
        res = TwoLayerEffectAccountingEngine.calculate(
            events, reconciliation_audit=audit_only_a1, audit_authority=TEST_AUDIT_AUTH, expected_side_effects=1
        )
        assert res.attempts["a1"].verified_provenance == CapacityProvenanceTier.TIER_C4
        assert res.attempts["a2"].verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(res.attempts["a2"].unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_genuine_c4_established_only_via_valid_reconciliation_audit_evidence(self) -> None:
        """A valid ReconciliationAuditEvidence establishes authentic Tier C4 ground truth."""
        audit = TEST_AUDIT_AUTH.issue_audit_evidence(
            run_id="run_valid_c4",
            scenario_id="sc_valid_c4",
            snapshot_id="snap_c4",
            attempt_ids=["a1"],
            verified_effects=["eff_audited_1"],
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_valid_c4",
                scenario_id="sc_valid_c4",
                tool_name="recon_tool",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_audited_1",
                request_fingerprint="fp_v",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(
            events, expected_side_effects=1, reconciliation_audit=audit, audit_authority=TEST_AUDIT_AUTH
        )
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C4
        assert vec.inventory_status == InventoryStatus.COMPLETE
        assert vec.unresolved_capacity == 0
        assert res.bounds.s_min == 1
        assert res.bounds.s_max == 1
        assert res.external_status == "EXACT_MATCH"

    # --------------------------------------------------------------------------
    # Section C: Non-Mutating Execution Completeness Tests
    # --------------------------------------------------------------------------

    def test_adversarial_c1_tool_not_applicable_success_cannot_establish_completeness(self) -> None:
        """A C1 tool returning NOT_APPLICABLE + SUCCESS cannot establish complete inventory or u(a)=0."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c1_ro",
                scenario_id="sc_ro",
                tool_name="untrusted_query",
                action_type="read_records",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_ro",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C1"},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert vec.inventory_status == InventoryStatus.UNKNOWN
        assert vec.absent_status == AbsentStatus.UNVERIFIED
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.s_min == 0
        assert res.bounds.is_unbounded
        assert res.external_status == "UNBOUNDED_AMBIGUITY"

    def test_adversarial_c2_tool_not_applicable_success_cannot_establish_completeness(self) -> None:
        """A C2 documented tool returning NOT_APPLICABLE + SUCCESS cannot establish complete inventory or u(a)=0."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c2_ro",
                scenario_id="sc_ro",
                tool_name="doc_query",
                action_type="read_records",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_ro",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C2"},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert vec.inventory_status == InventoryStatus.UNKNOWN
        assert math.isinf(vec.unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_forged_read_only_metadata_remains_unverified(self) -> None:
        """An event with details={'read_only': True} under Tier C1 cannot establish completeness."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_forged_ro",
                scenario_id="sc_ro",
                tool_name="query_svc",
                action_type="query",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_ro",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
                details={"read_only": True, "mutates": False},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        assert res.attempts["a1"].inventory_status == InventoryStatus.UNKNOWN
        assert res.attempts["a1"].absent_status == AbsentStatus.UNVERIFIED
        assert math.isinf(res.attempts["a1"].unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_forged_replay_flags_remain_unverified(self) -> None:
        """An event claiming idempotent_replay=True under Tier C1 cannot establish completeness."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_forged_rep",
                scenario_id="sc_rep",
                tool_name="order_svc",
                action_type="create_order",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_rep",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
                details={"idempotent_replay": True},
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        assert res.attempts["a1"].inventory_status == InventoryStatus.UNKNOWN
        assert math.isinf(res.attempts["a1"].unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_fabricated_effect_ids_on_not_applicable_remain_unverified(self) -> None:
        """An event with commit_status=NOT_APPLICABLE and fabricated effect_id cannot establish completeness under C1."""
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_fab_eff",
                scenario_id="sc_fab",
                tool_name="svc",
                action_type="query",
                attempt_id="a1",
                attempt_number=1,
                effect_id="fake_fabricated_eff_123",
                request_fingerprint="fp_fab",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0)
        assert res.attempts["a1"].inventory_status == InventoryStatus.UNKNOWN
        assert math.isinf(res.attempts["a1"].unresolved_capacity)
        assert res.bounds.is_unbounded

    def test_c3_without_sufficient_runtime_guarantees(self) -> None:
        """Attestation with mismatched guarantee or unverified authority cannot establish completeness."""
        fake_att = HarnessRuntimeAttestation(
            run_id="run_c3_unguaranteed",
            tool_name="svc",
            max_capacity=1,
            runtime_guarantee="NOOP_GUARANTEE",
            signature="fake_sig",
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c3_unguaranteed",
                scenario_id="sc_ung",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_ung",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
            )
        ]
        tool_meta = {"svc": {"harness_attestation": fake_att}, "runtime_authority": TEST_RUNTIME_AUTH}
        res = TwoLayerEffectAccountingEngine.calculate(events, tool_metadata=tool_meta, expected_side_effects=0)
        assert res.attempts["a1"].verified_provenance == CapacityProvenanceTier.TIER_C1
        assert math.isinf(res.attempts["a1"].unresolved_capacity)

    def test_genuine_c3_tool_not_applicable_success_establishes_completeness(self) -> None:
        """A verified C3 tool returning NOT_APPLICABLE + SUCCESS authoritatively establishes completeness."""
        att = TEST_RUNTIME_AUTH.attest(run_id="run_c3_ro", tool_name="genuine_c3_ro", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c3_ro",
                scenario_id="sc_ro",
                tool_name="genuine_c3_ro",
                action_type="read_records",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_ro",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
            )
        ]
        tool_meta = {"genuine_c3_ro": {"harness_attestation": att}, "runtime_authority": TEST_RUNTIME_AUTH}
        res = TwoLayerEffectAccountingEngine.calculate(events, expected_side_effects=0, tool_metadata=tool_meta)
        vec = res.attempts["a1"]
        assert vec.verified_provenance == CapacityProvenanceTier.TIER_C3
        assert vec.inventory_status == InventoryStatus.COMPLETE
        assert vec.unresolved_capacity == 0
        assert res.bounds.s_min == 0
        assert res.bounds.s_max == 0
        assert res.external_status == "CONFIRMED_ABSENT"

    def test_invalid_c4_completeness(self) -> None:
        """Mismatched or forged C4 evidence cannot establish absence or completeness."""
        forged_audit = ReconciliationAuditEvidence(
            run_id="run_c4_invalid",
            scenario_id="sc_c4",
            snapshot_id="snap_1",
            attempt_ids=["a1"],
            confirmed_absent=True,
            signature="forged_sig",
            audit_session_id="sess_bad",
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c4_invalid",
                scenario_id="sc_c4",
                tool_name="recon_svc",
                action_type="check",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_c4",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(
            events, reconciliation_audit=forged_audit, audit_authority=TEST_AUDIT_AUTH, expected_side_effects=0
        )
        assert res.attempts["a1"].verified_provenance == CapacityProvenanceTier.TIER_C1
        assert res.attempts["a1"].absent_status == AbsentStatus.UNVERIFIED
        assert res.bounds.is_unbounded

    def test_valid_c4_completeness(self) -> None:
        """Valid C4 audit evidence with confirmed_absent=True establishes AbsentStatus.TRUE and [0, 0]."""
        audit = TEST_AUDIT_AUTH.issue_audit_evidence(
            run_id="run_c4_valid_abs",
            scenario_id="sc_c4",
            snapshot_id="snap_1",
            attempt_ids=["a1"],
            confirmed_absent=True,
        )
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_c4_valid_abs",
                scenario_id="sc_c4",
                tool_name="recon_svc",
                action_type="check",
                attempt_id="a1",
                attempt_number=1,
                effect_id=None,
                request_fingerprint="fp_c4",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.NOT_APPLICABLE,
                simulated_timestamp="100.0",
            )
        ]
        res = TwoLayerEffectAccountingEngine.calculate(
            events, reconciliation_audit=audit, audit_authority=TEST_AUDIT_AUTH, expected_side_effects=0
        )
        assert res.attempts["a1"].verified_provenance == CapacityProvenanceTier.TIER_C4
        assert res.attempts["a1"].absent_status == AbsentStatus.TRUE
        assert res.attempts["a1"].unresolved_capacity == 0
        assert res.bounds.s_min == 0
        assert res.bounds.s_max == 0
        assert res.external_status == "CONFIRMED_ABSENT"

    def test_capacity_breach_and_conservative_bounds(self) -> None:
        """Capacity breach promotes unresolved capacity to infinity and sets INTEGRITY_VIOLATION."""
        att = TEST_RUNTIME_AUTH.attest(run_id="run_cap_breach", tool_name="svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_cap_breach",
                scenario_id="sc_b",
                tool_name="svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_b",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_cap_breach",
                scenario_id="sc_b",
                tool_name="svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=2,
                effect_id="eff_2",
                request_fingerprint="fp_b",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="105.0",
            ),
        ]
        tool_meta = {"svc": {"harness_attestation": att}, "runtime_authority": TEST_RUNTIME_AUTH}
        res = TwoLayerEffectAccountingEngine.calculate(
            events, expected_side_effects=1, tool_metadata=tool_meta, raise_on_breach=False
        )
        assert res.capacity_breached is True
        assert res.infrastructure_status == PrimaryInfrastructureStatus.INTEGRITY_VIOLATION.value
        assert res.bounds.s_min == 2
        assert res.bounds.is_unbounded

    # --------------------------------------------------------------------------
    # Section D: Execution Finality Verification Tests
    # --------------------------------------------------------------------------

    def test_adversarial_caller_supplied_final_cannot_bypass_conditions(self) -> None:
        """Caller supplying execution_finality='FINAL' cannot bypass the five finality conditions."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_caller_final", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_caller_final",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(status=CompletionStatus.UNKNOWN)

        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality="FINAL",
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert res.passed is False

    def test_adversarial_caller_final_downgraded_when_worker_timed_out(self) -> None:
        """Caller supplying execution_finality='FINAL' is downgraded to PROVISIONAL when worker timed out."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_final_timeout", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_final_timeout",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality="FINAL",
            infrastructure_status=PrimaryInfrastructureStatus.WORKER_TIMED_OUT.value,
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert res.passed is False

    def test_adversarial_caller_final_downgraded_when_bounds_unresolved(self) -> None:
        """Caller supplying execution_finality='FINAL' is downgraded when side-effect bounds are unresolved [1, 2]."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_final_unres", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_final_unres",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_final_unres",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id="a2",
                attempt_number=2,
                effect_id=None,
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="105.0",
            ),
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality="FINAL",
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert res.passed is False

    def test_adversarial_caller_final_downgraded_when_external_status_unresolved(self) -> None:
        """Caller supplying execution_finality='FINAL' is downgraded when external status is UNRESOLVED_INTERVAL."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_final_ext_unres", tool_name="payment_svc", max_capacity=2)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_final_ext_unres",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            ),
            LedgerEvent(
                event_id=2,
                run_id="run_final_ext_unres",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id="a2",
                attempt_number=2,
                effect_id=None,
                request_fingerprint="fp_fin2",
                execution_status=ExecutionStatus.ERROR,
                commit_status=CommitStatus.UNCERTAIN,
                simulated_timestamp="105.0",
            ),
        ]
        assertions = ScenarioAssertions(expected_side_effects=2, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality="FINAL",
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE

    def test_adversarial_caller_final_downgraded_when_infrastructure_failure(self) -> None:
        """Caller supplying execution_finality='FINAL' is downgraded when primary infrastructure is not CLEAN."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_final_infra_fail", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_final_infra_fail",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality="FINAL",
            infrastructure_status=PrimaryInfrastructureStatus.PROCESS_TERMINATED.value,
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR

    def test_adversarial_caller_final_downgraded_when_identity_missing(self) -> None:
        """Caller supplying execution_finality='FINAL' is downgraded when events have missing attempt_id."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_final_noid", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_final_noid",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id=None,
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality="FINAL",
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert res.passed is False

    def test_adversarial_caller_final_downgraded_when_agent_not_terminated(self) -> None:
        """Caller supplying execution_finality='FINAL' is downgraded when agent has not definitively terminated."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_final_run", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_final_run",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_fin",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(status=CompletionStatus.UNKNOWN)

        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality="FINAL",
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE

    def test_adversarial_finality_missing_proof_when_required(self) -> None:
        """When require_orchestrator_proof=True and no proof is passed, finality is forced to PROVISIONAL."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH, orchestrator_authority=TEST_ORCH_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_fin_req", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_fin_req",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_fin",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality=None,
            require_orchestrator_proof=True,
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE

    def test_adversarial_finality_cross_run_proof_rejected(self) -> None:
        """AuthoritativeFinalityProof issued for run_alpha passed to evaluate run_beta raises validation error."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH, orchestrator_authority=TEST_ORCH_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_beta", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_beta",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        proof_alpha = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_alpha", evaluation_digest="digest_alpha")
        with pytest.raises(EvaluationInputValidationError):
            evaluator.evaluate(
                events,
                assertions,
                agent_result,
                execution_finality=proof_alpha,
                tool_metadata={"payment_svc": {"harness_attestation": att}},
            )

    def test_adversarial_finality_stale_proof_rejected(self) -> None:
        """AuthoritativeFinalityProof with mismatched evaluation digest is rejected as invalid."""
        stale_proof = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_stale", evaluation_digest="digest_old")
        assert not TEST_ORCH_AUTH.verify(stale_proof, for_run_id="run_stale", for_evaluation_digest="digest_new")

    @pytest.mark.parametrize("invalid_finality", ["INVALID", "COMPLETE", "DONE", 123, True, False, []])
    def test_adversarial_invalid_finality_strings_raise_validation_error(self, invalid_finality: Any) -> None:
        """Supplying invalid execution_finality values raises EvaluationInputValidationError."""
        evaluator = RecoveryEvaluator()
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_inv_fin",
                scenario_id="sc_fin",
                tool_name="t",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        with pytest.raises(EvaluationInputValidationError):
            evaluator.evaluate(events, assertions, agent_result, execution_finality=invalid_finality)

    def test_authoritative_finality_proof_validation(self) -> None:
        """AuthoritativeFinalityProof validates orchestrator signature; forged proof raises validation error."""
        evaluator = RecoveryEvaluator(runtime_authority=TEST_RUNTIME_AUTH, orchestrator_authority=TEST_ORCH_AUTH)
        att = TEST_RUNTIME_AUTH.attest(run_id="run_auth_fin", tool_name="payment_svc", max_capacity=1)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_auth_fin",
                scenario_id="sc_fin",
                tool_name="payment_svc",
                action_type="charge",
                operation_id="op_auth",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_fin",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        expected_digest = compute_evaluation_digest(
            run_id="run_auth_fin",
            events=events,
            expected_side_effects=assertions.expected_side_effects,
            expect_agent_success=assertions.expect_agent_success,
            agent_status=agent_result.status.value,
        )
        valid_proof = TEST_ORCH_AUTH.issue_finality_proof(
            run_id="run_auth_fin",
            evaluation_digest=expected_digest,
        )
        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            execution_finality=valid_proof,
            tool_metadata={"payment_svc": {"harness_attestation": att}},
        )
        assert res.verdict == EvaluationVerdict.PASS

        forged_proof = AuthoritativeFinalityProof(
            run_id="run_auth_fin",
            orchestrator_id="Orchestrator",
            finality="FINAL",
            signature="forged_signature_12345",
        )
        with pytest.raises(EvaluationInputValidationError):
            evaluator.evaluate(events, assertions, agent_result, execution_finality=forged_proof)

    # --------------------------------------------------------------------------
    # Section E: Registry Isolation Tests
    # --------------------------------------------------------------------------

    def test_registry_isolation_between_two_independent_runs(self) -> None:
        """Two separate ToolRegistry instances bound to independent runs remain completely isolated."""
        class ToolA(ToolService):
            name = "tool_a"

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        class ToolB(ToolService):
            name = "tool_b"

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        reg1 = ToolRegistry()
        reg1.register(ToolA())
        reg1.bind_to_run("run_alpha")

        reg2 = ToolRegistry()
        reg2.register(ToolB())
        reg2.bind_to_run("run_beta")

        try:
            resolved_1 = ToolRegistry.get_for_run("run_alpha")
            assert resolved_1 is reg1
            assert "tool_a" in resolved_1
            assert "tool_b" not in resolved_1

            resolved_2 = ToolRegistry.get_for_run("run_beta")
            assert resolved_2 is reg2
            assert "tool_b" in resolved_2
            assert "tool_a" not in resolved_2

            assert ToolRegistry.get_for_run("run_gamma") is None
        finally:
            reg1.unbind_run("run_alpha")
            reg2.unbind_run("run_beta")

    def test_registry_duplicate_run_binding_behavior(self) -> None:
        """Binding the same run_id to the same registry is an idempotent no-op; to a different registry raises ValueError."""
        reg1 = ToolRegistry()
        reg2 = ToolRegistry()

        reg1.bind_to_run("run_dup_check")
        reg1.bind_to_run("run_dup_check")

        try:
            with pytest.raises(ValueError, match="already bound"):
                reg2.bind_to_run("run_dup_check")
        finally:
            reg1.unbind_run("run_dup_check")

    def test_registry_incorrect_unbinding_protection(self) -> None:
        """Registry A cannot unbind a run bound to Registry B."""
        reg_a = ToolRegistry()
        reg_b = ToolRegistry()

        reg_a.bind_to_run("run_owner_test")
        try:
            reg_b.unbind_run("run_owner_test")
            assert ToolRegistry.get_for_run("run_owner_test") is reg_a
        finally:
            reg_a.unbind_run("run_owner_test")
            assert ToolRegistry.get_for_run("run_owner_test") is None

    def test_registry_stale_binding_cleanup_in_runner(self) -> None:
        """ExecutionEngine unbinds the run from the registry upon completion, preventing stale leaks."""
        from recoverybench.engine.runner import ExecutionEngine
        from recoverybench.ledger.memory import InMemoryLedger
        from recoverybench.scenarios.schema import ScenarioModel
        from recoverybench.tools.payment import MockPaymentService

        reg = ToolRegistry()
        reg.register(MockPaymentService(enforce_capacity=True))
        ledger = InMemoryLedger()
        engine = ExecutionEngine(ledger=ledger, tool_registry=reg)

        scenario = ScenarioModel(
            schema_version=1,
            scenario_id="sc_clean_test",
            name="Clean Test",
            description="Test",
            target_tool="payment_gateway",
            target_action="charge",
            task={"action": "charge", "amount": 100},
            assertions=ScenarioAssertions(expected_side_effects=1, expect_agent_success=True),
        )

        def simple_agent(context: Any, tool: Any) -> dict[str, Any]:
            return cast(dict[str, Any], tool("payment_gateway", "charge", {"amount": 100}))

        run_id = "run_cleanup_check"
        result = engine.run(scenario, simple_agent, run_id=run_id)
        assert result.evaluation.passed is True
        assert ToolRegistry.get_for_run(run_id) is None

    def test_registry_cleanup_after_evaluation_exception(self) -> None:
        """ExecutionEngine unbinds the run from the registry even if an unhandled exception occurs."""
        from recoverybench.engine.runner import ExecutionEngine
        from recoverybench.ledger.memory import InMemoryLedger
        from recoverybench.scenarios.schema import ScenarioModel
        from recoverybench.tools.payment import MockPaymentService

        reg = ToolRegistry()
        reg.register(MockPaymentService(enforce_capacity=True))
        ledger = InMemoryLedger()
        engine = ExecutionEngine(ledger=ledger, tool_registry=reg)

        scenario = ScenarioModel(
            schema_version=1,
            scenario_id="sc_exc_test",
            name="Exc Test",
            description="Test",
            target_tool="payment_gateway",
            target_action="charge",
            task={},
            assertions=ScenarioAssertions(expected_side_effects=1),
        )

        def crashing_agent(context: Any, tool: Any) -> Any:
            raise RuntimeError("Fatal agent crash")

        run_id = "run_exc_cleanup"
        engine.run(scenario, crashing_agent, run_id=run_id)
        assert ToolRegistry.get_for_run(run_id) is None

    def test_registry_explicit_precedence_over_run_and_default(self) -> None:
        """An explicit tool_registry passed to evaluate() takes precedence over run-bound and default registries."""
        reg_default = ToolRegistry()
        reg_bound = ToolRegistry()
        reg_explicit = ToolRegistry()

        class ToolExp(ToolService):
            name = "target_tool"

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        reg_explicit.register(ToolExp())
        reg_bound.bind_to_run("run_prec")

        try:
            evaluator = RecoveryEvaluator(tool_registry=reg_default)
            events = [
                LedgerEvent(
                    event_id=1,
                    run_id="run_prec",
                    scenario_id="sc_prec",
                    tool_name="target_tool",
                    action_type="act",
                    attempt_id="a1",
                    attempt_number=1,
                    effect_id=None,
                    request_fingerprint="fp_p",
                    execution_status=ExecutionStatus.ERROR,
                    commit_status=CommitStatus.UNCERTAIN,
                    simulated_timestamp="100.0",
                )
            ]
            assertions = ScenarioAssertions(expected_side_effects=0)
            agent_result = AgentResult(success=True)

            result = evaluator.evaluate(
                events,
                assertions,
                agent_result,
                tool_registry=reg_explicit,
            )
            assert result is not None
        finally:
            reg_bound.unbind_run("run_prec")

    def test_registry_missing_behavior_returns_none(self) -> None:
        """ToolRegistry.get_for_run returns None for unknown or None run IDs without raising errors."""
        assert ToolRegistry.get_for_run(None) is None
        assert ToolRegistry.get_for_run("") is None
        assert ToolRegistry.get_for_run("non_existent_run_xyz") is None

    def test_declarative_mock_tool_preserves_trust_boundary_tier_c1(self) -> None:
        """@mock_tool annotation preserves developer declared provenance but enforces Tier C1 verified provenance."""
        from recoverybench.tools.declarative import mock_tool

        @mock_tool("untrusted_service", capacity_provenance=CapacityProvenanceTier.TIER_C4)
        def my_action(val: int) -> dict[str, Any]:
            return {"val": val}

        assert my_action.declared_provenance == CapacityProvenanceTier.TIER_C4
        assert my_action.verified_provenance == CapacityProvenanceTier.TIER_C1
        assert my_action.capacity_provenance == CapacityProvenanceTier.TIER_C1

    # --------------------------------------------------------------------------
    # Section F: Specific Attack Vector Regression Tests
    # --------------------------------------------------------------------------

    def test_attack_caller_authority_injection_via_tool_metadata_ignored(self) -> None:
        """Attack 1: Caller injects a rogue runtime_authority inside tool_metadata.

        The evaluator and accounting engine must strictly ignore tool_metadata['runtime_authority']
        and reject the forged attestation as Tier C1 unverified.
        """
        rogue_authority = HarnessRuntimeAuthority()
        harness_authority = HarnessRuntimeAuthority()
        evaluator = RecoveryEvaluator(runtime_authority=harness_authority)

        rogue_att = rogue_authority.attest(
            run_id="run_atk1",
            tool_name="rogue_svc",
            max_capacity=1,
            runtime_guarantee="RUNTIME_UNIQUE_CONSTRAINT",
        )

        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_atk1",
                scenario_id="sc_atk1",
                tool_name="rogue_svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_atk1",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
                details={"capacity_provenance": "TIER_C3", "max_capacity": 1},
            )
        ]

        # Adversary attempts to smuggle rogue_authority into tool_metadata
        malicious_tool_metadata = {
            "rogue_svc": {"harness_attestation": rogue_att},
            "runtime_authority": rogue_authority,
        }

        # Evaluator must ignore malicious_tool_metadata["runtime_authority"]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)
        res = evaluator.evaluate(
            events,
            assertions,
            agent_result,
            tool_metadata=malicious_tool_metadata,
            execution_finality="FINAL",
        )
        # Because rogue_att was signed by rogue_authority and NOT harness_authority,
        # it is rejected. verified_prov remains TIER_C1 -> unresolved_capacity=inf -> UNBOUNDED_AMBIGUITY
        # Condition 2 fails -> INSUFFICIENT_EVIDENCE
        assert res.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert res.passed is False

        # Direct test against _orig_calculate with explicit runtime_authority=harness_authority
        calc_res = _orig_calculate(
            events,
            expected_side_effects=1,
            tool_metadata=malicious_tool_metadata,
            runtime_authority=harness_authority,
        )
        assert calc_res.attempts["a1"].verified_provenance == CapacityProvenanceTier.TIER_C1
        assert calc_res.bounds.is_unbounded

    def test_attack_caller_audit_authority_injection_via_tool_metadata_ignored(self) -> None:
        """Attack 2: Caller injects a rogue audit_authority inside tool_metadata.

        The evaluator and accounting engine must strictly ignore tool_metadata['audit_authority'].
        """
        rogue_audit_auth = HarnessAuditAuthority()
        harness_audit_auth = HarnessAuditAuthority()

        rogue_evidence = rogue_audit_auth.issue_audit_evidence(
            run_id="run_atk2",
            scenario_id="sc_atk2",
            snapshot_id="snap_rogue",
            attempt_ids=["a1"],
            verified_effects=["eff_1"],
        )

        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_atk2",
                scenario_id="sc_atk2",
                tool_name="untrusted_svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_atk2",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]

        malicious_tool_metadata = {
            "audit_authority": rogue_audit_auth,
        }

        calc_res = _orig_calculate(
            events,
            expected_side_effects=1,
            reconciliation_audit=rogue_evidence,
            tool_metadata=malicious_tool_metadata,
            audit_authority=harness_audit_auth,
        )
        # Audit evidence signed by rogue authority must fail verification against harness_audit_auth
        assert calc_res.attempts["a1"].verified_provenance == CapacityProvenanceTier.TIER_C1
        assert calc_res.bounds.is_unbounded

    def test_attack_caller_orchestrator_authority_injection_via_tool_metadata_ignored(self) -> None:
        """Attack 3: Caller injects a rogue orchestrator_authority inside tool_metadata."""
        rogue_orch_auth = HarnessOrchestratorAuthority()
        harness_orch_auth = HarnessOrchestratorAuthority()
        evaluator = RecoveryEvaluator(orchestrator_authority=harness_orch_auth)

        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_atk3",
                scenario_id="sc_atk3",
                tool_name="svc",
                action_type="charge",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_atk3",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        digest = compute_evaluation_digest("run_atk3", events, 1, True, "SUCCEEDED")
        rogue_proof = rogue_orch_auth.issue_finality_proof(run_id="run_atk3", evaluation_digest=digest)

        malicious_metadata = {"orchestrator_authority": rogue_orch_auth}
        with pytest.raises(EvaluationInputValidationError, match="Invalid AuthoritativeFinalityProof signature"):
            evaluator.evaluate(
                events,
                assertions,
                agent_result,
                execution_finality=rogue_proof,
                tool_metadata=malicious_metadata,
            )

    def test_attack_public_signing_capabilities_not_in_public_all(self) -> None:
        """Attack 4: Verify signing authorities are not exposed in recoverybench.__all__."""
        import recoverybench

        public_exports = getattr(recoverybench, "__all__", [])
        forbidden = [
            "HarnessRuntimeAuthority",
            "HarnessAuditAuthority",
            "HarnessOrchestratorAuthority",
        ]
        for name in forbidden:
            assert name not in public_exports, f"CRITICAL: {name} exposed in public recoverybench.__all__"
            assert not hasattr(recoverybench, name), f"CRITICAL: {name} accessible on recoverybench package"

    def test_attack_empty_ledger_clean_failure_finality_bypass_blocked(self) -> None:
        """Attack 5A: Empty ledger with expected clean failure attempting finality bypass.

        Must NOT unconditionally return PASS when finality requirements or infrastructure conditions fail.
        """
        evaluator = RecoveryEvaluator()
        assertions = ScenarioAssertions(expected_side_effects=0, expect_agent_success=False)
        agent_result = AgentResult(success=False)

        # 1. require_orchestrator_proof=True with no proof -> target_finality forced to PROVISIONAL -> INSUFFICIENT_EVIDENCE
        res1 = evaluator.evaluate(
            [],
            assertions,
            agent_result,
            require_orchestrator_proof=True,
            execution_finality="FINAL",
        )
        assert res1.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert res1.passed is False

        # 2. Infrastructure timeout (WORKER_TIMED_OUT) must NOT return PASS -> INSUFFICIENT_EVIDENCE
        res2 = evaluator.evaluate(
            [],
            assertions,
            agent_result,
            infrastructure_status=PrimaryInfrastructureStatus.WORKER_TIMED_OUT.value,
            execution_finality="FINAL",
        )
        assert res2.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert res2.passed is False

        # 3. Infrastructure crash (PROCESS_TERMINATED) must NOT return PASS -> INFRASTRUCTURE_ERROR
        res3 = evaluator.evaluate(
            [],
            assertions,
            agent_result,
            infrastructure_status=PrimaryInfrastructureStatus.PROCESS_TERMINATED.value,
            execution_finality="FINAL",
        )
        assert res3.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR
        assert res3.passed is False

    def test_attack_empty_ledger_read_only_success_finality_bypass_blocked(self) -> None:
        """Attack 5B: Empty ledger with read-only success attempting finality bypass.

        Must NOT unconditionally return PASS when finality is PROVISIONAL or infrastructure failed.
        """
        evaluator = RecoveryEvaluator()
        assertions = ScenarioAssertions(expected_side_effects=0, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        # 1. execution_finality is PROVISIONAL -> Condition 1 fails -> INSUFFICIENT_EVIDENCE
        res1 = evaluator.evaluate(
            [],
            assertions,
            agent_result,
            execution_finality="PROVISIONAL",
        )
        assert res1.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE
        assert res1.passed is False

        # 2. Infrastructure failure (e.g. PROCESS_TERMINATED) -> INFRASTRUCTURE_ERROR
        res2 = evaluator.evaluate(
            [],
            assertions,
            agent_result,
            infrastructure_status=PrimaryInfrastructureStatus.PROCESS_TERMINATED.value,
            execution_finality="FINAL",
        )
        assert res2.verdict == EvaluationVerdict.INFRASTRUCTURE_ERROR
        assert res2.passed is False

    def test_attack_finality_proof_evaluation_digest_mismatch_rejected(self) -> None:
        """Attack 6: Finality proof issued for state D1 replayed against state D2.

        A proof with a different evaluation digest must raise EvaluationInputValidationError.
        """
        evaluator = RecoveryEvaluator(orchestrator_authority=TEST_ORCH_AUTH)
        events = [
            LedgerEvent(
                event_id=1,
                run_id="run_d_test",
                scenario_id="sc_d",
                tool_name="svc",
                action_type="act",
                attempt_id="a1",
                attempt_number=1,
                effect_id="eff_1",
                request_fingerprint="fp_d",
                execution_status=ExecutionStatus.SUCCESS,
                commit_status=CommitStatus.COMMITTED,
                simulated_timestamp="100.0",
            )
        ]
        assertions = ScenarioAssertions(expected_side_effects=1, expect_agent_success=True)
        agent_result = AgentResult(success=True)

        forged_digest_proof = TEST_ORCH_AUTH.issue_finality_proof(
            run_id="run_d_test",
            evaluation_digest="tampered_digest_00000000000000000000000000000000",
        )

        with pytest.raises(EvaluationInputValidationError, match="Invalid AuthoritativeFinalityProof signature"):
            evaluator.evaluate(
                events,
                assertions,
                agent_result,
                execution_finality=forged_digest_proof,
            )

    def test_attack_finality_proof_omitted_evaluation_digest_rejected(self) -> None:
        """Attack 7: Finality proof issued without evaluation digest or empty digest."""
        with pytest.raises((EvaluationInputValidationError, ValueError), match="evaluation_digest"):
            TEST_ORCH_AUTH.issue_finality_proof(run_id="run_no_digest", evaluation_digest="")

        # Empty digest in proof object fails verify()
        empty_digest_proof = AuthoritativeFinalityProof(
            run_id="run_no_digest",
            orchestrator_id="RecoveryBenchOrchestrator",
            finality="FINAL",
            evaluation_digest="",
            signature="sig",
        )
        assert not TEST_ORCH_AUTH.verify(empty_digest_proof, for_run_id="run_no_digest")

    def test_attack_unverified_tool_cannot_obtain_c3_attestation(self) -> None:
        """Attack 8: Unverified service claiming C3 fails demonstrate_runtime_capacity_enforcement."""
        from recoverybench.models import IdempotencyContract, IdempotencyGuarantee
        from recoverybench.tools.base import demonstrate_runtime_capacity_enforcement

        class SpoofedToolService(ToolService):
            name = "payment_gateway"
            is_runtime_enforced = True
            capacity_provenance = CapacityProvenanceTier.TIER_C3
            max_side_effect_capacity = 1
            # But guarantee is NONE and verified_service_deduplication is False!
            idempotency_contract = IdempotencyContract(
                guarantee=IdempotencyGuarantee.NONE,
                verified_service_deduplication=False,
            )

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        spoofed = SpoofedToolService()
        assert not demonstrate_runtime_capacity_enforcement(spoofed)

    def test_attack_c3_cannot_be_granted_via_inherited_defaults(self) -> None:
        """Attack 9: Subclass merely inheriting ToolService defaults fails closed to C1."""
        from recoverybench.tools.base import demonstrate_runtime_capacity_enforcement

        class InheritedDefaultService(ToolService):
            name = "custom_service"

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        service = InheritedDefaultService()
        assert not demonstrate_runtime_capacity_enforcement(service)

    def test_attack_c3_cannot_be_granted_via_self_declared_metadata(self) -> None:
        """Attack 10: Declaring TIER_C3 in metadata without verifiable enforcement fails closed."""
        from recoverybench.tools.base import demonstrate_runtime_capacity_enforcement

        class DeclaredC3Service(ToolService):
            name = "declared_c3_service"
            declared_provenance = CapacityProvenanceTier.TIER_C3
            capacity_provenance = CapacityProvenanceTier.TIER_C3
            is_runtime_enforced = True
            max_side_effect_capacity = 1

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                return ToolResult(success=True)

            def reset(self) -> None:
                pass

        service = DeclaredC3Service()
        assert not demonstrate_runtime_capacity_enforcement(service)

    def test_attack_c3_cannot_be_granted_via_storage_attribute_presence(self) -> None:
        """Attack 11: Simply having charges/invocations/idempotency_map dict does not grant C3."""
        from recoverybench.models import IdempotencyContract, IdempotencyGuarantee
        from recoverybench.tools.base import demonstrate_runtime_capacity_enforcement

        class StoragePresenceService(ToolService):
            name = "storage_spoofed_service"
            max_side_effect_capacity = 1
            declared_provenance = CapacityProvenanceTier.TIER_C3
            is_runtime_enforced = True
            idempotency_contract = IdempotencyContract(
                guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
                verified_service_deduplication=True,
            )

            def __init__(self) -> None:
                self.charges = {"fake": "charge"}
                self.idempotency_map = {"key": "value"}
                self.invocations: list[Any] = []

            def execute(self, action: str, params: dict[str, Any], idempotency_key: str | None = None, operation_id: str | None = None, attempt_id: str | None = None) -> ToolResult:
                # Replay does NOT deduplicate: always returns committed=True!
                return ToolResult(success=True, committed=True)

            def reset(self) -> None:
                pass

        service = StoragePresenceService()
        assert not demonstrate_runtime_capacity_enforcement(service)

    def test_genuine_runtime_enforcement_grants_c3(self) -> None:
        """Attack 12: Genuine services with verifiable runtime enforcement qualify for C3."""
        from recoverybench.tools.base import demonstrate_runtime_capacity_enforcement
        from recoverybench.tools.declarative import DeclarativeToolService
        from recoverybench.tools.payment import MockPaymentService

        payment = MockPaymentService(enforce_capacity=True)
        assert demonstrate_runtime_capacity_enforcement(payment)

        decl = DeclarativeToolService(
            name="test_tool",
            default_action="act",
            default_handler=lambda x: x,
            default_mutates=True,
            enforce_capacity=True,
        )
        assert demonstrate_runtime_capacity_enforcement(decl)

        payment_unconstrained = MockPaymentService(enforce_capacity=False)
        assert not demonstrate_runtime_capacity_enforcement(payment_unconstrained)

        decl_unconstrained = DeclarativeToolService(
            name="test_tool_unconstrained",
            default_action="act",
            default_handler=lambda x: x,
            default_mutates=True,
            enforce_capacity=False,
        )
        assert not demonstrate_runtime_capacity_enforcement(decl_unconstrained)

    def test_attack_c3_cannot_be_granted_via_self_authored_verifier(self) -> None:
        """Adversarial Test: Service implementing a self-authored verifier returning True cannot obtain C3."""
        from recoverybench.tools.base import demonstrate_runtime_capacity_enforcement

        class SelfAuthoredVerifierService(ToolService):
            name = "self_certifying_gateway"
            max_side_effect_capacity = 1
            declared_provenance = CapacityProvenanceTier.TIER_C3
            is_runtime_enforced = True
            idempotency_contract = IdempotencyContract(
                guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
                verified_service_deduplication=True,
            )

            def verify_runtime_capacity_enforcement(self) -> bool:
                # Maliciously returns True without performing real enforcement
                return True

            def execute(
                self,
                action: str,
                params: dict[str, Any],
                idempotency_key: str | None = None,
                operation_id: str | None = None,
                attempt_id: str | None = None,
            ) -> ToolResult:
                # Fails real deduplication or capacity: commits every time!
                return ToolResult(success=True, committed=True, effect_id=f"eff_{uuid.uuid4().hex}")

            def reset(self) -> None:
                pass

        service = SelfAuthoredVerifierService()
        assert not demonstrate_runtime_capacity_enforcement(service)

    def test_attack_c3_prevented_by_actual_capacity_overflow(self) -> None:
        """Adversarial Test: Service that commits beyond max_side_effect_capacity with distinct keys fails C3."""
        from recoverybench.tools.base import demonstrate_runtime_capacity_enforcement
        from recoverybench.tools.payment import MockPaymentService

        unconstrained_payment = MockPaymentService(enforce_capacity=False)
        assert not demonstrate_runtime_capacity_enforcement(unconstrained_payment)

    def test_c3_duplicate_replay_produces_exactly_one_committed_effect(self) -> None:
        """Adversarial Test: Service failing to deduplicate identical idempotency keys is denied C3."""
        from recoverybench.tools.base import demonstrate_runtime_capacity_enforcement

        class NoDedupService(ToolService):
            name = "nodedup_gateway"
            max_side_effect_capacity = 1
            declared_provenance = CapacityProvenanceTier.TIER_C3
            is_runtime_enforced = True
            idempotency_contract = IdempotencyContract(
                guarantee=IdempotencyGuarantee.STRICT_EXACTLY_ONCE,
                verified_service_deduplication=True,
            )

            def __init__(self) -> None:
                self.calls = 0

            def execute(
                self,
                action: str,
                params: dict[str, Any],
                idempotency_key: str | None = None,
                operation_id: str | None = None,
                attempt_id: str | None = None,
            ) -> ToolResult:
                self.calls += 1
                # Commits duplicate effect with distinct effect identity even with identical idempotency key
                return ToolResult(success=True, committed=True, effect_id=f"eff_dup_{self.calls}")

            def reset(self) -> None:
                self.calls = 0

        service = NoDedupService()
        assert not demonstrate_runtime_capacity_enforcement(service)

    def _make_test_event(self, event_id: int, effect_id: str | None, commit_status: CommitStatus) -> LedgerEvent:
        return LedgerEvent(
            event_id=event_id,
            run_id="run_test",
            scenario_id="sc_test",
            tool_name="payment_gateway",
            action_type="charge",
            operation_id="op_1",
            attempt_id=f"a_{event_id}",
            attempt_number=1,
            execution_status=ExecutionStatus.SUCCESS,
            commit_status=commit_status,
            effect_id=effect_id,
            request_fingerprint="fp_default",
            simulated_timestamp="2026-11-20T12:00:00Z",
        )

    def test_attack_finality_proof_tampered_attempt_rejected(self) -> None:
        """Attack 13: Tampering with attempts invalidates finality proof evaluation digest."""
        from recoverybench.models import AttemptStateVector, compute_evaluation_digest

        events = [self._make_test_event(1, "eff_1", CommitStatus.COMMITTED)]
        a1 = AttemptStateVector(attempt_id="att_1", max_capacity=1)
        a2_tampered = AttemptStateVector(attempt_id="att_1", max_capacity=2)

        d1 = compute_evaluation_digest("run_att", events, 1, True, "SUCCEEDED", attempts=[a1])
        d2 = compute_evaluation_digest("run_att", events, 1, True, "SUCCEEDED", attempts=[a2_tampered])
        assert d1 != d2

        proof = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_att", evaluation_digest=d1)
        assert TEST_ORCH_AUTH.verify(proof, for_run_id="run_att", for_evaluation_digest=d1)
        assert not TEST_ORCH_AUTH.verify(proof, for_run_id="run_att", for_evaluation_digest=d2)

    def test_attack_finality_proof_tampered_effects_rejected(self) -> None:
        """Attack 14: Tampering with expected side effects invalidates finality proof."""
        from recoverybench.models import compute_evaluation_digest

        events = [self._make_test_event(1, "eff_1", CommitStatus.COMMITTED)]
        d1 = compute_evaluation_digest("run_eff", events, 1, True, "SUCCEEDED")
        d2 = compute_evaluation_digest("run_eff", events, 2, True, "SUCCEEDED")
        assert d1 != d2

        proof = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_eff", evaluation_digest=d1)
        assert not TEST_ORCH_AUTH.verify(proof, for_run_id="run_eff", for_evaluation_digest=d2)

    def test_attack_finality_proof_tampered_fingerprint_rejected(self) -> None:
        """Attack 15: Tampering with request fingerprint invalidates finality proof."""
        from recoverybench.models import compute_evaluation_digest

        e1 = self._make_test_event(1, "eff_1", CommitStatus.COMMITTED)
        e1.request_fingerprint = "fp_original"
        e2 = self._make_test_event(1, "eff_1", CommitStatus.COMMITTED)
        e2.request_fingerprint = "fp_tampered"

        d1 = compute_evaluation_digest("run_fp", [e1], 1, True, "SUCCEEDED")
        d2 = compute_evaluation_digest("run_fp", [e2], 1, True, "SUCCEEDED")
        assert d1 != d2

        proof = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_fp", evaluation_digest=d1)
        assert not TEST_ORCH_AUTH.verify(proof, for_run_id="run_fp", for_evaluation_digest=d2)

    def test_attack_finality_proof_tampered_capacity_bounds_rejected(self) -> None:
        """Adversarial Test: Tampering with capacity bounds invalidates finality proof."""
        from recoverybench.models import SideEffectBounds, compute_evaluation_digest

        events = [self._make_test_event(1, "eff_1", CommitStatus.COMMITTED)]
        b1 = SideEffectBounds(s_min=1, s_max=1)
        b2 = SideEffectBounds(s_min=1, s_max=2)

        d1 = compute_evaluation_digest("run_bounds", events, 1, True, "SUCCEEDED", capacity_bounds=b1)
        d2 = compute_evaluation_digest("run_bounds", events, 1, True, "SUCCEEDED", capacity_bounds=b2)
        assert d1 != d2

        proof = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_bounds", evaluation_digest=d1)
        assert TEST_ORCH_AUTH.verify(proof, for_run_id="run_bounds", for_evaluation_digest=d1)
        assert not TEST_ORCH_AUTH.verify(proof, for_run_id="run_bounds", for_evaluation_digest=d2)

    def test_attack_finality_proof_tampered_infrastructure_status_rejected(self) -> None:
        """Attack 16: Tampering with infrastructure status invalidates finality proof."""
        from recoverybench.models import compute_evaluation_digest

        events = [self._make_test_event(1, "eff_1", CommitStatus.COMMITTED)]
        d_clean = compute_evaluation_digest("run_inf", events, 1, True, "SUCCEEDED", infrastructure_status="CLEAN")
        d_crashed = compute_evaluation_digest("run_inf", events, 1, True, "SUCCEEDED", infrastructure_status="PROCESS_TERMINATED")
        assert d_clean != d_crashed

        proof_clean = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_inf", evaluation_digest=d_clean)
        assert TEST_ORCH_AUTH.verify(proof_clean, for_run_id="run_inf", for_evaluation_digest=d_clean)
        assert not TEST_ORCH_AUTH.verify(proof_clean, for_run_id="run_inf", for_evaluation_digest=d_crashed)

    def test_attack_finality_proof_tampered_reconciliation_audit_rejected(self) -> None:
        """Attack 17: Tampering with reconciliation audit evidence invalidates finality proof."""
        from recoverybench.models import ReconciliationAuditEvidence, compute_evaluation_digest

        events = [self._make_test_event(1, "eff_1", CommitStatus.COMMITTED)]
        audit_legit = ReconciliationAuditEvidence(
            run_id="run_audit",
            scenario_id="sc_1",
            snapshot_id="snap_1",
            audit_session_id="aud_1",
            verified_effects={"eff_1"},
            signature="sig1",
        )
        audit_tampered = ReconciliationAuditEvidence(
            run_id="run_audit",
            scenario_id="sc_1",
            snapshot_id="snap_1",
            audit_session_id="aud_1",
            verified_effects={"eff_1", "eff_unauthorized_2"},
            signature="sig2",
        )

        d1 = compute_evaluation_digest("run_audit", events, 1, True, "SUCCEEDED", reconciliation_audit=audit_legit)
        d2 = compute_evaluation_digest("run_audit", events, 1, True, "SUCCEEDED", reconciliation_audit=audit_tampered)
        assert d1 != d2

        proof = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_audit", evaluation_digest=d1)
        assert not TEST_ORCH_AUTH.verify(proof, for_run_id="run_audit", for_evaluation_digest=d2)

    def test_attack_finality_proof_tampered_agent_status_rejected(self) -> None:
        """Attack 18: Tampering with agent status invalidates finality proof."""
        from recoverybench.models import compute_evaluation_digest

        events = [self._make_test_event(1, "eff_1", CommitStatus.COMMITTED)]
        d_succ = compute_evaluation_digest("run_st", events, 1, True, "SUCCEEDED")
        d_fail = compute_evaluation_digest("run_st", events, 1, True, "FAILED")
        assert d_succ != d_fail

        proof = TEST_ORCH_AUTH.issue_finality_proof(run_id="run_st", evaluation_digest=d_succ)
        assert not TEST_ORCH_AUTH.verify(proof, for_run_id="run_st", for_evaluation_digest=d_fail)

    def test_proof_issuance_and_verification_digest_parity(self) -> None:
        """Adversarial Test: Proof issuance in runner and verification in evaluator produce identical digests."""
        from recoverybench.engine.runner import ExecutionEngine
        from recoverybench.ledger.memory import InMemoryLedger
        from recoverybench.scenarios.schema import ScenarioModel
        from recoverybench.tools.payment import MockPaymentService

        reg = ToolRegistry()
        reg.register(MockPaymentService(enforce_capacity=True))
        ledger = InMemoryLedger()
        engine = ExecutionEngine(ledger=ledger, tool_registry=reg)

        scenario = ScenarioModel(
            schema_version=1,
            scenario_id="sc_parity_check",
            name="Parity Check",
            description="Test parity between issuance and verification",
            target_tool="payment_gateway",
            target_action="charge",
            task={"action": "charge", "amount": 100},
            assertions=ScenarioAssertions(expected_side_effects=1, expect_agent_success=True),
        )

        def simple_agent(context: Any, tool: Any) -> dict[str, Any]:
            return cast(dict[str, Any], tool("payment_gateway", "charge", {"amount": 100}, idempotency_key="key_parity_1"))

        run_id = "run_parity_check_1"
        result = engine.run(scenario, simple_agent, run_id=run_id)
        assert result.evaluation.passed is True
        assert result.evaluation.verdict == EvaluationVerdict.PASS
        assert result.evaluation.execution_finality == "FINAL"
