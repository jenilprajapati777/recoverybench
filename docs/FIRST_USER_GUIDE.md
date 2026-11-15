# RecoveryBench — First-User Getting Started Guide

Welcome to **RecoveryBench**. This guide takes you from an empty directory to running your first recovery test in under five minutes.

RecoveryBench tests how your autonomous AI agents handle external tool failures (timeouts, dropped responses, rate limits) without causing duplicate, missing, or orphaned business side-effects.

---

## 1. Prerequisites

Before installing, ensure your environment meets these requirements:

- **Python:** Version 3.11, 3.12, or 3.13 (`python --version`)
- **Git:** Installed and available on your PATH
- **Docker Engine (Optional for quick demo, recommended for container isolation):** Docker Desktop (Windows/macOS) or Docker Engine (Linux).
  - *Note:* The basic demo runs out-of-the-box in trusted in-process mode without Docker. Docker is only required when running with container network isolation (`--isolation isolated`).
- **No Cloud Accounts or API Keys Required:** RecoveryBench runs completely locally using deterministic mock services and an append-only SQLite effect ledger.

---

## 2. Clone and Setup

### Clone the Repository

```bash
git clone https://github.com/jenilprajapati777/recoverybench.git
cd recoverybench
```

### Windows (PowerShell)

```powershell
# 1. Create a virtual environment
python -m venv .venv

# 2. Activate the virtual environment
.venv\Scripts\Activate.ps1

# 3. Upgrade packaging tools
python -m pip install --upgrade pip setuptools wheel

# 4. Install RecoveryBench in editable mode with development dependencies
pip install -e ".[dev]"
```

*Tip (Execution Policy):* If PowerShell restricts script activation, run:
```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
.venv\Scripts\Activate.ps1
```

### Linux & macOS (bash/zsh)

```bash
# 1. Create a virtual environment
python3 -m venv .venv

# 2. Activate the virtual environment
source .venv/bin/activate

# 3. Upgrade packaging tools
python -m pip install --upgrade pip setuptools wheel

# 4. Install RecoveryBench in editable mode with development dependencies
pip install -e ".[dev]"
```

*(Optional: If you plan to test LangGraph agents, install with: `pip install -e ".[langgraph,dev]"`).*

---

## 3. Verify Your Environment (`recoverybench doctor`)

Run the built-in diagnostic command to verify your Python runtime, SQLite engine, scenario registry, and Docker availability:

```bash
recoverybench doctor
```

### Example Output:

```text
                RecoveryBench Doctor - Environment Diagnostics                 
┌─────────────────────────┬────────┬──────────────────────────────────────────┐
│ Component               │ Status │ Details                                  │
├─────────────────────────┼────────┼──────────────────────────────────────────┤
│ Python Runtime          │   OK   │ Version 3.13.8 (3.11+ required)          │
│ SQLite Engine           │   OK   │ SQLite in-memory and foreign key         │
│                         │        │ validation active                        │
│ Filesystem Storage      │   OK   │ Write access verified at                 │
│                         │        │ D:\RecoveryBench\.recoverybench          │
│ Scenario Registry       │   OK   │ 6 scenario(s) loaded (6 built-in)        │
│ Docker Executable       │   OK   │ Executable found                         │
│ Docker Daemon           │   OK   │ Docker available (Daemon OS: linux)      │
│ Runtime Image           │   OK   │ Image cached locally                     │
│ Sandbox Resource Limits │   OK   │ Memory: 512m | CPU: 1.0 | PIDs: 64       │
│ Isolation Readiness     │ READY  │ Containerized network isolation active   │
└─────────────────────────┴────────┴──────────────────────────────────────────┘
System diagnostics complete.
```

If Docker is not currently running, `Isolation Readiness` will indicate that container isolation is unavailable, but you can still run all scenarios in trusted in-process mode (`--isolation trusted`).

---

## 4. Run the Naive vs. Safe Demo (`recoverybench demo`)

The fastest way to understand RecoveryBench is running the comparative demonstration:

```bash
recoverybench demo
```

### What This Scenario Tests: The Two Generals' Hazard

1. **Step 1:** The agent calls `payment_gateway.charge(amount=100, currency="USD")`.
2. **Step 2:** The mock payment gateway successfully records and commits the $100 charge.
3. **Step 3 (The Fault):** A transient network drop occurs after commit (`timeout_after_commit`). The HTTP 200 response never reaches the agent.
4. **Step 4:** The agent catches a timeout exception.
5. **The Fork in the Road:**
   - **Naive Agent:** Catches the error and retries with a *new* idempotency key (or omits the key). The payment service cannot recognize this as a retry, commits a second charge, and the customer is **double-charged**.
   - **Idempotent Agent:** Preserves a stable idempotency key (`tx_key_cust_12345`) across retries. The payment service identifies the retry, returns the existing receipt, and the customer is charged **exactly once**.

### Real CLI Output:

```text
┌───────────────────────────────────────────────────────────────────┐
│   RECOVERYBENCH COMPARATIVE DEMONSTRATION: NAIVE vs. SAFE AGENT   │
│ Scenario: Payment Timeout After Commit (Two Generals' Hazard)     │
└───────────────────────────────────────────────────────────────────┘

1. Unsafe Agent (Naive Payment Agent)
┌────────────────────── RecoveryBench Execution Result ───────────────────────┐
│ Scenario: Payment Timeout After Commit (Two Generals' Hazard)               │
│ (payment_timeout_after_commit)                                              │
│ Agent: naive_payment_agent | Run ID: demo_naive_agent_run | Execution:      │
│ TRUSTED (NOT ISOLATED)                                                      │
│ Verdict: GENUINE_FAILURE | Duration: 12.3ms                                 │
│ Committed Effects: 2 (Expected: 1, Duplicates: 1)                           │
└─────────────────────────────────────────────────────────────────────────────┘

... [Ledger and Assertions Displayed] ...

2. Safe Agent (Idempotent Payment Agent)
┌────────────────────── RecoveryBench Execution Result ───────────────────────┐
│ Scenario: Payment Timeout After Commit (Two Generals' Hazard)               │
│ (payment_timeout_after_commit)                                              │
│ Agent: idempotent_payment_agent | Run ID: demo_safe_agent_run | Execution:  │
│ TRUSTED (NOT ISOLATED)                                                      │
│ Verdict: PASS | Duration: 10.8ms                                            │
│ Committed Effects: 1 (Expected: 1, Duplicates: 0)                           │
└─────────────────────────────────────────────────────────────────────────────┘

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

## 5. Inspecting Execution Evidence (HTML & JSON Reports)

Every run automatically saves complete ledger traces, assertion results, and timing metrics in `.recoverybench/reports/`:

```text
.recoverybench/reports/
├── demo_naive_report.json
├── demo_safe_report.json
├── demo_safe_report.html
└── demo_safe_report.xml
```

### Viewing the HTML Report
Open the self-contained HTML report in your browser:

- **Windows:**
  ```powershell
  Start-Process .recoverybench\reports\demo_safe_report.html
  ```
- **macOS:**
  ```bash
  open .recoverybench/reports/demo_safe_report.html
  ```
- **Linux:**
  ```bash
  xdg-open .recoverybench/reports/demo_safe_report.html
  ```

The HTML report features dark-mode styling, verdict badges, and collapsible evidence drawers citing exact ledger event IDs, timestamps, and commit tokens.

---

## 6. Running with Docker Container Isolation (`--isolation isolated`)

To guarantee that an agent cannot make unmonitored outbound network connections or access host credentials, run the scenario inside a hardened Linux container:

```bash
recoverybench run scenarios/payment_timeout_after_commit.yaml --agent idempotent --isolation isolated
```

### What Happens Behind the Scenes:
1. **Workspace Staging:** Copies your workspace to a temporary directory, strictly omitting `.env*`, `.git`, `.venv`, `.ssh`, and certificates.
2. **Container Launch:** Starts a container pinned to `python:3.11-slim@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535`.
3. **Hardened Flags:**
   - `--network=none`: Drops all container network interfaces. Outbound socket calls fail immediately at the kernel level.
   - `--cap-drop=ALL`: Strips all Linux root capabilities.
   - `--read-only`: Mounts workspace and root filesystems read-only.
   - `--security-opt=no-new-privileges`: Blocks privilege escalation.
4. **Stdio IPC Protocol:** Host and container communicate strictly via framed JSON Lines on stdin/stdout. Tool calls are routed back to host mock services, and mutations are recorded in the host SQLite ledger.
5. **Deterministic Cleanup:** The container is forcibly stopped and removed (`docker rm -f`) in a guaranteed `finally` block.

*(Note: Linux containers share the host operating system kernel; see [SECURITY_MODEL.md](SECURITY_MODEL.md) for threat modeling details).*

---

## 7. Testing Your Own Agent

You can integrate your own agent in two ways:

### Option A: Generic Python Callable (`@agent`)

```python
import recoverybench as rb
from recoverybench.agent.base import AgentResult, TaskContext, ToolCaller


@rb.agent(id="my_custom_agent")
def my_agent(task: TaskContext, tool_call: ToolCaller) -> AgentResult:
    # 1. Generate or retrieve stable idempotency key
    customer_id = task.task_params.get("customer_id", "guest")
    stable_key = f"idemp_{customer_id}_{task.task_params.get('order_id')}"

    # 2. Execute with retry
    for attempt in range(1, 4):
        try:
            res = tool_call(
                "payment_gateway",
                "charge",
                task.task_params,
                idempotency_key=stable_key,
            )
            return AgentResult(success=True, data=res, attempts=attempt)
        except Exception:
            continue

    return AgentResult(success=False, error="Max retries exceeded")
```

Run in a Python test:
```python
from recoverybench.sdk import test_agent

result = test_agent(
    scenario="scenarios/payment_timeout_after_commit.yaml",
    agent=my_agent,
    isolation="trusted",
)
assert result.passed, f"Agent failed recovery invariants: {result.failure_reasons}"
```

### Option B: LangGraph Framework Adapter

If using LangGraph `StateGraph`, import `LangGraphAdapter` and wrap your compiled graph:

```python
from recoverybench.adapters.langgraph import LangGraphAdapter, create_langchain_payment_tool

# Bridged LangChain tool compatible with LangGraph ToolNode
payment_tool = create_langchain_payment_tool(tool_client)
```

See [SDK_INTEGRATION_GUIDE.md](SDK_INTEGRATION_GUIDE.md) for full LangGraph examples.

---

## 8. Troubleshooting Common Errors

### 1. `Docker is not running or unreachable (Exit Code 3)`
- **Cause:** You ran with `--isolation isolated`, but Docker Engine / Docker Desktop is not started.
- **Remedy:** Either start Docker Desktop, or pass `--isolation trusted` for development mode without container isolation:
  ```bash
  recoverybench run scenarios/payment_timeout_after_commit.yaml --agent idempotent --isolation trusted
  ```

### 2. `PowerShell Execution Policy Error on venv Activation`
- **Cause:** Windows PowerShell restricts running unsigned scripts by default.
- **Remedy:** Run in your current PowerShell session:
  ```powershell
  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope Process
  ```

### 3. `ModuleNotFoundError: No module named 'recoverybench'`
- **Cause:** Virtual environment is not activated, or the package was not installed in editable mode.
- **Remedy:** Ensure `.venv` is active, then run `pip install -e .`.

### 4. `Reserved Environment Variable Rejected`
- **Cause:** When running in isolated mode, custom agent environment variables cannot override container security variables (`PATH`, `LD_PRELOAD`, `PYTHONPATH`, etc.).
- **Remedy:** Rename your custom environment variables to application-specific identifiers (e.g. `MY_API_URL` instead of `PATH`).

---

## 9. Next Steps

- Explore available fault scenarios: `recoverybench list-scenarios`
- Read the [SDK Integration Guide](SDK_INTEGRATION_GUIDE.md) for advanced agent patterns and multi-operation workflows.
- Review the [Architecture Specification](ARCHITECTURE.md) and [Security Model](SECURITY_MODEL.md).
- Integrate RecoveryBench into your CI pipeline using the [GitHub Actions Guide](GITHUB_ACTIONS_GUIDE.md).
