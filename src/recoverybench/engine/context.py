"""Execution context and run state."""

from pydantic import BaseModel, Field

from recoverybench.models import (
    EvaluationResult,
    LedgerEvent,
    RunSummary,
)
from recoverybench.scenarios.schema import ScenarioModel


class RunResult(BaseModel):
    """Complete result package of a scenario execution run."""

    run_id: str
    scenario: ScenarioModel
    agent_id: str
    evaluation: EvaluationResult
    events: list[LedgerEvent] = Field(default_factory=list)
    summary: RunSummary
    agent_raw_result: dict[str, object] | None = None

    @property
    def passed(self) -> bool:
        """Convenience property returning whether all recovery assertions passed."""
        return self.evaluation.passed

    @property
    def verdict(self) -> str:
        """Convenience property returning the evaluation verdict string."""
        return self.evaluation.verdict.value

    @property
    def isolation_mode(self) -> str:
        """Convenience property returning the execution isolation mode string."""
        return self.summary.isolation_mode

    def assert_passed(self) -> None:
        """Assert that the test run passed; raises AssertionError with failure details on failure."""
        if not self.passed:
            reasons = "; ".join(self.evaluation.failure_reasons)
            raise AssertionError(f"RecoveryBench test '{self.scenario.scenario_id}' failed: {reasons}")
