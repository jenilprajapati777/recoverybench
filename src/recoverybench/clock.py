"""Deterministic virtual clock for simulated execution time."""

from datetime import UTC, datetime


class VirtualClock:
    """Simulated clock advancing deterministically without wall-clock sleep."""

    def __init__(self, start_timestamp: float = 1704067200.0) -> None:
        """Initialize the virtual clock.

        Args:
            start_timestamp: Unix epoch in seconds. Defaults to 2024-01-01T00:00:00 UTC.
        """
        self._current_time: float = start_timestamp
        self._initial_time: float = start_timestamp

    @property
    def timestamp(self) -> float:
        """Current virtual timestamp in epoch seconds."""
        return self._current_time

    def now(self) -> datetime:
        """Current virtual time as a timezone-aware UTC datetime."""
        return datetime.fromtimestamp(self._current_time, tz=UTC)

    def now_iso(self) -> str:
        """Current virtual time formatted as an ISO 8601 string."""
        return self.now().isoformat()

    def advance(self, seconds: float) -> None:
        """Advance the simulated clock by the specified number of seconds."""
        if seconds < 0:
            raise ValueError(f"Cannot advance clock by negative duration: {seconds}")
        self._current_time += seconds

    def sleep(self, seconds: float) -> None:
        """Simulate a sleep delay by advancing the clock without wall-clock wait."""
        self.advance(seconds)

    def elapsed_seconds(self) -> float:
        """Return total elapsed virtual seconds since clock initialization."""
        return self._current_time - self._initial_time

    def reset(self) -> None:
        """Reset virtual clock back to initial timestamp."""
        self._current_time = self._initial_time
