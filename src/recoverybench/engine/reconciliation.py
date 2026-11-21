"""Reconciliation engine, deterministic projection, and multi-artifact report lifecycle."""

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any

from recoverybench.evaluator.accounting import (
    TwoLayerEffectAccountingEngine,
    derive_external_status,
)
from recoverybench.evaluator.rules import RuleEngine
from recoverybench.models import (
    CommitStatus,
    ExecutionStatus,
    HarnessAuditAuthority,
    HarnessRuntimeAuthority,
    LedgerEvent,
    PrimaryInfrastructureStatus,
    ReconciliationAuditEvidence,
    ReconciliationInfrastructureStatus,
)
from recoverybench.tools.base import ToolRegistry


def compute_report_hash(report_data: dict[str, Any]) -> str:
    """Compute canonical SHA-256 digest of a sealed primary execution report."""
    canonical = json.dumps(report_data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ReconciliationEngine:
    """Engine executing post-cutoff reconciliation and synthesizing supplemental reports.

    Implements ADR-004 Section 6 & 8:
    - Artifact 1: Immutable primary execution report.
    - Artifact 2: Append-only reconciliation ledger (LateOutcomeLedger).
    - Artifact 3: Supplemental reconciliation report (<run_id>.supplemental.<seq>.json).
    - View 4: Current reconciled evaluation view (projected from primary + supplementals).
    - Enforces Non-Erasure Invariant: behavioral verdict and primary infrastructure status
      remain permanently frozen and are never overwritten by post-hoc reconciliation.
    """

    def __init__(
        self,
        runtime_authority: HarnessRuntimeAuthority | None = None,
        audit_authority: HarnessAuditAuthority | None = None,
        ledger: Any | None = None,
        late_ledger: Any | None = None,
        tool_metadata: dict[str, Any] | None = None,
    ) -> None:
        self.runtime_authority = runtime_authority
        self.audit_authority = audit_authority
        self.ledger = ledger or late_ledger
        self.tool_metadata = tool_metadata

    def reconcile(
        self,
        primary_report: dict[str, Any],
        late_events: list[Any] | None = None,
        reconciliation_audit: ReconciliationAuditEvidence | None = None,
        tool_registry: ToolRegistry | None = None,
        reconciliation_succeeded: bool = True,
        sequence_number: int = 1,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Reconcile late outcomes against primary report.

        Args:
            primary_report: Dict representation of primary report (<run_id>.report.json).
            late_events: List of LateOutcomeRecord or late LedgerEvent objects.
            reconciliation_audit: Optional out-of-band audit evidence for Tier C4.
            tool_registry: Optional registry of mock tools.
            reconciliation_succeeded: False if reconciliation operation encountered a system failure.
            sequence_number: Sequence number of supplemental report (1, 2, ...).

        Returns:
            tuple: (supplemental_report_dict, reconciled_view_dict)
        """
        run_id = primary_report.get("run_id", "unknown_run")
        parent_sha256 = compute_report_hash(primary_report)

        if late_events is None and self.ledger is not None:
            if hasattr(self.ledger, "get_events"):
                late_events = self.ledger.get_events(run_id)
            elif hasattr(self.ledger, "get_late_records"):
                late_events = self.ledger.get_late_records(run_id)

        # 1. Extract Primary Report Dimensions
        primary_outcome = primary_report.get("outcome", {})
        behavioral_verdict = primary_outcome.get("behavioral_verdict") or primary_outcome.get("verdict", "INSUFFICIENT_EVIDENCE")
        primary_infra_status = primary_outcome.get("primary_infrastructure_status") or primary_outcome.get("infrastructure_status", PrimaryInfrastructureStatus.CLEAN.value)
        expected_side_effects = (
            primary_report.get("side_effects", {}).get("expected_count")
            if primary_report.get("side_effects", {}).get("expected_count") is not None
            else primary_report.get("scenario", {}).get("assertions", {}).get("expected_side_effects")
        )
        if expected_side_effects is None:
            expected_side_effects = primary_report.get("assertions", {}).get("expected_side_effects")

        # 2. Extract Base Primary Events
        combined_events: list[LedgerEvent] = []
        for raw_e in primary_report.get("events", []):
            if isinstance(raw_e, LedgerEvent):
                combined_events.append(raw_e)
            elif isinstance(raw_e, dict):
                # Reconstruct LedgerEvent
                c_stat_str = raw_e.get("commit_status", "NOT_APPLICABLE")
                try:
                    c_stat = CommitStatus(c_stat_str)
                except ValueError:
                    c_stat = CommitStatus.UNCERTAIN

                ex_stat_str = raw_e.get("execution_status", "SUCCESS")
                try:
                    ex_stat = ExecutionStatus(ex_stat_str)
                except ValueError:
                    ex_stat = ExecutionStatus.SUCCESS

                combined_events.append(
                    LedgerEvent(
                        event_id=raw_e.get("event_id"),
                        run_id=raw_e.get("run_id", run_id),
                        scenario_id=raw_e.get("scenario_id", ""),
                        tool_name=raw_e.get("tool_name") or raw_e.get("tool", ""),
                        action_type=raw_e.get("action_type") or raw_e.get("action", ""),
                        operation_id=raw_e.get("operation_id"),
                        attempt_id=raw_e.get("attempt_id"),
                        effect_id=raw_e.get("effect_id"),
                        idempotency_key_hash=raw_e.get("idempotency_key_hash"),
                        request_fingerprint=raw_e.get("request_fingerprint") or raw_e.get("fingerprint", "fp_default"),
                        attempt_number=raw_e.get("attempt_number", 1),
                        execution_status=ex_stat,
                        commit_status=c_stat,
                        simulated_timestamp=raw_e.get("simulated_timestamp") or raw_e.get("timestamp", ""),
                        error_type=raw_e.get("error_type"),
                        evidence_reference=raw_e.get("evidence_reference"),
                        details=raw_e.get("details", {}),
                    )
                )

        # 3. Ingest Late Events
        late_events_list = late_events or []
        for le in late_events_list:
            if hasattr(le, "payload") and isinstance(le.payload, dict):
                p = le.payload
                c_stat_str = p.get("commit_status", "COMMITTED")
                try:
                    c_stat = CommitStatus(c_stat_str)
                except ValueError:
                    c_stat = CommitStatus.COMMITTED

                ex_stat_str = p.get("execution_status", "SUCCESS")
                try:
                    ex_stat = ExecutionStatus(ex_stat_str)
                except ValueError:
                    ex_stat = ExecutionStatus.SUCCESS

                prior_event = next((e for e in combined_events if e.attempt_id == le.attempt_id), None)
                tool_name = p.get("tool_name") or p.get("tool") or (prior_event.tool_name if prior_event else "unknown_tool")
                action_type = p.get("action_type") or p.get("action") or (prior_event.action_type if prior_event else "execute")
                details = {"late_arrival": True, "writer_id": le.writer_id, **p.get("details", {})}
                if prior_event and prior_event.details:
                    for k in ("capacity_provenance", "max_capacity", "idempotency_contract"):
                        if k in prior_event.details and k not in details:
                            details[k] = prior_event.details[k]

                combined_events.append(
                    LedgerEvent(
                        event_id=len(combined_events) + 1,
                        run_id=le.run_id,
                        scenario_id=primary_report.get("scenario", {}).get("scenario_id", ""),
                        tool_name=tool_name,
                        action_type=action_type,
                        operation_id=le.operation_id,
                        attempt_id=le.attempt_id,
                        effect_id=p.get("effect_id"),
                        idempotency_key_hash=p.get("idempotency_key_hash"),
                        request_fingerprint=le.payload_fingerprint,
                        attempt_number=p.get("attempt_number", 1),
                        execution_status=ex_stat,
                        commit_status=c_stat,
                        simulated_timestamp=le.emitted_timestamp,
                        error_type=p.get("error_type"),
                        evidence_reference=p.get("evidence_reference"),
                        details=details,
                    )
                )
            elif isinstance(le, LedgerEvent):
                combined_events.append(le)

        # Build effective tool metadata & runtime authority for reconciliation
        effective_tool_meta: dict[str, Any] = dict(self.tool_metadata) if self.tool_metadata else {}
        effective_runtime_auth = self.runtime_authority

        primary_bounds = primary_report.get("side_effects", {}).get("bounds", [0, 0])
        is_primary_finite = len(primary_bounds) == 2 and primary_bounds[1] != "infinity" and primary_bounds[1] != math.inf

        if is_primary_finite:
            if effective_runtime_auth is None:
                effective_runtime_auth = HarnessRuntimeAuthority()
            for evt in combined_events:
                t_name = evt.tool_name
                if t_name and t_name not in effective_tool_meta:
                    prov = evt.details.get("capacity_provenance") if evt.details else None
                    if prov == "TIER_C3":
                        max_c = evt.details.get("max_capacity", 1)
                        att = effective_runtime_auth.attest(
                            run_id=run_id,
                            tool_name=t_name,
                            max_capacity=max_c,
                        )
                        effective_tool_meta[t_name] = {
                            "harness_attestation": att,
                            "verified_service_deduplication": True,
                        }

        # 4. Re-calculate Two-Layer Accounting with combined evidence
        accounting_res = TwoLayerEffectAccountingEngine.calculate(
            events=combined_events,
            tool_registry=tool_registry,
            tool_metadata=effective_tool_meta,
            expected_side_effects=expected_side_effects,
            reconciliation_audit=reconciliation_audit,
            runtime_authority=effective_runtime_auth,
            audit_authority=self.audit_authority,
        )

        reconciled_bounds = accounting_res.bounds.to_list()
        reconciled_external_status = derive_external_status(
            accounting_res.bounds,
            expected_side_effects=expected_side_effects,
            contradictory_evidence=accounting_res.contradictory_evidence,
        )

        # 5. Dual Infrastructure Status Resolution
        # Reconciliation status is independent from primary infrastructure status
        recon_infra_status = (
            ReconciliationInfrastructureStatus.CLEAN.value
            if reconciliation_succeeded
            else ReconciliationInfrastructureStatus.RECONCILIATION_FAILED.value
        )

        # Synthesize Overall Reconciled Outcome
        reconciled_overall_outcome = RuleEngine.aggregate_outcome(
            behavioral_verdict=behavioral_verdict,
            external_status=reconciled_external_status,
            infrastructure_status=primary_infra_status,
            expected_side_effects=expected_side_effects,
            execution_finality="FINAL",
            reconciliation_infrastructure_status=recon_infra_status,
        )

        created_at = datetime.now(UTC).isoformat()

        # 6. Build Artifact 3: Supplemental Reconciliation Report
        supplemental_report = {
            "report_schema_version": 2,
            "run_id": run_id,
            "sequence_number": sequence_number,
            "parent_report_sha256": parent_sha256,
            "created_at": created_at,
            "late_events_count": len(late_events_list),
            "reconciled_outcome": {
                "behavioral_verdict": behavioral_verdict,
                "side_effect_status": reconciled_external_status,
                "primary_infrastructure_status": primary_infra_status,
                "reconciliation_infrastructure_status": recon_infra_status,
                "overall_outcome": reconciled_overall_outcome,
                "execution_finality": "FINAL",
            },
            "side_effects": {
                "bounds": reconciled_bounds,
                "verified_effects": sorted(list(accounting_res.verified_effects)),
                "actual_count": len(accounting_res.verified_effects),
            },
        }

        # 7. Build View 4: Projected Reconciled Evaluation View
        reconciled_view = dict(primary_report)
        reconciled_view["report_schema_version"] = 2
        reconciled_view["outcome"] = {
            "behavioral_verdict": behavioral_verdict,
            "side_effect_status": reconciled_external_status,
            "primary_infrastructure_status": primary_infra_status,
            "reconciliation_infrastructure_status": recon_infra_status,
            "overall_outcome": reconciled_overall_outcome,
            "verdict": reconciled_overall_outcome,  # Backward compatibility
            "execution_finality": "FINAL",
        }
        reconciled_view["side_effects"] = {
            "bounds": reconciled_bounds,
            "actual_count": len(accounting_res.verified_effects),
            "uncertain_count": 0 if not accounting_res.bounds.is_unbounded else 1,
            "verified_effects": sorted(list(accounting_res.verified_effects)),
        }
        reconciled_view["supplemental_reconciliation"] = {
            "sequence_number": sequence_number,
            "parent_report_sha256": parent_sha256,
            "reconciled_at": created_at,
        }

        return supplemental_report, reconciled_view

    def generate_supplemental_report(
        self,
        primary_report: dict[str, Any],
        late_events: list[Any] | None = None,
        reconciliation_audit: ReconciliationAuditEvidence | None = None,
        tool_registry: ToolRegistry | None = None,
        reconciliation_succeeded: bool = True,
        sequence_number: int = 1,
        cutoff_timestamp: str | None = None,
    ) -> Any:
        """Generate supplemental report object supporting dot-notation access."""
        import types

        supp_dict, _ = self.reconcile(
            primary_report=primary_report,
            late_events=late_events,
            reconciliation_audit=reconciliation_audit,
            tool_registry=tool_registry,
            reconciliation_succeeded=reconciliation_succeeded,
            sequence_number=sequence_number,
        )

        def to_obj(d: Any) -> Any:
            if isinstance(d, dict):
                return types.SimpleNamespace(**{k: to_obj(v) for k, v in d.items()})
            elif isinstance(d, list):
                return [to_obj(item) for item in d]
            return d

        return to_obj(supp_dict)

    @staticmethod
    def project_reconciled_view(
        primary_report: dict[str, Any],
        supplemental_reports: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Project current evaluation view from primary report and ordered supplemental reports."""
        if not supplemental_reports:
            return dict(primary_report)

        # Sort supplementals by sequence_number
        ordered = sorted(supplemental_reports, key=lambda s: s.get("sequence_number", 1))
        latest = ordered[-1]

        reconciled = dict(primary_report)
        reconciled["report_schema_version"] = 2
        rec_outcome = latest.get("reconciled_outcome", {})
        reconciled["outcome"] = {
            "behavioral_verdict": primary_report.get("outcome", {}).get("behavioral_verdict") or primary_report.get("outcome", {}).get("verdict"),
            "side_effect_status": rec_outcome.get("side_effect_status"),
            "primary_infrastructure_status": rec_outcome.get("primary_infrastructure_status"),
            "reconciliation_infrastructure_status": rec_outcome.get("reconciliation_infrastructure_status"),
            "overall_outcome": rec_outcome.get("overall_outcome"),
            "verdict": rec_outcome.get("overall_outcome"),
            "execution_finality": rec_outcome.get("execution_finality", "FINAL"),
        }
        reconciled["side_effects"] = dict(latest.get("side_effects", {}))
        reconciled["supplemental_reconciliation"] = {
            "sequence_number": latest.get("sequence_number"),
            "parent_report_sha256": latest.get("parent_report_sha256"),
            "reconciled_at": latest.get("created_at"),
        }
        return reconciled
