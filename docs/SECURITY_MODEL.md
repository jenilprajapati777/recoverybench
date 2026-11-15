# RecoveryBench - Security Model & Threat Architecture

**Version:** 0.3.0  
**Security Posture:** Zero-Trust Execution, Real Kernel/Container Isolation, Fail-Closed  

---

## 1. Guiding Security Principles

RecoveryBench is built to test potentially buggy, recursive, or unpredictable agent behaviors under simulated distributed systems failure. Because agents may execute uncontrolled loops, retry strategies, or attempt direct network operations, the harness enforces defense-in-depth isolation:

1. **Operating-System & Container Network Isolation:** All isolated agent runs are contained within Linux containers configured with `--network=none`. The container has no network interfaces other than a disconnected loopback device. Direct outbound TCP/UDP, DNS resolution, and HTTP requests are blocked at the Linux kernel/network namespace level.
2. **Zero Production Credentials in Containers:** Agent source trees are staged into isolated temporary workspaces where credentials, environment files, SSH keys, cloud profiles, virtual environments, and private certificates are stripped before mounting into the sandbox.
3. **Controlled Host-Worker IPC:** Host-agent communication occurs strictly over unidirectional stdin/stdout pipes using a framed, schema-validated JSON Lines protocol. User stdout is redirected to stderr to prevent protocol stream corruption.
4. **Append-Only Effect Ledger:** All business mutations terminate strictly inside simulated mock service instances. Side effects, retries, and attempts are recorded in an append-only SQLite ledger with distinct `operation_id`, `attempt_id`, and `effect_id` boundaries.
5. **Circuit Breakers & Hard Resource Constraints:** To prevent rogue agents from burning host resources or executing fork-bombs, containers enforce strict limits: memory (512 MB default), CPU (1.0 core), PIDs (64 limit), read-only root filesystems, and execution timeouts.
6. **Fail-Closed Execution:** When isolation is requested (or defaulted), inability to connect to Docker or locate runtime images fails closed with Exit Code 3 (`INFRASTRUCTURE_ERROR`). The system never silently downgrades to trusted in-process execution.

---

## 2. Threat Modeling & Mitigations

| Threat ID | Threat Description | Severity | Mitigation Strategy |
| :--- | :--- | :--- | :--- |
| **THREAT-01** | **Accidental Production Egress**<br>Agent code bypasses `ToolClient` and directly calls live Stripe/PayPal/AWS APIs via raw sockets or `urllib`/`requests`. | Critical | **Container Network Isolation:** Agents run inside a Docker container configured with `--network=none`. Outbound socket creation and DNS resolution are blocked by the OS kernel. Tool calls must be routed via the stdin/stdout IPC protocol to host-side simulated mock services. |
| **THREAT-02** | **Credential Leakage in Logs / Sandbox**<br>API keys, passwords, or cloud credentials stolen by agent code or leaked in reports. | Critical | **Staged Workspace & Redaction:** `WorkspaceStaging` strictly omits `.env*`, `.ssh`, `.aws`, `.azure`, `.venv`, `.git`, `node_modules`, `*.pem`, `*.key`. Staged workspace is mounted read-only (`:ro`). The IPC worker masks secrets (`sk_live_...`, tokens, bearer headers) on stderr. Reports hash sensitive keys with SHA-256. |
| **THREAT-03** | **Filesystem Traversal & Symlink Escape**<br>Agent attempts directory traversal (`../`) or symlink exploitation to read host secrets. | High | **Symlink Escape Detection:** `WorkspaceStaging` inspects every file and symlink; any symlink pointing outside the workspace root raises `ConfigurationError`. Root filesystem is mounted `--read-only`. Temporary writable directory `/tmp` is mounted with `noexec,nosuid`. |
| **THREAT-04** | **Unbounded Agent Retry / Fork-Bomb (DoS)**<br>An agent with a flawed `while True` retry loop or fork-bomb hangs the host. | High | **Strict Resource Limits:** Container runtime limits enforce `--pids-limit=64`, `--memory=512m`, `--cpus=1.0`, and wall-clock execution timeouts (default 30s). Process is killed deterministically (`docker rm -f`) upon timeout. |
| **THREAT-05** | **Arbitrary Code Deserialization**<br>Harness accepts pickled functions or untrusted serialization payloads across container boundary. | Critical | **No Pickle Transfer:** Callables are NEVER transferred via `pickle` or `dill`. The harness uses `AgentSpec` referencing on-disk modules (`module_path` + `entrypoint`). Worker loads code via standard `importlib`. |
| **THREAT-06** | **Privilege Escalation**<br>Agent attempts `sudo`, setuid binary execution, or container escape. | High | **Rootless & Cap-Drop:** Container drops all capabilities (`--cap-drop=ALL`), enforces `--security-opt=no-new-privileges`, and runs as non-root user (`nobody:nogroup` / `65534:65534`). |
| **THREAT-07** | **False Success Reporting**<br>Agent returns empty result, uncaught exception, or swallowed error, falsely reported as recovery. | High | **P0-2 Strict Normalization:** Returns are categorized into `SUCCEEDED`, `FAILED`, and `UNKNOWN`. Ambiguous completions evaluate to `INSUFFICIENT_EVIDENCE` or `GENUINE_FAILURE`. |

---

## 3. Container Sandbox Security Specification

When running with `--isolation isolated`, the execution harness constructs a container invocation with the following hardened parameters:

```bash
docker run -i \
  --name rb_sandbox_<uuid> \
  --network=none \
  --read-only \
  --cap-drop=ALL \
  --security-opt=no-new-privileges \
  --pids-limit=64 \
  --memory=512m \
  --cpus=1.0 \
  --user=nobody:nogroup \
  --tmpfs=/tmp:rw,noexec,nosuid,size=64m \
  -v <staged_workspace_path>:/workspace:ro \
  -w /workspace \
  -e PATH=/usr/local/bin:/usr/bin:/bin \
  -e PYTHONPATH=/workspace \
  python:3.11-slim \
  python -u /workspace/_recoverybench_worker.py
```

### Prohibited Configuration Directives
The following flags are strictly prohibited in the container execution command and will fail harness validation:
- `--privileged`
- `--net=host` / `--network=host`
- `--pid=host`
- `--device`
- Volume mounts targeting `/var/run/docker.sock` or host root `/`

---

## 4. Execution Isolation Modes

RecoveryBench provides two distinct execution modes:

### 1. `ISOLATED` (Default & Recommended)
- Executes the agent inside a dedicated Linux container with `--network=none`.
- Staged workspace with credential stripping and read-only mounting.
- Strict CPU, memory, PID, and timeout bounds.
- Guaranteed container destruction upon test completion (`finally: kill_and_remove`).
- If Docker is unavailable or the runtime image is missing: **FAILS CLOSED** with Exit Code 3.

### 2. `TRUSTED` (Explicit Development Mode)
- Executes the agent directly in the current Python interpreter.
- Requires explicit flag: `--isolation trusted`.
- Displays prominent warning banner: `WARNING: Running in TRUSTED in-process mode. No network isolation enforced.`
- Intended solely for rapid local iteration, unit testing, and environments where Docker Engine cannot run.

---

## 5. Fail-Closed Exit Code Invariants

RecoveryBench enforces deterministic exit codes across all execution paths:

| Exit Code | Classification | Trigger Condition |
| :---: | :--- | :--- |
| **0** | **PASS** | All evaluated scenarios satisfied all recovery invariants (e.g. exactly one side effect committed, idempotency verified). |
| **1** | **FAIL** | One or more recovery invariants were violated (e.g. duplicate side effect detected, missing side effect, false success). |
| **2** | **CONFIG_ERROR** | Scenario YAML syntax error, invalid agent name, or missing scenario directory. |
| **3** | **INFRA_ERROR** | Docker requested but unavailable, missing runtime image, SQLite database lock, or harness I/O error. |

---

## 6. Security Boundaries, Kernel Sharing, and Verification Caveats

1. **Shared Host Kernel Boundary:** Linux containers rely on kernel namespaces (network, mount, pid, ipc, uts) and cgroups. They **share the host operating system kernel** and are **not hardware-isolated virtual machines** (such as AWS Firecracker microVMs or QEMU/KVM).
2. **Kernel Exploits & Zero-Days:** While `--cap-drop=ALL`, `--security-opt=no-new-privileges`, and non-root execution (`nobody:nogroup`) significantly minimize attack surface, containers do not mathematically guarantee containment against zero-day Linux kernel vulnerabilities or container runtime (runc) vulnerabilities.
3. **Host-Dependent Verification Status:** In environments where Docker Engine is not installed or running (such as bare-metal Windows without Docker Desktop / WSL2), real container execution tests are SKIPPED and classified as **`NOT VERIFIED (BLOCKED_NO_DOCKER)`**. Network isolation is verified on Linux CI runners (GitHub Actions `ubuntu-latest`).
4. **Trusted Mode Advisory:** When `--isolation trusted` is selected, user code runs directly inside the host Python interpreter without network, filesystem, or resource isolation. Trusted mode must only be used with vetted code during local development.
