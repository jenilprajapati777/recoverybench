"""Saga-style multi-tool workflow execution, failure propagation, and compensation coordinator.

Implements Phase 4.5 Multi-Tool Scenarios and Saga Compensation:
- Executes multi-step workflows with explicit step identities and dependencies.
- Handles partial completion, mid-flight timeouts, and failure propagation.
- Triggers reverse compensation actions with distinct identities and evidence.
- Preserves immutable forward effects in audit ledger while recording compensation effects.
- Computes conservative side-effect bounds without assuming automatic rollback.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from pydantic import BaseModel, Field

from recoverybench.agent.base import (
    AgentAdapter,
    AgentResult,
    CompletionStatus,
    TaskContext,
    ToolCaller,
)
from recoverybench.errors import (
    ConfigurationError,
    ProcessTerminatedError,
    ToolTimeoutError,
)
from recoverybench.scenarios.schema import (
    ScenarioModel,
    StepExecutionStatus,
    WorkflowStatus,
    WorkflowStep,
)

logger = logging.getLogger("recoverybench.saga")


class StepExecutionRecord(BaseModel):
    """Detailed record of an individual workflow step execution and its compensation."""

    step_id: str
    tool_name: str
    action: str
    status: StepExecutionStatus = StepExecutionStatus.PENDING
    forward_attempt_id: str | None = None
    forward_operation_id: str | None = None
    forward_effect_id: str | None = None
    result_data: Any | None = None
    error: str | None = None

    # Compensation properties (distinct identity, never historical deletion)
    compensation_triggered: bool = False
    compensation_attempt_id: str | None = None
    compensation_operation_id: str | None = None
    compensation_effect_id: str | None = None
    compensation_status: StepExecutionStatus | None = None
    compensation_error: str | None = None


class SagaWorkflowResult(BaseModel):
    """Overall outcome of a multi-tool saga workflow execution."""

    workflow_id: str
    status: WorkflowStatus
    step_records: dict[str, StepExecutionRecord] = Field(default_factory=dict)
    forward_effects: list[str] = Field(default_factory=list)
    compensation_effects: list[str] = Field(default_factory=list)
    unresolved_compensation_attempts: list[str] = Field(default_factory=list)
    net_uncompensated_effects: int = 0
    residual_uncertainty: bool = False
    error: str | None = None


class SagaOrchestrator:
    """Orchestrates forward multi-tool step execution and reverse saga compensation."""

    def __init__(self) -> None:
        pass

    @classmethod
    def topological_sort(cls, steps: list[WorkflowStep]) -> list[WorkflowStep]:
        """Validate dependencies and return steps in topological execution order.

        Raises:
            ConfigurationError: If unknown dependency or cyclic dependency is detected.
        """
        step_map = {s.step_id: s for s in steps}

        # 1. Validate all referenced dependencies exist
        for s in steps:
            for dep in s.depends_on:
                if dep not in step_map:
                    raise ConfigurationError(
                        f"Workflow step '{s.step_id}' references unknown dependency '{dep}'."
                    )

        # 2. Build in-degree and adjacency graph
        in_degree: dict[str, int] = {s.step_id: len(s.depends_on) for s in steps}
        graph: dict[str, list[str]] = {s.step_id: [] for s in steps}
        for s in steps:
            for dep in s.depends_on:
                graph[dep].append(s.step_id)

        # 3. Kahn's algorithm
        queue = [s for s in steps if in_degree[s.step_id] == 0]
        sorted_steps: list[WorkflowStep] = []

        while queue:
            curr = queue.pop(0)
            sorted_steps.append(curr)
            for neighbor_id in graph[curr.step_id]:
                in_degree[neighbor_id] -= 1
                if in_degree[neighbor_id] == 0:
                    queue.append(step_map[neighbor_id])

        if len(sorted_steps) != len(steps):
            unresolved = [s.step_id for s in steps if in_degree[s.step_id] > 0]
            raise ConfigurationError(
                f"Cyclic or unsatisfiable dependency detected in workflow steps: {unresolved}"
            )

        return sorted_steps

    def execute_workflow(
        self,
        scenario: ScenarioModel,
        tool_call: ToolCaller,
        workflow_id: str | None = None,
        context_params: dict[str, Any] | None = None,
    ) -> SagaWorkflowResult:
        """Execute all steps in scenario.steps with failure propagation and compensation."""
        w_id = workflow_id or f"wf_{uuid.uuid4().hex[:8]}"
        step_records: dict[str, StepExecutionRecord] = {}
        completed_compensatable_steps: list[WorkflowStep] = []
        workflow_error: str | None = None
        has_failure: bool = False

        # Validate and topologically sort workflow steps
        ordered_steps = self.topological_sort(scenario.steps)

        # Build execution map
        for step in scenario.steps:
            step_records[step.step_id] = StepExecutionRecord(
                step_id=step.step_id,
                tool_name=step.tool_name,
                action=step.action,
            )

        # ----------------------------------------------------------------------
        # 1. Forward Execution Phase (in Topological Order)
        # ----------------------------------------------------------------------
        for step in ordered_steps:
            rec = step_records[step.step_id]

            # Check upstream step dependencies
            dependencies_satisfied = True
            for dep in step.depends_on:
                dep_rec = step_records.get(dep)
                if not dep_rec or dep_rec.status != StepExecutionStatus.COMPLETED:
                    dependencies_satisfied = False
                    break

            if not dependencies_satisfied:
                rec.status = StepExecutionStatus.SKIPPED
                continue

            # Assign globally unique attempt and operation identities
            forward_att_id = f"att_{w_id}_{step.step_id}"
            forward_op_id = f"op_{w_id}_{step.step_id}"
            forward_key = f"key_{w_id}_{step.step_id}"
            rec.forward_attempt_id = forward_att_id
            rec.forward_operation_id = forward_op_id

            # Prepare parameters
            merged_params = dict(step.params)
            if context_params:
                merged_params.update(context_params)

            rec.status = StepExecutionStatus.RUNNING

            try:
                # Dispatch tool call through RecoveryBench pipeline
                raw_result = tool_call(
                    step.tool_name,
                    step.action,
                    merged_params,
                    idempotency_key=forward_key,
                    operation_id=forward_op_id,
                )

                rec.status = StepExecutionStatus.COMPLETED
                rec.result_data = raw_result

                # Extract effect_id if returned
                if isinstance(raw_result, dict):
                    rec.forward_effect_id = raw_result.get("effect_id") or raw_result.get("charge_id")
                elif hasattr(raw_result, "effect_id"):
                    rec.forward_effect_id = raw_result.effect_id

                if not rec.forward_effect_id:
                    rec.forward_effect_id = f"eff_{step.step_id}_{uuid.uuid4().hex[:6]}"

                if step.is_compensatable:
                    completed_compensatable_steps.append(step)

            except Exception as exc:
                has_failure = True
                rec.status = StepExecutionStatus.FAILED
                rec.error = f"{type(exc).__name__}: {exc}"
                workflow_error = rec.error

                # If step is not optional, mark subsequent steps as SKIPPED and trigger compensation
                if not step.optional:
                    for remaining_step in scenario.steps:
                        rem_rec = step_records[remaining_step.step_id]
                        if rem_rec.status == StepExecutionStatus.PENDING:
                            rem_rec.status = StepExecutionStatus.SKIPPED
                    break

        # ----------------------------------------------------------------------
        # 2. Saga Compensation Phase (Triggered on Failure)
        # ----------------------------------------------------------------------
        compensation_failed = False
        compensation_uncertain = False

        if has_failure and completed_compensatable_steps:
            # Compensate in reverse topological order
            for step_to_compensate in reversed(completed_compensatable_steps):
                rec = step_records[step_to_compensate.step_id]
                rec.compensation_triggered = True

                comp_tool = step_to_compensate.compensation_tool or step_to_compensate.tool_name
                comp_action = step_to_compensate.compensation_action

                # If compensatable flag was set but no compensation action was declared
                if not comp_action:
                    rec.compensation_status = StepExecutionStatus.COMPENSATION_FAILED
                    rec.compensation_error = "Step declared compensatable but no compensation_action specified"
                    compensation_failed = True
                    continue

                # Compensation has its OWN distinct attempt, operation, and idempotency key
                comp_att_id = f"comp_att_{w_id}_{step_to_compensate.step_id}"
                comp_op_id = f"comp_op_{w_id}_{step_to_compensate.step_id}"
                comp_key = f"comp_key_{w_id}_{step_to_compensate.step_id}"

                rec.compensation_attempt_id = comp_att_id
                rec.compensation_operation_id = comp_op_id

                # Prepare compensation payload referencing original effect
                comp_params = dict(step_to_compensate.compensation_params)
                if rec.forward_effect_id:
                    comp_params["original_effect_id"] = rec.forward_effect_id
                    comp_params["target_effect_id"] = rec.forward_effect_id

                rec.compensation_status = StepExecutionStatus.RUNNING

                try:
                    # Dispatch compensation through RecoveryBench pipeline
                    comp_result = tool_call(
                        comp_tool,
                        comp_action,
                        comp_params,
                        idempotency_key=comp_key,
                        operation_id=comp_op_id,
                    )

                    rec.compensation_status = StepExecutionStatus.COMPENSATED
                    rec.status = StepExecutionStatus.COMPENSATED

                    if isinstance(comp_result, dict):
                        rec.compensation_effect_id = comp_result.get("effect_id")
                    elif hasattr(comp_result, "effect_id"):
                        rec.compensation_effect_id = comp_result.effect_id

                    if not rec.compensation_effect_id:
                        rec.compensation_effect_id = f"comp_eff_{step_to_compensate.step_id}_{uuid.uuid4().hex[:6]}"

                except ProcessTerminatedError as term_exc:
                    # Compensation child process was hard terminated: status remains UNCERTAIN
                    rec.compensation_status = StepExecutionStatus.COMPENSATION_UNCERTAIN
                    rec.status = StepExecutionStatus.COMPENSATION_UNCERTAIN
                    rec.compensation_error = f"ProcessTerminatedError: {term_exc}"
                    compensation_uncertain = True

                except (ToolTimeoutError, TimeoutError) as timeout_exc:
                    # Compensation call timed out: server execution state is UNCERTAIN
                    rec.compensation_status = StepExecutionStatus.COMPENSATION_UNCERTAIN
                    rec.status = StepExecutionStatus.COMPENSATION_UNCERTAIN
                    rec.compensation_error = f"{type(timeout_exc).__name__}: {timeout_exc}"
                    compensation_uncertain = True

                except Exception as comp_exc:
                    rec.compensation_status = StepExecutionStatus.COMPENSATION_FAILED
                    rec.status = StepExecutionStatus.COMPENSATION_FAILED
                    rec.compensation_error = f"{type(comp_exc).__name__}: {comp_exc}"
                    compensation_failed = True

        # ----------------------------------------------------------------------
        # 3. Outcome & Effect Aggregation
        # ----------------------------------------------------------------------
        forward_effects: list[str] = [
            r.forward_effect_id
            for r in step_records.values()
            if r.forward_effect_id is not None
        ]

        compensation_effects: list[str] = [
            r.compensation_effect_id
            for r in step_records.values()
            if r.compensation_effect_id is not None
        ]

        unresolved_comp: list[str] = [
            r.compensation_attempt_id
            for r in step_records.values()
            if r.compensation_status == StepExecutionStatus.COMPENSATION_UNCERTAIN
            and r.compensation_attempt_id is not None
        ]

        # Net uncompensated forward effects
        net_uncompensated = len(forward_effects) - len(compensation_effects)
        if net_uncompensated < 0:
            net_uncompensated = 0

        # Determine overall workflow status
        final_status: WorkflowStatus
        if not has_failure:
            final_status = WorkflowStatus.SUCCEEDED
        elif compensation_uncertain or compensation_failed:
            final_status = WorkflowStatus.FAILED_COMPENSATION_FAILED
        elif completed_compensatable_steps:
            final_status = WorkflowStatus.FAILED_COMPENSATED
        else:
            final_status = WorkflowStatus.FAILED_UNCOMPENSATED

        return SagaWorkflowResult(
            workflow_id=w_id,
            status=final_status,
            step_records=step_records,
            forward_effects=forward_effects,
            compensation_effects=compensation_effects,
            unresolved_compensation_attempts=unresolved_comp,
            net_uncompensated_effects=net_uncompensated,
            residual_uncertainty=compensation_uncertain,
            error=workflow_error,
        )


class SagaWorkflowAgent(AgentAdapter):
    """Adapter executing a multi-tool scenario workflow against RecoveryBench."""

    def __init__(
        self,
        scenario: ScenarioModel,
        *,
        agent_id: str = "saga_workflow_agent",
        orchestrator: SagaOrchestrator | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.scenario = scenario
        self.orchestrator = orchestrator or SagaOrchestrator()

    def run(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        """Synchronously execute multi-tool saga workflow."""
        result = self.orchestrator.execute_workflow(
            scenario=self.scenario,
            tool_call=tool_call,
            workflow_id=f"wf_{task.scenario_id}",
            context_params=task.task_params,
        )

        is_success = result.status == WorkflowStatus.SUCCEEDED
        status = CompletionStatus.SUCCEEDED if is_success else CompletionStatus.FAILED

        return AgentResult(
            success=is_success,
            status=status,
            data=result.model_dump(),
            error=result.error,
        )

    def execute(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        return self.run(task, tool_call)

    async def run_async(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        return self.run(task, tool_call)

    async def execute_async(self, task: TaskContext, tool_call: ToolCaller) -> AgentResult:
        return await self.run_async(task, tool_call)
