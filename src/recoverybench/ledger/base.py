"""Abstract interface and protocol for the Effect Ledger."""

from abc import ABC, abstractmethod

from recoverybench.models import CommitStatus, LedgerEvent, RunSummary


class Ledger(ABC):
    """Abstract base class for append-only Effect Ledgers."""

    @abstractmethod
    def append(self, event: LedgerEvent) -> LedgerEvent:
        """Append an event to the ledger and return it with assigned event_id."""
        raise NotImplementedError

    @abstractmethod
    def get_events(self, run_id: str) -> list[LedgerEvent]:
        """Retrieve all recorded events for a specific run in monotonic order."""
        raise NotImplementedError

    @abstractmethod
    def get_committed_effects(self, run_id: str) -> list[LedgerEvent]:
        """Retrieve all events with COMMITTED status for a run."""
        raise NotImplementedError

    @abstractmethod
    def record_run(self, summary: RunSummary) -> None:
        """Persist high-level metadata for an executed run."""
        raise NotImplementedError

    @abstractmethod
    def get_run(self, run_id: str) -> RunSummary | None:
        """Retrieve run metadata by run_id."""
        raise NotImplementedError

    @abstractmethod
    def list_runs(self, limit: int = 50) -> list[RunSummary]:
        """List historical runs, ordered by most recent."""
        raise NotImplementedError

    @abstractmethod
    def clear_run(self, run_id: str) -> None:
        """Clear historical events and metadata for a specific run_id."""
        raise NotImplementedError

    def count_attempts(self, run_id: str, request_fingerprint: str) -> int:
        """Count attempts made for a specific request fingerprint in a run."""
        events = self.get_events(run_id)
        return sum(1 for e in events if e.request_fingerprint == request_fingerprint)

    def count_committed_effects(self, run_id: str) -> int:
        """Count total committed side-effects for a run."""
        return len([e for e in self.get_events(run_id) if e.commit_status == CommitStatus.COMMITTED])

    def close(self) -> None:
        """Release any resources or connections held by the ledger."""
        return None
