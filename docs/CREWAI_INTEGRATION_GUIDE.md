# RecoveryBench — CrewAI Integration Guide

**Version:** 0.2.0
**Status:** Official Guide
**Applies to:** RecoveryBench Phase 4.4+

---

## 1. Overview

CrewAI is an autonomous multi-agent framework orchestrating collaborative agents (`Agent`), structured tasks (`Task`), and execution pipelines (`Crew`).

The RecoveryBench CrewAI Adapter (`CrewAIAdapter` in `recoverybench.adapters.crewai`) allows teams building CrewAI agents to:
- Intercept and fault-inject tool calls made by CrewAI agents.
- Enforce idempotency key stability across agent retries.
- Detect double-execution, lost updates, and duplicate financial charges.
- Record all tool invocations in RecoveryBench's append-only causal ledger.
- Verify recovery behavior inside isolated network-disabled sandbox environments (`--network=none`).

---

## 2. Installation & Optional Dependencies

CrewAI is an optional integration dependency in RecoveryBench:

```powershell
pip install "recoverybench[crewai]"
```
Or with pip directly:
```powershell
pip install crewai langchain-core
```

---

## 3. How It Works

RecoveryBench bridges its deterministic mock services (`ToolService`) into CrewAI-compatible `BaseTool` instances via `create_crewai_tool()` or `create_crewai_payment_tool()`:

```
┌──────────────────────────────────────────────────────────────┐
│                         CrewAI Crew                          │
│   ┌────────────────────┐          ┌──────────────────────┐   │
│   │   FinancialAgent   │          │  ProcessPaymentTask  │   │
│   └─────────┬──────────┘          └──────────┬───────────┘   │
│             │ uses                           │               │
│   ┌─────────▼────────────────────────────────▼───────────┐   │
│   │ Bridged CrewAI Tool (payment_gateway_charge)         │   │
│   └─────────────────────────┬────────────────────────────┘   │
└─────────────────────────────┼────────────────────────────────┘
                              │ tool_call()
┌─────────────────────────────▼────────────────────────────────┐
│           RecoveryBench Execution Engine & Ledger            │
│  - Fault Injection (TimeoutAfterCommit, Transient 500)       │
│  - Causal Attempt & Effect Tracking                          │
│  - Conservative Side-Effect Accounting                       │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. Usage Example

### Defining a CrewAI Agent and Testing with RecoveryBench

```python
from crewai import Agent, Crew, Process, Task
import recoverybench as rb
from recoverybench.adapters.crewai import CrewAIAdapter, create_crewai_payment_tool

def build_order_fulfillment_crew(tools: list) -> Crew:
    """Factory creating a Crew equipped with RecoveryBench bridged tools."""
    billing_agent = Agent(
        role="Billing Specialist",
        goal="Safely process customer payments with stable idempotency keys",
        backstory="Reliability-focused agent handling critical financial operations.",
        tools=tools,
        verbose=False,
    )

    billing_task = Task(
        description="Charge ${amount} for order {order_id} using idempotency key order_{order_id}.",
        expected_output="Confirmation of transaction completion.",
        agent=billing_agent,
    )

    return Crew(
        agents=[billing_agent],
        tasks=[billing_task],
        process=Process.sequential,
    )

# Wrap with RecoveryBench Adapter
adapter = CrewAIAdapter(build_order_fulfillment_crew)

# Run recovery evaluation under timeout_after_commit fault
result = rb.test_agent("payment_timeout_after_commit", adapter)
result.assert_passed()
```

---

## 5. Result Extraction & Status Normalization

The adapter automatically parses the returned `CrewOutput`:
- Raw text and structured `json_dict` / `pydantic` outputs are captured into `AgentResult.data`.
- If an agent task encounters an unhandled exception or raises a fatal tool error, the adapter catches the exception and produces `AgentResult(success=False, status=CompletionStatus.FAILED, error=...)`.
- The platform distinguishes between agent-level task failure and harness-level infrastructure errors.
