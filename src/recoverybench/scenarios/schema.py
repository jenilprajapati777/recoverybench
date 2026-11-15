"""Pydantic schema definitions for RecoveryBench scenarios."""

from typing import Any

from pydantic import BaseModel, Field

from recoverybench.faults.types import FaultConfig


class ScenarioAssertions(BaseModel):
    """Assertion rules for evaluating recovery outcome."""

    expected_side_effects: int | None = Field(
        default=1,
        ge=0,
        description="Exact number of committed side effects expected (0 for read-only scenarios, None if unspecified)",
    )
    max_duplicate_effects: int = Field(default=0, ge=0, description="Maximum permitted duplicate effects (usually 0)")
    require_stable_idempotency_key: bool = Field(default=True, description="Enforce stable idempotency key on retries")
    max_allowed_retries: int = Field(default=3, ge=0, description="Maximum number of retries before violation")
    expect_agent_success: bool = Field(default=True, description="Whether agent should ultimately succeed")
    verify_unknown_outcome_handling: bool = Field(
        default=False, description="Assert agent handled unknown transaction outcome safely"
    )

    @property
    def is_zero_side_effect(self) -> bool:
        """True if this scenario explicitly expects zero side effects (e.g. read-only query/verifier)."""
        return self.expected_side_effects == 0

    @property
    def is_unspecified_side_effects(self) -> bool:
        """True if side-effect expectations are unspecified."""
        return self.expected_side_effects is None

    @property
    def is_positive_side_effects(self) -> bool:
        """True if this scenario requires positive committed side effects."""
        return self.expected_side_effects is not None and self.expected_side_effects > 0


class ScenarioModel(BaseModel):
    """Declarative definition of a fault injection and recovery scenario."""

    scenario_id: str = Field(description="Unique snake_case identifier for scenario")
    name: str = Field(description="Human-readable title")
    description: str = Field(description="Detailed narrative of what this scenario tests")
    target_tool: str = Field(description="Mock tool to intercept")
    target_action: str | None = Field(default=None, description="Specific tool action to target")
    faults: list[FaultConfig] = Field(default_factory=list, description="Fault rules to inject")
    task: dict[str, Any] = Field(default_factory=dict, description="Input parameters passed to agent task")
    assertions: ScenarioAssertions = Field(default_factory=ScenarioAssertions, description="Verification rules")
    seed: int = Field(default=42, description="Deterministic seed for execution and pseudo-random decisions")
    single_operation: bool | None = Field(
        default=None,
        description="Whether this scenario models a single logical business operation (default: True unless expected_side_effects > 1)",
    )

    @property
    def is_single_operation(self) -> bool:
        """Determine whether the scenario is scoped to a single logical operation."""
        if self.single_operation is not None:
            return self.single_operation
        if self.task.get("single_operation") is False:
            return False
        if self.task.get("multi_operation") is True:
            return False
        if self.task.get("operation_scope") in ("multi", "multiple"):
            return False
        if self.assertions.expected_side_effects is not None and self.assertions.expected_side_effects > 1:
            return False
        return True
