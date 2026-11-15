"""Fault injector intercepting tool calls with deterministic rules and seeds."""

import random
from typing import Any

from recoverybench.errors import (
    MalformedResponseError,
    RateLimitError,
    ServiceUnavailableError,
    ToolTimeoutError,
)
from recoverybench.faults.types import FaultConfig, FaultType


class FaultInjector:
    """Evaluates and injects deterministic faults during tool execution using reproducible seeds."""

    def __init__(self, faults: list[FaultConfig] | None = None, seed: int = 42) -> None:
        self.faults = faults or []
        self.seed = seed
        self._attempt_counters: dict[str, int] = {}

    def _get_key(self, tool_name: str, action: str) -> str:
        return f"{tool_name}:{action}"

    def register_attempt(self, tool_name: str, action: str) -> int:
        """Increment and return current attempt number for tool:action."""
        key = self._get_key(tool_name, action)
        self._attempt_counters[key] = self._attempt_counters.get(key, 0) + 1
        return self._attempt_counters[key]

    def get_matching_fault(self, tool_name: str, action: str) -> FaultConfig | None:
        """Find the first matching fault rule for this tool and action."""
        for fault in self.faults:
            if fault.target_tool == tool_name:
                if fault.target_action is None or fault.target_action == action:
                    return fault
        return None

    def should_trigger(self, fault: FaultConfig, attempt: int) -> bool:
        """Determine deterministically whether a fault should trigger for a given attempt."""
        if attempt > fault.failure_count:
            return False

        if fault.failure_rate < 1.0:
            # Deterministic pseudo-randomness based on seed and attempt
            deterministic_seed = fault.seed ^ (attempt * 10007)
            rng = random.Random(deterministic_seed)
            if rng.random() > fault.failure_rate:
                return False

        return True

    def evaluate_pre_commit(self, tool_name: str, action: str, attempt: int) -> Exception | None:
        """Check if a pre-commit fault should be injected (e.g. timeout before commit, 429, 500).

        Returns an Exception instance to raise, or None if tool execution should proceed.
        """
        fault = self.get_matching_fault(tool_name, action)
        if not fault or not self.should_trigger(fault, attempt):
            return None

        msg = fault.error_message

        if fault.type == FaultType.TIMEOUT_BEFORE_COMMIT:
            return ToolTimeoutError(msg or "Socket timeout before commit", committed=False)
        elif fault.type == FaultType.HTTP_429_THEN_SUCCESS:
            retry_after = fault.retry_after_seconds
            if fault.jitter_range:
                rng = random.Random(fault.seed ^ (attempt * 31))
                jitter = rng.uniform(fault.jitter_range[0], fault.jitter_range[1])
                retry_after += jitter
            return RateLimitError(
                msg or "Rate limit exceeded (HTTP 429)",
                retry_after_s=retry_after,
            )
        elif fault.type == FaultType.HTTP_500_TRANSIENT:
            return ServiceUnavailableError(msg or "Internal server error (HTTP 500)")
        elif fault.type == FaultType.MALFORMED_TOOL_RESPONSE and fault.malformed_mode == "raise_exception":
            return MalformedResponseError(msg or "Invalid JSON received from tool service")

        return None

    def evaluate_post_commit(self, tool_name: str, action: str, attempt: int) -> Exception | None:
        """Check if a post-commit fault should be injected (e.g. timeout after commit).

        Called AFTER the mock service has committed state to simulated storage,
        simulating a dropped network acknowledgement packet.
        """
        fault = self.get_matching_fault(tool_name, action)
        if not fault or not self.should_trigger(fault, attempt):
            return None

        msg = fault.error_message

        if fault.type == FaultType.TIMEOUT_AFTER_COMMIT:
            return ToolTimeoutError(
                msg or "Response dropped by network after transaction committed",
                committed=True,
            )

        return None

    def is_duplicate_delivery(self, tool_name: str, action: str, attempt: int) -> tuple[bool, int]:
        """Check if duplicate delivery should be simulated for this attempt.

        Returns (is_duplicate, duplicate_count).
        """
        fault = self.get_matching_fault(tool_name, action)
        if not fault or fault.type != FaultType.DUPLICATE_DELIVERY:
            return False, 0

        if not self.should_trigger(fault, attempt):
            return False, 0

        return True, fault.duplicate_count

    def evaluate_malformed_payload(
        self,
        tool_name: str,
        action: str,
        attempt: int,
        original_data: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Check if tool response dictionary should be corrupted (malformed_tool_response)."""
        fault = self.get_matching_fault(tool_name, action)
        if not fault or fault.type != FaultType.MALFORMED_TOOL_RESPONSE:
            return None

        if fault.malformed_mode != "corrupt_payload" or not self.should_trigger(fault, attempt):
            return None

        # Return payload missing essential fields or containing corrupt marker
        return {
            "_corrupted": True,
            "raw_chunk": '{"status": "succ... [TRUNCATED_JSON]',
            "error": "UNEXPECTED_EOF",
        }
