"""Pydantic schema definitions for RecoveryBench scenarios."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from recoverybench.faults.types import FaultConfig

CURRENT_SCENARIO_SCHEMA_VERSION: int = 1
SUPPORTED_SCENARIO_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})


def validate_scenario_schema_version(version: Any) -> int:
    """Validate that schema_version is an exact integer and currently supported.

    Rejects:
    - Missing/None
    - Booleans (bool is a subclass of int in Python)
    - Non-integers (strings, floats, etc.)
    - Unsupported versions
    """
    if version is None:
        raise ValueError(
            f"Missing required field 'schema_version'. "
            f"Supported versions: {sorted(SUPPORTED_SCENARIO_SCHEMA_VERSIONS)}."
        )
    if type(version) is not int:
        raise ValueError(
            f"Invalid schema_version type '{type(version).__name__}'. "
            f"schema_version must be an integer, supported versions: {sorted(SUPPORTED_SCENARIO_SCHEMA_VERSIONS)}."
        )
    if version not in SUPPORTED_SCENARIO_SCHEMA_VERSIONS:
        raise ValueError(
            f"Unsupported scenario schema_version: {version}. "
            f"Supported versions: {sorted(SUPPORTED_SCENARIO_SCHEMA_VERSIONS)}."
        )
    return version


class StepExecutionStatus(StrEnum):
    """Lifecycle execution status of a single workflow step."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    COMPENSATED = "COMPENSATED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    COMPENSATION_UNCERTAIN = "COMPENSATION_UNCERTAIN"


class WorkflowStatus(StrEnum):
    """Overall execution and recovery status of a multi-tool workflow."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED_COMPENSATED = "FAILED_COMPENSATED"
    FAILED_COMPENSATION_FAILED = "FAILED_COMPENSATION_FAILED"
    FAILED_UNCOMPENSATED = "FAILED_UNCOMPENSATED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"


class WorkflowStep(BaseModel):
    """Specification of an individual step in a multi-tool workflow."""

    step_id: str = Field(description="Unique step identifier within the workflow")
    tool_name: str = Field(description="Target tool service to invoke")
    action: str = Field(description="Target action to execute on tool service")
    params: dict[str, Any] = Field(default_factory=dict, description="Static or template parameters for invocation")
    depends_on: list[str] = Field(default_factory=list, description="List of step_ids that must succeed before this step executes")
    is_compensatable: bool = Field(default=True, description="Whether this step can and should be compensated on downstream failure")
    compensation_tool: str | None = Field(default=None, description="Tool service to invoke for compensation (defaults to tool_name)")
    compensation_action: str | None = Field(default=None, description="Action to invoke for compensation (e.g. 'refund', 'cancel')")
    compensation_params: dict[str, Any] = Field(default_factory=dict, description="Parameters for compensation action")
    optional: bool = Field(default=False, description="Whether workflow continues despite failure of this step")


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
    expect_compensation: bool | None = Field(
        default=None, description="Assert whether saga compensation was executed on failure"
    )
    max_uncompensated_effects: int | None = Field(
        default=None, description="Maximum permitted uncompensated forward effects (e.g. 0 on successful compensation)"
    )
    require_all_steps_succeeded: bool | None = Field(
        default=None, description="Assert all workflow steps completed without error"
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

    schema_version: int = Field(
        description=f"Scenario schema specification version. Current version: {CURRENT_SCENARIO_SCHEMA_VERSION}."
    )
    scenario_id: str = Field(description="Unique snake_case identifier for scenario")
    name: str = Field(description="Human-readable title")
    description: str = Field(description="Detailed narrative of what this scenario tests")
    target_tool: str = Field(default="multi_tool", description="Mock tool to intercept")
    target_action: str | None = Field(default=None, description="Specific tool action to target")
    faults: list[FaultConfig] = Field(default_factory=list, description="Fault rules to inject")
    task: dict[str, Any] = Field(default_factory=dict, description="Input parameters passed to agent task")
    steps: list[WorkflowStep] = Field(
        default_factory=list, description="Ordered steps for multi-tool workflow execution and saga compensation"
    )
    assertions: ScenarioAssertions = Field(default_factory=ScenarioAssertions, description="Verification rules")
    seed: int = Field(default=42, description="Deterministic seed for execution and pseudo-random decisions")
    single_operation: bool | None = Field(
        default=None,
        description="Whether this scenario models a single logical business operation (default: True unless expected_side_effects > 1)",
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def _validate_schema_version(cls, v: Any) -> int:
        return validate_scenario_schema_version(v)

    @property
    def is_single_operation(self) -> bool:
        """Determine whether the scenario is scoped to a single logical operation."""
        if self.steps:
            return False
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
