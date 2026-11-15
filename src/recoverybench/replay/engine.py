"""Deterministic replay and trace reconstruction engine."""


from pydantic import BaseModel, Field

from recoverybench.ledger.base import Ledger
from recoverybench.models import LedgerEvent, RunSummary


class ReplayTrace(BaseModel):
    """Reconstructed trace of an execution run."""

    run_id: str
    summary: RunSummary
    events: list[LedgerEvent] = Field(default_factory=list)
    reconstructed_timeline: list[dict[str, object]] = Field(default_factory=list)


class ReplayEngine:
    """Reconstructs and inspects historical scenario execution traces."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    def replay_run(self, run_id: str) -> ReplayTrace | None:
        """Fetch historical run and reconstruct its event sequence."""
        summary = self.ledger.get_run(run_id)
        if not summary:
            return None

        events = self.ledger.get_events(run_id)
        timeline: list[dict[str, object]] = []

        for e in events:
            timeline.append(
                {
                    "step": e.event_id,
                    "operation_id": e.operation_id,
                    "attempt_id": e.attempt_id,
                    "attempt": e.attempt_number,
                    "tool": e.tool_name,
                    "action": e.action_type,
                    "execution_status": e.execution_status.value,
                    "commit_status": e.commit_status.value,
                    "effect_id": e.effect_id,
                    "idempotency_hash": e.idempotency_key_hash,
                    "request_fingerprint": e.request_fingerprint,
                    "error": e.error_type,
                    "timestamp": e.simulated_timestamp,
                }
            )

        return ReplayTrace(
            run_id=run_id,
            summary=summary,
            events=events,
            reconstructed_timeline=timeline,
        )
