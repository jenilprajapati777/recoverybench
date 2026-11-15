"""In-memory append-only effect ledger."""


from recoverybench.ledger.base import Ledger
from recoverybench.models import CommitStatus, LedgerEvent, RunSummary


class InMemoryLedger(Ledger):
    """In-memory implementation of the append-only Effect Ledger."""

    def __init__(self) -> None:
        self._events: list[LedgerEvent] = []
        self._runs: dict[str, RunSummary] = {}
        self._next_id: int = 1

    def append(self, event: LedgerEvent) -> LedgerEvent:
        """Append an event to memory with a monotonically assigned event_id."""
        event_with_id = event.model_copy(update={"event_id": self._next_id})
        self._next_id += 1
        self._events.append(event_with_id)
        return event_with_id

    def get_events(self, run_id: str) -> list[LedgerEvent]:
        """Return all events for a run ordered by event_id."""
        return [e for e in self._events if e.run_id == run_id]

    def get_committed_effects(self, run_id: str) -> list[LedgerEvent]:
        """Return all committed events for a run."""
        return [e for e in self._events if e.run_id == run_id and e.commit_status == CommitStatus.COMMITTED]

    def record_run(self, summary: RunSummary) -> None:
        """Save a run summary to memory."""
        self._runs[summary.run_id] = summary

    def get_run(self, run_id: str) -> RunSummary | None:
        """Retrieve run summary by run_id."""
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        """Return all runs in reverse chronological order."""
        runs = list(self._runs.values())
        runs.reverse()
        return runs[:limit]

    def clear_run(self, run_id: str) -> None:
        """Clear historical events and metadata for a run_id."""
        self._events = [e for e in self._events if e.run_id != run_id]
        self._runs.pop(run_id, None)
