# RecoveryBench - Competitive Positioning & Market Differentiation

**Category:** Local-First AI Agent Recovery & Side-Effect Testing Platform  
**Positioning Statement:** *RecoveryBench is the first testing tool specifically built to verify how autonomous AI agents recover from external tool faults without causing duplicate or orphaned side-effects.*

---

## 1. The Market Gap

The AI tooling landscape is densely populated in observability, prompting benchmarks, and runtime traces. However, there is a glaring blindspot: **testing the failure recovery behavior of side-effectful agents before they deploy to production.**

```
+----------------------------------------------------------------------------------+
|                            THE AGENT TOOLING SPECTRUM                            |
|                                                                                  |
|  [ LLM Observability ]       [ LLM Benchmark Evals ]      [ RecoveryBench ]      |
|  - LangSmith                 - DeepEval                   - Deterministic Faults |
|  - Langfuse                  - Ragas                      - Ground-Truth Ledger  |
|  - Arize Phoenix             - Promptfoo                  - Side-Effect Invariants|
|  Passive monitoring          Prompt / Content scoring     Active recovery testing|
|  "What happened in prod?"    "Is the summary accurate?"   "Did it double-charge?" |
+----------------------------------------------------------------------------------+
```

---

## 2. Detailed Differentiation Matrix

| Capability | Generic LLM Tracers (LangSmith, Langfuse) | Prompt / RAG Evals (DeepEval, Ragas) | Chaos Tools (Gremlin, Toxiproxy) | **RecoveryBench** |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Focus** | Passive telemetry & token metrics | Semantic quality & hallucinations | Network packet drops on infrastructure | **Deterministic agent tool failure & side effects** |
| **Testing Paradigm** | Post-hoc analysis in production | Prompt-response matching | Uncontrolled infrastructure stress | **Deterministic pre-deployment unit/integration tests** |
| **Side-Effect Verification** | None (only logs what agent sent) | None (evaluates text output) | None (only verifies service uptime) | **Ground-Truth Effect Ledger (commits, duplicates)** |
| **Two Generals' Problem Testing** | Impossible | Impossible | Unreliable / probabilistic | **Native (`timeout_after_commit` deterministic injector)** |
| **Virtual Time Execution** | No (real wall-clock delays) | No | No | **Yes (zero-sleep deterministic virtual clock)** |
| **Offline / Local Execution** | Usually requires cloud service | Local or API-dependent | Infrastructure required | **100% Local-first (SQLite, zero API keys required)** |
| **Idempotency Assertions** | No | No | No | **Native key-stability & duplicate commit detection** |

---

## 3. Why Existing Solutions Fall Short

### 3.1 LLM Observability Tools Are Not Recovery Testers
Observability tools record what happened. If an agent times out on a payment call, retries, and double-charges a customer, LangSmith will accurately show two tool calls. **Observability does not prevent the bug, test the retry handler, or assert idempotency in CI.**

### 3.2 LLM-as-a-Judge Cannot Verify Ground-Truth State
Asking an LLM *"Did the agent successfully recover from the timeout?"* produces hallucinations. An LLM cannot query the internal balance of a bank mock or inspect database commit records. RecoveryBench relies strictly on deterministic assertions over an immutable ledger.

### 3.3 Traditional Mocks Lack State-Fault Coupling
Standard mocks (`unittest.mock`, `responses`, `WireMock`) return static status codes. They do not simulate the nuanced state transitions of real-world distributed failures—such as committing an item to a database *first*, and *then* dropping the TCP connection before sending the 200 OK.

---

## 4. Value Proposition Summary

1. **Financial & Data Safety:** Catch double-charges, orphaned reservations, and duplicate ticket creation in pull requests before customer impact.
2. **Speed & Determinism:** Run 50 fault scenarios in under 5 seconds with zero flaky `time.sleep` calls.
3. **Developer Trust:** Gives teams the empirical confidence to unleash autonomous agents on real external APIs.
