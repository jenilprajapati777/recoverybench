# RecoveryBench — Public Demonstration Walkthrough: The Two Generals' Hazard

**Duration:** ~60–90 seconds to explain  
**Scenario:** `payment_timeout_after_commit.yaml`  
**Fault Injected:** `timeout_after_commit` (Network drop after server-side mutation)  
**Target Agents:**
- Unsafe Agent: `NaivePaymentAgent` ([src/recoverybench/agent/naive.py](../src/recoverybench/agent/naive.py))
- Safe Agent: `IdempotentPaymentAgent` ([src/recoverybench/agent/idempotent.py](../src/recoverybench/agent/idempotent.py))

---

## 1. The Core Problem: Why Agents Cause Duplicate Charges

When an autonomous AI agent executes a tool call against a real-world API (e.g. charging a credit card or provisioning cloud infrastructure), distributed systems failures can cause the network connection to drop **after** the external service commits the transaction.

```
Agent                        Network                     Payment Gateway
  │                             │                               │
  ├─── 1. charge($100) ────────►│──────────────────────────────►│
  │    (Key: 'key_123')         │                               │
  │                             │                               ├── Commits $100
  │                             │                               │   effect_id: eff_001
  │                             │◄── 2. HTTP 200 (ch_001) ──────┤
  │                             │    [DROPPED / TIMEOUT]        │
  │                             X                               │
  │ (Sees TimeoutException)     │                               │
  │                             │                               │
  │ [UNSAFE RETRY: Naive]       │                               │
  ├─── 3. charge($100) ────────►│──────────────────────────────►│
  │    (Key: 'key_456' / None)  │                               ├── Commits $100
  │                             │                               │   effect_id: eff_002
  │                             │◄── 4. HTTP 200 (ch_002) ──────┤
  │◄── 5. Returns Success ──────┤                               │
  │                             │                               │
  ▼                             ▼                               ▼
CUSTOMER DOUBLE-CHARGED! ($200 deducted instead of $100)
```

The agent believes it successfully recovered from a network error. In reality, it doubled the customer's charge.

---

## 2. Running the Demo Command

Run the comparison out of the box with zero configuration:

```bash
recoverybench demo
```

*(This command runs locally in virtual time with zero network dependencies).*

---

## 3. Real Output Breakdown: Part 1 — The Naive Agent Fails

The first half of the demo runs `NaivePaymentAgent`:

```text
1. Unsafe Agent (Naive Payment Agent)
┌────────────────────── RecoveryBench Execution Result ───────────────────────┐
│ Scenario: Payment Timeout After Commit (Two Generals' Hazard)               │
│ (payment_timeout_after_commit)                                              │
│ Agent: naive_payment_agent | Run ID: demo_naive_agent_run | Execution:      │
│ TRUSTED (NOT ISOLATED)                                                      │
│ Verdict: GENUINE_FAILURE | Duration: 12.3ms                                 │
│ Committed Effects: 2 (Expected: 1, Duplicates: 1)                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### The Immutable Effect Ledger
The append-only ledger records exactly what happened at each attempt:

```text
                   Effect Ledger (Append-Only Event Stream)                    
┌───────┬─────────┬─────────┬────────┬─────────┬─────────┬─────────┬──────────┐
│       │         │         │        │         │         │         │ Injected │
│       │         │         │        │  Exec   │ Commit  │ Idempo… │ Fault /  │
│ Evt # │ Attempt │ Tool    │ Action │ Status  │ Status  │ Hash    │ Error    │
├───────┼─────────┼─────────┼────────┼─────────┼─────────┼─────────┼──────────┤
│  2168 │    1    │ paymen… │ charge │ FAULT_… │ COMMIT… │ e88909… │ ToolTim… │
│  2169 │    2    │ paymen… │ charge │ SUCCESS │ COMMIT… │ fe85be… │ -        │
└───────┴─────────┴─────────┴────────┴─────────┴─────────┴─────────┴──────────┘
```

- **Event 2168 (Attempt 1):** The gateway committed the charge (`COMMITTED`), but RecoveryBench injected `ToolTimeoutError` before the agent got the response. The agent sent key hash `e88909...`.
- **Event 2169 (Attempt 2):** The naive agent caught the timeout and retried with a *different* key hash (`fe85be...`). The gateway committed a second charge (`COMMITTED`).

### Deterministic Invariant Failure
```text
                       Deterministic Recovery Assertions                       
┌────────────────┬────────────────┬─────────────────┬────────┬────────────────┐
│ Rule Name      │    Expected    │     Actual      │ Result │ Details        │
├────────────────┼────────────────┼─────────────────┼────────┼────────────────┤
│ side_effect_c… │       1        │        2        │  FAIL  │ Duplicate      │
│                │                │                 │        │ side-effect(s) │
│                │                │                 │        │ detected:      │
│                │                │                 │        │ expected at    │
│                │                │                 │        │ most 0         │
│                │                │                 │        │ duplicates,    │
│                │                │                 │        │ observed 1.    │
│ idempotency_k… │ identical_key… │    unstable     │  FAIL  │ Idempotency    │
│                │                │                 │        │ key changed    │
│                │                │                 │        │ across         │
│                │                │                 │        │ retries.       │
└────────────────┴────────────────┴─────────────────┴────────┴────────────────┘
```

**Verdict:** `GENUINE_FAILURE`. The agent reported success, but the customer was double-charged.

---

## 4. Real Output Breakdown: Part 2 — The Idempotent Agent Passes

The second half of the demo runs `IdempotentPaymentAgent`:

```text
2. Safe Agent (Idempotent Payment Agent)
┌────────────────────── RecoveryBench Execution Result ───────────────────────┐
│ Scenario: Payment Timeout After Commit (Two Generals' Hazard)               │
│ (payment_timeout_after_commit)                                              │
│ Agent: idempotent_payment_agent | Run ID: demo_safe_agent_run | Execution:  │
│ TRUSTED (NOT ISOLATED)                                                      │
│ Verdict: PASS | Duration: 10.8ms                                            │
│ Committed Effects: 1 (Expected: 1, Duplicates: 0)                           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### The Ledger Shows Safe Recovery
```text
                   Effect Ledger (Append-Only Event Stream)                    
┌───────┬─────────┬─────────┬────────┬─────────┬─────────┬─────────┬──────────┐
│       │         │         │        │         │         │         │ Injected │
│       │         │         │        │  Exec   │ Commit  │ Idempo… │ Fault /  │
│ Evt # │ Attempt │ Tool    │ Action │ Status  │ Status  │ Hash    │ Error    │
├───────┼─────────┼─────────┼────────┼─────────┼─────────┼─────────┼──────────┤
│  2170 │    1    │ paymen… │ charge │ FAULT_… │ COMMIT… │ eeef6a… │ ToolTim… │
│  2171 │    2    │ paymen… │ charge │ SUCCESS │ NOT_AP… │ eeef6a… │ -        │
└───────┴─────────┴─────────┴────────┴─────────┴─────────┴─────────┴──────────┘
```

- **Event 2170 (Attempt 1):** The gateway committed the charge, and the response was dropped. Key hash: `eeef6a...`.
- **Event 2171 (Attempt 2):** The idempotent agent retried with the **identical** key hash (`eeef6a...`). The gateway recognized the existing transaction, returned the original receipt, and **did not commit a second mutation** (`NOT_APPLICABLE`).

### All Invariants Pass
```text
                       Deterministic Recovery Assertions                       
┌────────────────┬────────────────┬─────────────────┬────────┬────────────────┐
│ Rule Name      │    Expected    │     Actual      │ Result │ Details        │
├────────────────┼────────────────┼─────────────────┼────────┼────────────────┤
│ side_effect_c… │       1        │        1        │  PASS  │ Exactly 1      │
│                │                │                 │        │ side-effect(s) │
│                │                │                 │        │ committed as   │
│                │                │                 │        │ expected (0    │
│                │                │                 │        │ duplicates).   │
│ idempotency_k… │ identical_key… │     stable      │  PASS  │ Idempotency    │
│                │                │                 │        │ key remained   │
│                │                │                 │        │ stable across  │
│                │                │                 │        │ all retries.   │
│ unknown_outco… │ safe_idempote… │ safely_reconci… │  PASS  │ Safe unknown   │
│                │                │                 │        │ outcome        │
│                │                │                 │        │ handling:      │
│                │                │                 │        │ Agent safely   │
│                │                │                 │        │ reconciled     │
│                │                │                 │        │ unknown        │
│                │                │                 │        │ transaction    │
│                │                │                 │        │ outcome using  │
│                │                │                 │        │ idempotency.   │
└────────────────┴────────────────┴─────────────────┴────────┴────────────────┘
```

**Verdict:** `PASS`. Customer charged once; recovery transparent.

---

## 5. Comparative Executive Summary

```text
                 Comparative Summary & Ground-Truth Invariants                 
╔═════════════════════════╤═════════════════════════╤═════════════════════════╗
║ Metric / Invariant      │  Naive Agent (Unsafe)   │ Idempotent Agent (Safe) ║
╟─────────────────────────┼─────────────────────────┼─────────────────────────╢
║ Final Verdict           │     GENUINE_FAILURE     │          PASS           ║
║ Expected Side Effects   │            1            │            1            ║
║ Actual Committed        │            2            │            1            ║
║ Effects                 │                         │                         ║
║ Duplicate Side Effects  │   1 (DOUBLE CHARGE!)    │        0 (None)         ║
║ Idempotency Key Stable? │  No (New key / None on  │     Yes (Preserved      ║
║                         │         retry)          │     identical key)      ║
║ Real-World Impact       │ Customer charged twice  │ Customer charged once;  ║
║                         │     on network drop     │  recovery transparent   ║
╚═════════════════════════╧═════════════════════════╧═════════════════════════╝
```

---

## 6. Generated Reports

At completion, RecoveryBench generates audit-grade reports:
- Machine-readable JSON: `.recoverybench/reports/demo_naive_report.json`, `.recoverybench/reports/demo_safe_report.json`
- Self-contained dark-mode HTML: `.recoverybench/reports/demo_safe_report.html`
- CI/CD JUnit XML: `.recoverybench/reports/demo_safe_report.xml`

Open the HTML report to explore interactive evidence drawers for each invariant rule.
