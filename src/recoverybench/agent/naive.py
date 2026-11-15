"""Naive payment agent implementation demonstrating unmanaged retry hazard."""

import uuid

from recoverybench.agent.base import AgentAdapter, AgentResult, TaskContext, ToolCaller
from recoverybench.errors import ToolError


class NaivePaymentAgent(AgentAdapter):
    """An unsafe agent that retries failures without a stable idempotency key.

    In the event of a network timeout after server commit (timeout_after_commit),
    this agent issues a new request with a brand new or missing idempotency key,
    creating duplicate charges in production.
    """

    agent_id: str = "naive_payment_agent"

    def __init__(self, max_retries: int = 3, use_random_idempotency_key: bool = True) -> None:
        self.max_retries = max_retries
        self.use_random_idempotency_key = use_random_idempotency_key

    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Execute payment with naive blind retry logic."""
        attempts = 0
        last_error = ""

        for attempt in range(1, self.max_retries + 1):
            attempts = attempt
            # Flaw: Either passes None (no idempotency support) or generates
            # a brand new random key on every attempt, preventing the server from recognizing retries
            flawed_key = f"key_{uuid.uuid4().hex[:8]}" if self.use_random_idempotency_key else None

            try:
                response = tool_call(
                    "payment_gateway",
                    "charge",
                    task.task_params,
                    flawed_key,
                )
                return AgentResult(
                    success=True,
                    data=response,
                    attempts=attempts,
                    status_message=f"Payment succeeded on attempt {attempts}",
                )
            except ToolError as e:
                last_error = str(e)
                # Virtual sleep for backoff
                task.clock.sleep(1.0)
                continue

        return AgentResult(
            success=False,
            error=last_error,
            attempts=attempts,
            status_message=f"Payment failed after {attempts} attempts: {last_error}",
        )
