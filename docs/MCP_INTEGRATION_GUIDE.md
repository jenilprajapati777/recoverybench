# RecoveryBench — Model Context Protocol (MCP) Integration Guide

**Version:** 0.2.0
**Status:** Official Guide
**Applies to:** RecoveryBench Phase 4.3+

---

## 1. Overview & Architecture

The Model Context Protocol (MCP) is an open standard that standardizes how AI applications and agents (such as Claude Desktop, Cursor, Goose, and custom autonomous agents) discover and invoke external tools and resources over JSON-RPC 2.0 transports (`stdio` and SSE).

RecoveryBench provides an **MCP Server Bridge** (`recoverybench.mcp`), positioning RecoveryBench as a specialized **Fault Injection & Transaction Verification Server**:

```
┌────────────────────────────────────────────────────────────────────────┐
│               External AI Agent (Claude, Cursor, AutoGen)              │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ JSON-RPC 2.0 over stdio
                                    │ initialize, tools/list, tools/call
┌───────────────────────────────────▼────────────────────────────────────┐
│                  RecoveryBench MCP Server Bridge                       │
│  ┌──────────────────────────────────────────────────────────────────┐  │
│  │ Protocol Router: tools/list, tools/call, resources/list/read     │  │
│  │ Fault Injection Engine: Timeouts, Drops, 500s, Latency Jitter    │  │
│  │ Ledger Recorder: Causal attempt IDs, effect IDs, fingerprints    │  │
│  │ Tool Process Runner: Hard OS process supervision for sync tools  │  │
│  └──────────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

### Key Capabilities
- **Standard Tool Discovery (`tools/list`):** Exposes registered RecoveryBench tools (`payment_gateway_charge`, custom declarative tools) with standard JSON Schema parameter specifications.
- **Deterministic Fault Injection (`tools/call`):** Any fault scenario configured in RecoveryBench (e.g. `timeout_after_commit`, `http_500_transient`, `duplicate_delivery`) is injected directly into tool execution when external agents invoke tools.
- **Append-Only Causal Ledger:** Every invocation attempt is stamped with a unique `attempt_id`, payload fingerprint, and status (`COMMITTED`, `UNCERTAIN`, `ABORTED`).
- **Real OS Process Isolation:** Uncooperative synchronous tools execute in supervised child processes (`multiprocessing.get_context("spawn")`) and are forcibly killed if deadlines expire.
- **Interactive Resources (`resources/list` & `resources/read`):** External agents and developers can inspect live ledger events (`recoverybench://ledger/events`), scenario configurations (`recoverybench://scenario/active`), and assertion evaluations (`recoverybench://evaluation/latest`).

---

## 2. Quickstart: Running the MCP Server

### 2.1 Via CLI
Start the server in stdio mode:
```powershell
recoverybench mcp-server
```

To bind a specific fault scenario:
```powershell
# Intercept charges with timeout_after_commit
recoverybench mcp-server --scenario payment_timeout_after_commit

# Run with full OS process isolation for uncooperative tools
recoverybench mcp-server --scenario payment_timeout_after_commit --isolation isolated
```

### 2.2 Client Configuration Examples

#### Claude Desktop (`claude_desktop_config.json`)
Add RecoveryBench to your Claude Desktop configuration:
```json
{
  "mcpServers": {
    "recoverybench": {
      "command": "poetry",
      "args": [
        "run",
        "recoverybench",
        "mcp-server",
        "--scenario",
        "payment_timeout_after_commit"
      ],
      "cwd": "D:\\RecoveryBench"
    }
  }
}
```

#### Custom Python MCP Client / Pipe
```python
import json
import subprocess

proc = subprocess.Popen(
    ["poetry", "run", "recoverybench", "mcp-server"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    text=True,
)

# 1. Initialize Handshake
init_req = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "my-client", "version": "1.0.0"}},
}
proc.stdin.write(json.dumps(init_req) + "\n")
proc.stdin.flush()
response = json.loads(proc.stdout.readline())

# 2. Discover Tools
tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
proc.stdin.write(json.dumps(tools_req) + "\n")
proc.stdin.flush()
tools_res = json.loads(proc.stdout.readline())
```

---

## 3. Python API Integration

You can instantiate and control the MCP server programmatically within test suites or agent harnesses:

```python
import json
from recoverybench.mcp import MCPServerConfig, RecoveryBenchMCPServer
from recoverybench.sandbox import IsolationMode

# Configure server with isolated execution and custom timeout
config = MCPServerConfig(
    server_name="my-recoverybench-mcp",
    isolation=IsolationMode.ISOLATED,
    default_tool_timeout=5.0,
)
server = RecoveryBenchMCPServer(config=config)

# Handle raw JSON-RPC messages
req = {
    "jsonrpc": "2.0",
    "id": 42,
    "method": "tools/call",
    "params": {
        "name": "payment_gateway_charge",
        "arguments": {
            "amount": 99.50,
            "customer_id": "cust_101",
            "idempotency_key": "order_key_101",
        },
    },
}
response_json = server.handle_message(json.dumps(req))
result = json.loads(response_json)
```

---

## 4. MCP Tools & Resources Reference

### Available Tools

| Tool Name | Parameters | Description |
|:---|:---|:---|
| `payment_gateway_charge` | `amount`, `customer_id`, `idempotency_key`, `operation_id` | Executes credit card charge through fault-injected payment gateway. |
| `payment_gateway` | `action`, `params`, `idempotency_key`, `operation_id` | Generic action dispatcher for mock payment gateway. |
| `recoverybench_evaluate` | *(none)* | Evaluates session ledger events against scenario assertions and returns verdict. |
| `recoverybench_reset_session` | `scenario_id` (optional) | Resets ledger and active session state for a fresh test run. |

### Available Resources

| Resource URI | MIME Type | Description |
|:---|:---:|:---|
| `recoverybench://ledger/events` | `application/json` | Real-time JSON list of all recorded attempts, fingerprints, and effect statuses. |
| `recoverybench://scenario/active` | `application/json` | Active scenario task parameters, fault rules, and assertions. |
| `recoverybench://evaluation/latest` | `application/json` | Latest assertion evaluation summary and conservative side-effect bounds. |

---

## 5. Safety & Failure Containment Guarantees

1. **Attempt Identity & Deduplication:** Every MCP tool call is assigned a unique `attempt_id`. Replays sharing the same `idempotency_key` produce identical `effect_id` values.
2. **Conservative Side-Effect Accounting:** If a tool call times out or encounters a crash mid-flight, the server returns an error with `commit_status: UNCERTAIN`. RecoveryBench never fabricates success or zero side effects.
3. **OS Process Boundary:** In isolated mode (`--isolation isolated`), tools with uncooperative execution models run in spawned child processes that are forcibly terminated if deadlines expire, preventing host thread leaks.
