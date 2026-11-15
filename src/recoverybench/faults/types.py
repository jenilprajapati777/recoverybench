"""Fault types and parameters for deterministic injection."""

from enum import StrEnum

from pydantic import BaseModel, Field


class FaultType(StrEnum):
    """Supported fault injection modes."""

    TIMEOUT_BEFORE_COMMIT = "timeout_before_commit"
    TIMEOUT_AFTER_COMMIT = "timeout_after_commit"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    HTTP_429_THEN_SUCCESS = "http_429_then_success"
    HTTP_500_TRANSIENT = "http_500_transient"
    MALFORMED_TOOL_RESPONSE = "malformed_tool_response"


class FaultConfig(BaseModel):
    """Configuration for an active fault injection rule."""

    type: FaultType = Field(description="Fault mode to inject")
    target_tool: str = Field(description="Name of the tool targeted by this fault")
    target_action: str | None = Field(default=None, description="Optional target action e.g. charge")
    failure_count: int = Field(default=1, ge=1, description="Number of attempts to fail before succeeding")
    retry_after_seconds: float = Field(default=1.0, ge=0.0, description="Retry-After header hint for 429s")
    error_message: str | None = Field(default=None, description="Custom error message override")
    seed: int = Field(default=42, description="Deterministic seed for pseudo-random decisions")
    failure_rate: float = Field(default=1.0, ge=0.0, le=1.0, description="Probability of fault triggering (1.0 = 100%)")
    duplicate_count: int = Field(default=1, ge=1, description="Number of duplicate requests to deliver")
    malformed_mode: str = Field(
        default="raise_exception",
        description="Mode for malformed response: 'raise_exception' or 'corrupt_payload'",
    )
    jitter_range: tuple[float, float] | None = Field(
        default=None,
        description="Optional (min, max) seconds jitter to add to retry_after or latency",
    )
