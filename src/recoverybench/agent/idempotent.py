"""Safe idempotent payment agent using stable transaction idempotency keys."""

import hashlib
import json

from recoverybench.agent.base import AgentAdapter, AgentResult, TaskContext, ToolCaller
from recoverybench.errors import MalformedResponseError, RateLimitError, ToolError


class IdempotentPaymentAgent(AgentAdapter):
    """A resilient agent that uses stable idempotency keys and exponential backoff.

    In the event of network drops or timeouts after server commit,
    it re-presents the exact same idempotency key, allowing the payment gateway
    to recognize the retry and return the original receipt without duplicating charges.
    """

    agent_id: str = "idempotent_payment_agent"

    def __init__(self, max_retries: int = 3, initial_backoff: float = 0.5) -> None:
        self.max_retries = max_retries
        self.initial_backoff = initial_backoff

    def _generate_stable_idempotency_key(self, scenario_id: str, params: dict[str, object]) -> str:
        """Derive a deterministic, stable idempotency key for this logical transaction."""
        serialized = json.dumps({"scenario": scenario_id, "params": params}, sort_keys=True)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
        return f"idem_{digest}"

    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Execute payment with stable idempotency and backoff."""
        # 1. Establish stable idempotency key BEFORE any calls
        stable_key = self._generate_stable_idempotency_key(task.scenario_id, task.task_params)
        attempts = 0
        last_error = ""
        backoff = self.initial_backoff

        for attempt in range(1, self.max_retries + 1):
            attempts = attempt

            try:
                # Present the stable idempotency key on every attempt
                response = tool_call(
                    "payment_gateway",
                    "charge",
                    task.task_params,
                    stable_key,
                )
                # Verify payload integrity
                if response.get("_corrupted") or "charge_id" not in response:
                    raise MalformedResponseError(
                        "Received corrupted or truncated response payload from payment gateway"
                    )
                return AgentResult(
                    success=True,
                    data=response,
                    attempts=attempts,
                    status_message=f"Payment recovered and confirmed on attempt {attempts}",
                )
            except RateLimitError as rle:
                last_error = str(rle)
                # Respect Retry-After header hint
                task.clock.sleep(rle.retry_after_s)
                continue
            except ToolError as te:
                last_error = str(te)
                # Deterministic virtual sleep backoff
                task.clock.sleep(backoff)
                backoff *= 2.0
                continue

        return AgentResult(
            success=False,
            error=last_error,
            attempts=attempts,
            status_message=f"Transaction halted after {attempts} attempts: {last_error}",
        )
