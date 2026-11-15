# RecoveryBench — Competitive Positioning & Product Landscape

**Category:** AI Agent Recovery Testing  
**Tagline:** *Test the failure before production does.*  
**Core Description:** RecoveryBench injects deterministic failures into agent tool workflows and checks whether the agent recovers without creating duplicate, missing or incorrectly reported side effects.  

---

## 1. Product Positioning

RecoveryBench is an integration-oriented recovery-testing tool designed for developers to test their own autonomous AI agents under deterministic distributed systems faults. It bridges the gap between pre-deployment unit/integration testing and runtime side-effect verification.

### Target Audience
- **Python AI Agent Developers:** Authoring agent loops and resilient tool execution logic.
- **LangGraph Developers:** Building directed acyclic or cyclic agent workflows with `StateGraph` and `ToolNode`.
- **Backend & Platform Engineers:** Responsible for financial, provisioning, and transactional side-effects triggered by autonomous agents.
- **QA & Reliability Engineers:** Establishing automated CI/CD safety gates to verify invariant compliance prior to production release.

### Initial Use Case
Payment and transaction workflows where a transient failure or timeout after commit (`timeout_after_commit`, the Two Generals' Hazard) can trigger blind retries, resulting in duplicate billing or orphaned mutations.

### What RecoveryBench Is NOT
- **Not a general-purpose LLM observability platform:** It does not replace tools like LangSmith or Langfuse for production tracing, token tracking, or cost telemetry.
- **Not an LLM benchmark for reasoning quality:** It does not benchmark MMLU, GSM8k, or general knowledge accuracy.
- **Not a general infrastructure chaos engineering platform:** It is not an OS-level or cluster-wide chaos orchestrator like Gremlin or Toxiproxy.

---

## 2. Product Landscape & Adjacent Solutions

Autonomous agents interact with real-world APIs, but existing tooling addresses different stages of the application lifecycle. RecoveryBench focuses specifically on **active, pre-deployment recovery verification against ground-truth side effects**.

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                               AI AGENT TOOLING SPECTRUM                                │
│                                                                                        │
│   [ Production Observability ]       [ Quality & Content Evals ]     [ RecoveryBench ] │
│   - LangSmith                        - DeepEval                      - Active Faults   │
│   - Langfuse                         - Ragas                         - Effect Ledger   │
│   - Arize Phoenix                    - Promptfoo                     - Invariant Rules │
│   Passive production traces          Model output scoring            Recovery testing  │
│   "What happened in prod?"           "Was the answer accurate?"      "Did it charge 2x?"│
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Adjacent Tool Categories

1. **LLM Observability & Tracing (LangSmith, Langfuse, Arize Phoenix)**
   - *Role:* Instrument production workflows, record distributed traces, capture input/output spans, and monitor latency/tokens.
   - *Distinction:* Observability records what an agent executed in production or during test runs. However, observability tools do not inject deterministic application faults (such as a TCP drop *after* an upstream commit) into mock tool dependencies, nor do they assert ground-truth database mutations or idempotency key stability. RecoveryBench complements observability by certifying agent recovery logic *before* deployment.

2. **Infrastructure Chaos & Network Fault Simulation (Gremlin, Toxiproxy)**
   - *Role:* Inject latency, jitter, packet loss, or server shutdowns into networks and container clusters.
   - *Distinction:* Network chaos tools operate at the transport layer (L4/L7) across live infrastructure. While Toxiproxy can drop TCP sockets, it does not understand agent tool boundaries, logical transaction identity (`operation_id` vs `attempt_id`), or application-level commit semantics. RecoveryBench operates at the application/agent integration boundary, pairing stateful mock tool state with deterministic fault injection.

3. **Academic Retry Benchmarking (RetryBench)**
   - *Role:* Evaluates LLM prompting strategies and retry resilience across academic benchmark suites.
   - *Distinction:* RetryBench focuses on academic benchmarking of LLM behaviors under prompt retries. RecoveryBench is an engineering tool designed for developers to test their own production code, integrating with Python callables and LangGraph workflows, backed by an append-only effect ledger and hardened Linux container isolation.

---

## 3. Comparison Matrix

| Dimension | LLM Observability (LangSmith, Langfuse, Phoenix) | Infrastructure Chaos (Gremlin, Toxiproxy) | Academic Benchmarks (RetryBench) | **RecoveryBench** |
| :--- | :--- | :--- | :--- | :--- |
| **Primary Focus** | Passive telemetry, trace visualization, token analytics | Infrastructure resilience, packet drops, latency injection | Evaluating LLM prompting strategies under retries | **Deterministic agent recovery & business side-effect verification** |
| **Testing Stage** | Continuous monitoring / staging analysis | Staging & production chaos experiments | Research evaluation & prompt comparison | **Pre-deployment unit, integration, and CI/CD testing** |
| **Fault Injection** | Not designed for active fault injection | Injects network/server faults at the OS/socket layer | Evaluates prompt-level retries | **Application-level tool faults (e.g. `timeout_after_commit`, 429, 500)** |
| **Side-Effect Verification** | Observes API payloads sent by agent | Observes system availability & uptime | Measures prompt benchmark score | **Ground-truth effect ledger (verifies actual mutations & duplicates)** |
| **Transaction Identity** | Trace ID / Span ID tracking | Transport-level connection tracking | Prompt / run session identifier | **Decoupled `operation_id`, `attempt_id`, and `effect_id`** |
| **Execution Sandboxing** | Standard host execution | Runs against deployed target environments | Sandbox dependent on benchmark harness | **Hardened Linux container isolation (`--network=none`, cap-drop)** |
| **Local / Offline Workflow** | Often relies on hosted SaaS or cloud backends | Requires local proxy or agent daemons | Local or remote LLM API calls | **Local-first (in-memory or SQLite, zero API keys required)** |

---

## 4. Current Integrations vs. Future Roadmap

To ensure clarity and credibility, RecoveryBench explicitly distinguishes currently verified framework adapters from roadmap items:

### Currently Supported & Verified (v0.1.0)
- **Generic Python Callables:** Any Python function or method adapted via `@agent` or `wrap_agent()`.
- **LangGraph Adapter:** Native integration with LangGraph `StateGraph` and LangChain `@tool` definitions, intercepting `ToolNode` exceptions and evaluating state transitions.

### Future Roadmap
- **Model Context Protocol (MCP) Adapter:** Integration testing for MCP tool servers and client agents under adverse fault conditions.
- **CrewAI Framework Adapter:** Dedicated hooks and task execution wrapper for CrewAI multi-agent crews.
- **AutoGen Framework Adapter:** ConversableAgent tool-execution interceptor and recovery evaluator.

---

## 5. Comparison Sources & Documentation References

The comparative statements in this document were compiled from public documentation, specifications, and repositories accessed on **November 15–16, 2026**:

1. **LangSmith Documentation:** [https://docs.smith.langchain.com/](https://docs.smith.langchain.com/) (Accessed: November 15, 2026) — Verified focus on distributed tracing, prompt playgrounds, and dataset evaluation.
2. **Langfuse Documentation:** [https://langfuse.com/docs](https://langfuse.com/docs) (Accessed: November 15, 2026) — Verified open-source LLM engineering platform capabilities (tracing, prompt management, evaluation).
3. **Arize Phoenix Documentation:** [https://docs.arize.com/phoenix/](https://docs.arize.com/phoenix/) (Accessed: November 15, 2026) — Verified observability, evaluation, and tracing workflows.
4. **Gremlin Documentation:** [https://www.gremlin.com/docs/](https://www.gremlin.com/docs/) (Accessed: November 15, 2026) — Verified infrastructure and application chaos injection models.
5. **Shopify Toxiproxy Repository:** [https://github.com/Shopify/toxiproxy](https://github.com/Shopify/toxiproxy) (Accessed: November 15, 2026) — Verified TCP proxy fault injection mechanics.
6. **RetryBench Research & Benchmarking Literature:** Academic benchmarks analyzing LLM retry behavior and failure modes (Accessed: November 15, 2026).
