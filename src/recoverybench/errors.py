"""Typed exceptions for RecoveryBench."""

from typing import Any


class RecoveryBenchError(Exception):
    """Base exception for all RecoveryBench errors."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class ConfigurationError(RecoveryBenchError):
    """Raised when configuration or scenario files are missing, malformed, or invalid."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


class ScenarioValidationError(RecoveryBenchError):
    """Raised when a scenario fails schema or semantic validation."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        self.errors = errors or []
        full_msg = message
        if self.errors:
            full_msg = f"{message}: {'; '.join(self.errors)}"
        super().__init__(full_msg, exit_code=2)


class ReportValidationError(RecoveryBenchError):
    """Raised when an execution report fails schema or semantic validation."""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        self.errors = errors or []
        full_msg = message
        if self.errors:
            full_msg = f"{message}: {'; '.join(self.errors)}"
        super().__init__(full_msg, exit_code=2)


class InfrastructureError(RecoveryBenchError):
    """Raised when a runtime, environment, or system-level failure occurs."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=3)


class CircuitBreakerError(RecoveryBenchError):
    """Raised when an agent loop exceeds maximum permitted attempts or execution timeout."""

    def __init__(self, attempts: int, limit: int) -> None:
        super().__init__(
            f"Circuit breaker tripped: observed {attempts} attempts, exceeding limit of {limit}.",
            exit_code=1,
        )
        self.attempts = attempts
        self.limit = limit


class ToolError(Exception):
    """Base exception raised during tool invocation."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ToolTimeoutError(ToolError):
    """Raised when a tool call times out (either before or after commit)."""

    def __init__(self, message: str = "Operation timed out", committed: bool = False) -> None:
        super().__init__(message, details={"committed": committed})
        self.committed = committed


class RateLimitError(ToolError):
    """Raised when a tool endpoint returns HTTP 429 Too Many Requests."""

    def __init__(self, message: str = "Rate limit exceeded (HTTP 429)", retry_after_s: float = 1.0) -> None:
        super().__init__(message, details={"retry_after_s": retry_after_s})
        self.retry_after_s = retry_after_s


class ServiceUnavailableError(ToolError):
    """Raised when a tool endpoint returns HTTP 500 or 503."""

    def __init__(self, message: str = "Service temporarily unavailable (HTTP 500/503)") -> None:
        super().__init__(message)


class MalformedResponseError(ToolError):
    """Raised when tool response cannot be parsed or breaks schema expectations."""

    def __init__(self, message: str = "Malformed response payload received from tool") -> None:
        super().__init__(message)

class EvaluationInputValidationError(RecoveryBenchError, ValueError):
    """Raised when evaluation input parameters fail strict type or enum membership validation."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


class CapacityBreachIntegrityError(InfrastructureError):
    """Raised when observed side effects exceed declared or enforced tool capacity."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class IdempotencyCorrelationError(RecoveryBenchError):
    """Raised when idempotency metadata is contradictory, invalid, or fails correlation constraints."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


class ConflictingEventIdentityError(InfrastructureError):
    """Raised when an incoming event has an existing event_id or sequence identity but conflicting payload."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


class ConcurrencyViolationError(RecoveryBenchError):
    """Raised when concurrent operations violate safety invariants (e.g. registry reset during active worker)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=3)


class IncompatibleSchemaVersionError(RecoveryBenchError):
    """Raised when a report cannot be losslessly projected into the requested schema version."""

    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


class ProcessTaintedError(InfrastructureError):
    """Raised when attempting execution in a process tainted by a previous uncontained worker leak."""

    def __init__(self, message: str = "Process is tainted by previous worker leak. Restart required.") -> None:
        super().__init__(message)


class ProcessTerminatedError(InfrastructureError):
    """Raised when an isolated tool worker child process exceeds deadline and is forcibly terminated."""

    def __init__(self, message: str, child_pid: int | None = None) -> None:
        super().__init__(message)
        self.child_pid = child_pid


class ProcessContainmentError(InfrastructureError):
    """Raised when an uncooperative child process cannot be confirmed terminated by the OS."""

    def __init__(self, message: str, child_pid: int | None = None) -> None:
        super().__init__(message)
        self.child_pid = child_pid
