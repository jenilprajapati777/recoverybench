# RecoveryBench — Multi-Tool Scenarios and Saga Compensation Guide (Phase 4.5)

## 1. Overview

Real-world AI agent workloads frequently span multiple heterogeneous services (e.g., inventory reservation, payment processing, shipping dispatch, notification broadcasting). When an intermediate tool step fails, times out, or gets killed due to resource exhaustion, the agent must not leave the distributed system in an inconsistent state.

Phase 4.5 introduces native multi-tool workflow schema definitions and saga compensation semantics to RecoveryBench, integrating directly with:
- **Two-Layer Conservative Side-Effect Accounting (ADR-004)**: Tracking every attempt $a \in \mathcal{A}$ and verified committed effect $e \in \mathcal{E}_{verified}$, including forward mutations and reverse compensating actions.
- **Process Isolation**: Safely running uncooperative sync tools in dedicated child OS processes, and handling hard terminations during compensation without corrupting ledger integrity.
- **SDK & Test Harness**: Seamless evaluation of multi-step agents via `test_agent()` and `SagaWorkflowAgent`.

---

## 2. Multi-Tool Scenario Schema

RecoveryBench scenarios can define multi-step sequential or DAG-structured workflows using the `steps` field on `ScenarioModel`.

### 2.1 WorkflowStep Specification

```python
class WorkflowStep(BaseModel):
    step_id: str                              # Unique identifier within the workflow
    tool_name: str                            # Target tool service (e.g., "inventory_service")
    action: str                               # Forward action (e.g., "reserve")
    params: dict[str, Any]                    # Forward invocation payload
    depends_on: list[str] = Field(default_factory=list)  # Step IDs required before this step
    is_compensatable: bool = False            # Whether forward mutations require rollback on failure
    compensation_tool: str | None = None      # Tool executing rollback (defaults to tool_name)
    compensation_action: str | None = None    # Rollback action (e.g., "release", "refund")
    compensation_params: dict[str, Any] = Field(default_factory=dict)  # Rollback payload
    optional: bool = False                    # If True, step failure does not abort the workflow
```

### 2.2 Scenario Assertions Extension

Multi-tool scenarios support rich assertion fields in `ScenarioAssertions`:

- `expect_compensation: bool | None`: When `True`, asserts that at least one compensation action was triggered and recorded.
- `max_uncompensated_effects: int | None`: Caps the allowable forward mutations that remained uncompensated.
- `require_all_steps_succeeded: bool | None`: When `True`, requires all non-optional steps to succeed without aborting or triggering compensation.

---

## 3. Saga Execution and Compensation Semantics

### 3.1 Forward Execution Lifecycle

1. **Topological Order**: Steps are executed in order respecting their `depends_on` prerequisites.
2. **Distinct Operation Boundaries**: Each step establishes an isolated `operation_id` (`f"op_{workflow_id}_{step_id}"`), preventing cross-step idempotency key pollution.
3. **Failure Detection**: If any non-optional step encounters an error (`ToolError`, `ProcessTerminatedError`, `TimeoutError`, or backend failure):
   - The forward execution aborts immediately.
   - Any downstream steps in `PENDING` status are marked `SKIPPED`.
   - The compensation coordinator triggers reverse saga rollback for all completed compensatable steps.

### 3.2 Reverse Compensation Order (LIFO)

Rollback actions are executed in strict reverse order of completion (LIFO: Last-In, First-Out):

$$\text{Rollback Sequence} = \text{reverse}(\{s \in \text{Completed Steps} \mid s.\text{is\_compensatable} = \text{True}\})$$

Each compensation action:
- Invokes `compensation_tool` with `compensation_action` and `compensation_params`.
- Passes an explicit `idempotency_key` (`f"comp_key_{workflow_id}_{step_id}"`) and distinct `operation_id` (`f"comp_op_{workflow_id}_{step_id}"`).
- Records the resulting compensation effect or error.

### 3.3 Status State Machine

```
              ┌─────────┐
              │ PENDING │
              └────┬────┘
                   │
         ┌─────────┴─────────┐
         ▼                   ▼
    ┌───────────┐       ┌─────────┐
    │ COMPLETED │       │ FAILED  │
    └─────┬─────┘       └────┬────┘
          │ (on failure      │
          │  downstream)     ▼
          │             ┌─────────┐
          │             │ SKIPPED │
          ▼             └─────────┘
┌──────────────────┐
│   COMPENSATING   │
└─────────┬────────┘
     ┌────┴────────────────────────┐
     ▼                             ▼
┌─────────────┐       ┌─────────────────────────┐
│ COMPENSATED │       │ COMPENSATION_UNCERTAIN  │
└─────────────┘       └─────────────────────────┘
```

---

## 4. Conservative Side-Effect Accounting with Sagas

Under ADR-004, saga compensation actions are treated as distinct operations with their own effect identities:

1. **Forward Mutations**: Incur side effects with unique effect identities ($e_f \in \mathcal{E}_{verified}$).
2. **Compensating Actions**:
   - If mutating (e.g., releasing inventory or crediting balance), compensation creates a separate compensation effect $e_c$.
   - The engine computes net uncompensated effects:
     $$\text{net\_uncompensated} = |\mathcal{E}_{\text{forward}}| - |\mathcal{E}_{\text{compensated}}|$$
3. **Hard Process Termination / Uncertainty**:
   - If a compensation tool process hangs and is terminated via `SIGKILL` / `TerminateProcess` (`ProcessTerminatedError`), its status becomes `COMPENSATION_UNCERTAIN`.
   - The orchestrator sets `residual_uncertainty = True`, ensuring the evaluation verdict conservatively reflects unbounded or unresolved capacity rather than falsely reporting clean recovery.

---

## 5. Usage Example

```python
from recoverybench.engine.saga import SagaOrchestrator, SagaWorkflowAgent
from recoverybench.scenarios.schema import ScenarioModel, WorkflowStep, ScenarioAssertions
from recoverybench.sdk.tester import test_agent

# 1. Define multi-step scenario
scenario = ScenarioModel(
    schema_version=1,
    scenario_id="order_fulfillment_saga",
    name="Order Fulfillment Saga",
    description="Reserve inventory -> Charge payment -> Dispatch shipping",
    target_tool="multi_tool",
    task={"workflow_id": "wf_order_1001"},
    steps=[
        WorkflowStep(
            step_id="step1_inventory",
            tool_name="inventory_service",
            action="reserve",
            params={"item_id": "widget_42", "qty": 1},
            is_compensatable=True,
            compensation_action="release",
            compensation_params={"item_id": "widget_42", "qty": 1},
        ),
        WorkflowStep(
            step_id="step2_payment",
            tool_name="payment_gateway",
            action="charge",
            params={"amount": 49.99, "customer_id": "cust_abc"},
            depends_on=["step1_inventory"],
            is_compensatable=True,
            compensation_action="refund",
            compensation_params={"amount": 49.99},
        ),
        WorkflowStep(
            step_id="step3_shipping",
            tool_name="shipping_service",
            action="create_shipment",
            params={"order_id": "ord_1001"},
            depends_on=["step2_payment"],
            is_compensatable=False,
        ),
    ],
    assertions=ScenarioAssertions(
        expected_side_effects=3,
        max_duplicate_effects=0,
    ),
)

# 2. Run with SagaWorkflowAgent via SDK
agent = SagaWorkflowAgent(scenario)
result = test_agent(scenario=scenario, agent=agent)

print(f"Passed: {result.passed}, Verdict: {result.evaluation.verdict}")
```
