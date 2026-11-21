"""Tests for Multi-Tool Scenarios and Saga Compensation (Phase 4.5)."""

from typing import Any

import pytest

from recoverybench.engine.saga import (
    SagaOrchestrator,
    SagaWorkflowAgent,
)
from recoverybench.errors import (
    ConfigurationError,
    ToolError,
    ToolTimeoutError,
)
from recoverybench.faults.types import FaultConfig, FaultType
from recoverybench.scenarios.schema import (
    ScenarioAssertions,
    ScenarioModel,
    StepExecutionStatus,
    WorkflowStatus,
    WorkflowStep,
)
from recoverybench.sdk.tester import test_agent as rb_test_agent
from recoverybench.tools.base import ToolRegistry, ToolResult, ToolService
from recoverybench.tools.declarative import DeclarativeToolService


class InventoryTool(ToolService):
    """Mock inventory reservation service."""

    name: str = "inventory_service"
    is_mutating: bool = True

    def __init__(self) -> None:
        super().__init__()
        self.reservations: dict[str, int] = {}
        self.released: list[str] = []

    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        if action == "reserve":
            item_id = params.get("item_id", "item_1")
            qty = params.get("qty", 1)
            self.reservations[item_id] = self.reservations.get(item_id, 0) + qty
            return ToolResult(
                success=True,
                data={"status": "reserved", "item_id": item_id, "qty": qty},
                committed=True,
                effect_id=f"eff_res_{item_id}",
            )
        elif action == "release":
            item_id = params.get("item_id", "item_1")
            self.released.append(item_id)
            return ToolResult(
                success=True,
                data={"status": "released", "item_id": item_id},
                committed=True,
                effect_id=f"eff_rel_{item_id}",
            )
        raise ValueError(f"Unknown action: {action}")

    def reset(self) -> None:
        self.reservations.clear()
        self.released.clear()


class ShippingTool(ToolService):
    """Mock shipping order creation service."""

    name: str = "shipping_service"
    is_mutating: bool = True

    def __init__(self) -> None:
        super().__init__()
        self.shipments: list[str] = []

    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        if action == "create_shipment":
            ship_id = f"ship_{params.get('order_id', '101')}"
            self.shipments.append(ship_id)
            return ToolResult(
                success=True,
                data={"status": "created", "shipment_id": ship_id},
                committed=True,
                effect_id=f"eff_{ship_id}",
            )
        raise ValueError(f"Unknown action: {action}")

    def reset(self) -> None:
        self.shipments.clear()


def build_three_step_scenario(fail_step: int | None = None) -> ScenarioModel:
    """Helper creating a 3-step scenario (inventory -> payment -> shipping)."""
    steps = [
        WorkflowStep(
            step_id="step1_inventory",
            tool_name="inventory_service",
            action="reserve",
            params={"item_id": "widget_a", "qty": 2},
            is_compensatable=True,
            compensation_action="release",
            compensation_params={"item_id": "widget_a"},
        ),
        WorkflowStep(
            step_id="step2_payment",
            tool_name="payment_gateway",
            action="charge",
            params={"amount": 100.0, "customer_id": "cust_workflow"},
            depends_on=["step1_inventory"],
            is_compensatable=True,
            compensation_action="refund",
            compensation_params={"amount": 100.0},
        ),
        WorkflowStep(
            step_id="step3_shipping",
            tool_name="shipping_service",
            action="create_shipment",
            params={"order_id": "ord_99"},
            depends_on=["step2_payment"],
            is_compensatable=False,
        ),
    ]

    faults = []
    if fail_step == 1:
        faults.append(
            FaultConfig(
                type=FaultType.HTTP_500_TRANSIENT,
                target_tool="inventory_service",
                target_action="reserve",
                failure_count=1,
            )
        )
    elif fail_step == 2:
        faults.append(
            FaultConfig(
                type=FaultType.TIMEOUT_BEFORE_COMMIT,
                target_tool="payment_gateway",
                target_action="charge",
                failure_count=1,
            )
        )
    elif fail_step == 3:
        faults.append(
            FaultConfig(
                type=FaultType.HTTP_500_TRANSIENT,
                target_tool="shipping_service",
                target_action="create_shipment",
                failure_count=1,
            )
        )

    return ScenarioModel(
        schema_version=1,
        scenario_id="multi_tool_workflow_test",
        name="Multi-Tool Saga Workflow Test",
        description="Sequential 3-step saga with compensation",
        target_tool="multi_tool",
        task={"workflow_id": "wf_test_1"},
        steps=steps,
        faults=faults,
        assertions=ScenarioAssertions(),
    )


class TestMultiToolSaga:
    """Comprehensive test suite for Phase 4.5 Multi-Tool and Saga Compensation."""

    def test_all_steps_succeed(self) -> None:
        """Verify forward execution completes all steps when no faults are triggered."""
        scenario = build_three_step_scenario(fail_step=None)
        orchestrator = SagaOrchestrator()
        invocations: list[tuple[str, str]] = []

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            invocations.append((tool, action))
            return {"status": "ok", "effect_id": f"eff_{tool}_{action}"}

        result = orchestrator.execute_workflow(scenario, mock_caller, workflow_id="wf_ok")

        assert result.status == WorkflowStatus.SUCCEEDED
        assert len(result.step_records) == 3
        assert result.step_records["step1_inventory"].status == StepExecutionStatus.COMPLETED
        assert result.step_records["step2_payment"].status == StepExecutionStatus.COMPLETED
        assert result.step_records["step3_shipping"].status == StepExecutionStatus.COMPLETED
        assert len(result.forward_effects) == 3
        assert len(result.compensation_effects) == 0
        assert result.net_uncompensated_effects == 3
        assert result.residual_uncertainty is False

    def test_first_step_fails_no_compensation_needed(self) -> None:
        """Verify that when step 1 fails, downstream steps are skipped and 0 compensation is run."""
        scenario = build_three_step_scenario(fail_step=1)
        orchestrator = SagaOrchestrator()
        called: list[str] = []

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            called.append(f"{tool}.{action}")
            if tool == "inventory_service":
                raise RuntimeError("Inventory connection refused")
            return {"status": "ok"}

        result = orchestrator.execute_workflow(scenario, mock_caller, workflow_id="wf_fail1")

        assert result.status == WorkflowStatus.FAILED_UNCOMPENSATED
        assert result.step_records["step1_inventory"].status == StepExecutionStatus.FAILED
        assert result.step_records["step2_payment"].status == StepExecutionStatus.SKIPPED
        assert result.step_records["step3_shipping"].status == StepExecutionStatus.SKIPPED
        assert len(result.forward_effects) == 0
        assert len(result.compensation_effects) == 0

    def test_middle_step_fails_triggers_compensation_for_step1(self) -> None:
        """Verify step 2 failure triggers reverse compensation of step 1."""
        scenario = build_three_step_scenario(fail_step=2)
        orchestrator = SagaOrchestrator()
        executed_calls: list[tuple[str, str]] = []

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            executed_calls.append((tool, action))
            if tool == "payment_gateway" and action == "charge":
                raise ToolError("Credit card declined")
            return {"status": "ok", "effect_id": f"eff_{tool}_{action}"}

        result = orchestrator.execute_workflow(scenario, mock_caller, workflow_id="wf_fail2")

        assert result.status == WorkflowStatus.FAILED_COMPENSATED
        # Step 1 was executed then compensated
        rec1 = result.step_records["step1_inventory"]
        assert rec1.status == StepExecutionStatus.COMPENSATED
        assert rec1.compensation_triggered is True
        assert rec1.compensation_effect_id is not None
        assert rec1.compensation_attempt_id is not None
        assert rec1.compensation_operation_id is not None
        # Step 2 failed
        assert result.step_records["step2_payment"].status == StepExecutionStatus.FAILED
        # Step 3 was skipped
        assert result.step_records["step3_shipping"].status == StepExecutionStatus.SKIPPED

        # Verify compensation was dispatched
        assert ("inventory_service", "release") in executed_calls
        # Forward effect is preserved in history
        assert len(result.forward_effects) == 1
        assert len(result.compensation_effects) == 1
        assert result.net_uncompensated_effects == 0

    def test_final_step_fails_compensates_all_prior_steps(self) -> None:
        """Verify step 3 failure triggers reverse compensation for step 2 then step 1."""
        scenario = build_three_step_scenario(fail_step=3)
        orchestrator = SagaOrchestrator()
        call_sequence: list[str] = []

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            call_sequence.append(f"{tool}.{action}")
            if tool == "shipping_service":
                raise ToolError("Warehouse out of capacity")
            return {"status": "ok", "effect_id": f"eff_{tool}_{action}"}

        result = orchestrator.execute_workflow(scenario, mock_caller, workflow_id="wf_fail3")

        assert result.status == WorkflowStatus.FAILED_COMPENSATED
        # Verify execution order: inventory.reserve -> payment.charge -> shipping.create -> payment.refund -> inventory.release
        assert call_sequence == [
            "inventory_service.reserve",
            "payment_gateway.charge",
            "shipping_service.create_shipment",
            "payment_gateway.refund",
            "inventory_service.release",
        ]
        assert len(result.forward_effects) == 2
        assert len(result.compensation_effects) == 2
        assert result.net_uncompensated_effects == 0

    def test_compensation_action_fails(self) -> None:
        """Verify that when a compensation action fails, the failure is recorded without hiding forward effects."""
        scenario = build_three_step_scenario(fail_step=3)
        orchestrator = SagaOrchestrator()

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if tool == "shipping_service":
                raise ToolError("Shipping failure")
            if action == "refund":
                raise RuntimeError("Payment gateway refund API unavailable")
            return {"status": "ok", "effect_id": f"eff_{tool}_{action}"}

        result = orchestrator.execute_workflow(scenario, mock_caller, workflow_id="wf_comp_fail")

        assert result.status == WorkflowStatus.FAILED_COMPENSATION_FAILED
        rec_payment = result.step_records["step2_payment"]
        assert rec_payment.compensation_status == StepExecutionStatus.COMPENSATION_FAILED
        assert "Payment gateway refund API unavailable" in (rec_payment.compensation_error or "")
        # Forward effects remain in audit history!
        assert len(result.forward_effects) == 2
        assert result.net_uncompensated_effects > 0

    def test_compensation_process_terminated_retains_uncertainty(self) -> None:
        """Verify that if compensation process is killed mid-flight, status remains UNCERTAIN."""
        scenario = build_three_step_scenario(fail_step=3)
        orchestrator = SagaOrchestrator()

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if tool == "shipping_service":
                raise ToolError("Shipping failure")
            if action == "refund":
                from recoverybench.errors import ProcessTerminatedError
                raise ProcessTerminatedError("Refund worker process exceeded timeout and was killed", child_pid=8888)
            return {"status": "ok", "effect_id": f"eff_{tool}_{action}"}

        result = orchestrator.execute_workflow(scenario, mock_caller, workflow_id="wf_term_comp")

        assert result.status == WorkflowStatus.FAILED_COMPENSATION_FAILED
        assert result.residual_uncertainty is True
        rec_payment = result.step_records["step2_payment"]
        assert rec_payment.compensation_status == StepExecutionStatus.COMPENSATION_UNCERTAIN

    def test_saga_workflow_agent_via_sdk(self) -> None:
        """Verify SagaWorkflowAgent integrates seamlessly with SDK test_agent()."""
        scenario = build_three_step_scenario(fail_step=None)
        scenario.assertions.expected_side_effects = 3
        agent = SagaWorkflowAgent(scenario)

        from recoverybench.tools.payment import MockPaymentService

        inventory_svc = DeclarativeToolService(
            name="inventory_service",
            default_action="reserve",
            default_handler=lambda item_id, qty=1, workflow_id=None: {
                "status": "reserved",
                "item_id": item_id,
                "qty": qty,
            },
            default_mutates=True,
            max_side_effect_capacity=1,
            enforce_capacity=True,
        )
        inventory_svc.register_action(
            "release",
            lambda item_id, workflow_id=None: {"status": "released", "item_id": item_id},
            mutates=False,
        )

        shipping_svc = DeclarativeToolService(
            name="shipping_service",
            default_action="create_shipment",
            default_handler=lambda order_id, workflow_id=None: {
                "status": "created",
                "shipment_id": f"ship_{order_id}",
            },
            default_mutates=True,
            max_side_effect_capacity=1,
            enforce_capacity=True,
        )

        registry = ToolRegistry()
        registry.register(MockPaymentService())
        registry.register(inventory_svc)
        registry.register(shipping_svc)

        run_res = rb_test_agent(
            scenario=scenario,
            agent=agent,
            custom_tools=registry,
        )
        assert run_res.run_id is not None
        assert run_res.passed is True
        # Verify ledger has records for each step
        assert len(run_res.events) >= 3
        step_tools = [e.tool_name for e in run_res.events]
        assert "inventory_service" in step_tools
        assert "payment_gateway" in step_tools
        assert "shipping_service" in step_tools

    def test_dependencies_and_topological_execution_order(self) -> None:
        """Verify steps specified out of order execute in valid dependency order (Case 5)."""
        orchestrator = SagaOrchestrator()
        # Define steps in reverse dependency order: shipping -> payment -> inventory
        steps = [
            WorkflowStep(
                step_id="step3_shipping",
                tool_name="shipping_service",
                action="create_shipment",
                params={"order_id": "ord_100"},
                depends_on=["step2_payment"],
            ),
            WorkflowStep(
                step_id="step2_payment",
                tool_name="payment_gateway",
                action="charge",
                params={"amount": 50.0},
                depends_on=["step1_inventory"],
            ),
            WorkflowStep(
                step_id="step1_inventory",
                tool_name="inventory_service",
                action="reserve",
                params={"item_id": "widget_b"},
            ),
        ]
        scenario = ScenarioModel(
            schema_version=1,
            scenario_id="topo_order_test",
            name="Topological Order Test",
            description="Tests topological ordering of workflow steps",
            target_tool="multi_tool",
            task={},
            steps=steps,
            assertions=ScenarioAssertions(),
        )

        call_order: list[str] = []

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            call_order.append(f"{tool}.{action}")
            return {"status": "ok", "effect_id": f"eff_{tool}"}

        result = orchestrator.execute_workflow(scenario, mock_caller)
        assert result.status == WorkflowStatus.SUCCEEDED
        assert call_order == [
            "inventory_service.reserve",
            "payment_gateway.charge",
            "shipping_service.create_shipment",
        ]

    def test_invalid_or_cyclic_workflow_dependencies_rejected(self) -> None:
        """Verify cyclic or missing dependencies raise ConfigurationError (Case 6)."""
        orchestrator = SagaOrchestrator()

        # Case A: Cyclic dependency A -> B -> A
        cycle_steps = [
            WorkflowStep(step_id="A", tool_name="tool_a", action="act", params={}, depends_on=["B"]),
            WorkflowStep(step_id="B", tool_name="tool_b", action="act", params={}, depends_on=["A"]),
        ]
        scenario_cycle = ScenarioModel(
            schema_version=1,
            scenario_id="cycle_test",
            name="Cycle Test",
            description="Tests cyclic dependency detection",
            target_tool="multi_tool",
            task={},
            steps=cycle_steps,
            assertions=ScenarioAssertions(),
        )
        with pytest.raises(ConfigurationError) as exc_info_cycle:
            orchestrator.execute_workflow(scenario_cycle, lambda *args, **kwargs: {})
        assert "Cyclic or unsatisfiable dependency detected" in str(exc_info_cycle.value)

        # Case B: Unknown dependency
        missing_dep_steps = [
            WorkflowStep(step_id="step1", tool_name="tool_a", action="act", params={}, depends_on=["non_existent"]),
        ]
        scenario_missing = ScenarioModel(
            schema_version=1,
            scenario_id="missing_test",
            name="Missing Dep Test",
            description="Tests unknown dependency detection",
            target_tool="multi_tool",
            task={},
            steps=missing_dep_steps,
            assertions=ScenarioAssertions(),
        )
        with pytest.raises(ConfigurationError) as exc_info_missing:
            orchestrator.execute_workflow(scenario_missing, lambda *args, **kwargs: {})
        assert "unknown dependency" in str(exc_info_missing.value)

    def test_compensation_timeout_retains_uncertainty(self) -> None:
        """Verify compensation timeout marks status COMPENSATION_UNCERTAIN (Case 11 & 14)."""
        scenario = build_three_step_scenario(fail_step=3)
        orchestrator = SagaOrchestrator()

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if tool == "shipping_service":
                raise ToolError("Shipping warehouse fault")
            if action == "refund":
                raise ToolTimeoutError("Payment gateway refund request timed out after commit", committed=True)
            return {"status": "ok", "effect_id": f"eff_{tool}_{action}"}

        result = orchestrator.execute_workflow(scenario, mock_caller)
        assert result.status == WorkflowStatus.FAILED_COMPENSATION_FAILED
        assert result.residual_uncertainty is True
        rec_payment = result.step_records["step2_payment"]
        assert rec_payment.compensation_status == StepExecutionStatus.COMPENSATION_UNCERTAIN
        assert "ToolTimeoutError" in (rec_payment.compensation_error or "")

    def test_compensation_partially_succeeds(self) -> None:
        """Verify partial compensation: step 2 succeeds, step 1 fails (Case 12)."""
        scenario = build_three_step_scenario(fail_step=3)
        orchestrator = SagaOrchestrator()

        def mock_caller(tool: str, action: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if tool == "shipping_service":
                raise ToolError("Shipping unavailable")
            if action == "release":
                raise RuntimeError("Inventory DB deadlocked during rollback")
            return {"status": "ok", "effect_id": f"eff_{tool}_{action}"}

        result = orchestrator.execute_workflow(scenario, mock_caller)
        assert result.status == WorkflowStatus.FAILED_COMPENSATION_FAILED
        rec_payment = result.step_records["step2_payment"]
        assert rec_payment.compensation_status == StepExecutionStatus.COMPENSATED

        rec_inventory = result.step_records["step1_inventory"]
        assert rec_inventory.compensation_status == StepExecutionStatus.COMPENSATION_FAILED
        assert "deadlocked" in (rec_inventory.compensation_error or "")

        # Exact accounting verification: 2 forward effects, 1 compensation effect, 1 net uncompensated
        assert len(result.forward_effects) == 2
        assert len(result.compensation_effects) == 1
        assert result.net_uncompensated_effects == 1

    def test_unbounded_uncertainty_fails_closed_in_evaluator(self) -> None:
        """Verify unverified C1 tools or residual uncertainty fail closed (Case 21 & 22)."""
        scenario = build_three_step_scenario(fail_step=None)
        scenario.assertions.expected_side_effects = 3
        agent = SagaWorkflowAgent(scenario)

        # Register unverified Tier C1 tools (ordinary ToolService mocks without C3 attestation)
        from recoverybench.tools.payment import MockPaymentService
        registry = ToolRegistry()
        registry.register(MockPaymentService(enforce_capacity=False))
        registry.register(InventoryTool())
        registry.register(ShippingTool())

        run_res = rb_test_agent(
            scenario=scenario,
            agent=agent,
            custom_tools=registry,
        )
        # Conservative accounting must fail closed to INSUFFICIENT_EVIDENCE
        assert run_res.passed is False
        from recoverybench.models import EvaluationVerdict
        assert run_res.evaluation.verdict in (
            EvaluationVerdict.INSUFFICIENT_EVIDENCE,
            EvaluationVerdict.GENUINE_FAILURE,
        )
