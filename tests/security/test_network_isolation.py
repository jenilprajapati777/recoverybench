"""Network isolation tests verifying --network=none prevents outbound traffic and DNS resolution.

NOTE: Real container execution tests are skipped when Docker is unavailable in the environment.
They must NEVER be fabricated or reported as passed when Docker is absent.
"""

import http.server
import json
import socket
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from recoverybench.agent.base import CompletionStatus
from recoverybench.agent.idempotent import IdempotentPaymentAgent
from recoverybench.agent.naive import NaivePaymentAgent
from recoverybench.engine.runner import ExecutionEngine
from recoverybench.ledger.sqlite import SQLiteLedger
from recoverybench.models import EvaluationVerdict
from recoverybench.sandbox.backend import DockerExecutionBackend
from recoverybench.sandbox.config import SandboxConfig
from recoverybench.sandbox.docker_client import DockerClient
from recoverybench.sandbox.spec import AgentSpec
from recoverybench.scenarios.registry import ScenarioRegistry
from recoverybench.tools.base import ToolRegistry
from recoverybench.tools.payment import MockPaymentService

client = DockerClient()
docker_available, docker_reason = client.is_docker_available()


def test_host_connectivity_baseline() -> None:
    """Baseline check confirming that host has standard network and loopback capabilities.

    This ensures that container network isolation failures under --network=none
    are caused by container isolation rather than a broken host socket stack.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert port > 0


def test_host_dns_resolution_baseline() -> None:
    """Baseline check testing whether host environment permits public DNS queries."""
    try:
        ip = socket.gethostbyname("google.com")
        assert len(ip) > 0
    except socket.gaierror:
        pytest.skip("Host environment does not have external DNS connectivity (offline or firewall)")


def test_host_outbound_connectivity_baseline() -> None:
    """Baseline check testing whether host environment permits external TCP egress."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        s.connect(("1.1.1.1", 80))
        s.close()
    except OSError:
        s.close()
        pytest.skip("Host environment does not permit external TCP egress (offline or firewall)")


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_container_dns_resolution_blocked() -> None:
    """Verify DNS resolution inside --network=none container fails completely."""
    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    dns_cmd = [
        "python",
        "-c",
        "import socket; socket.gethostbyname('google.com')",
    ]

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        image,
    ] + dns_cmd

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    # DNS lookup MUST fail inside an isolated container
    assert result.returncode != 0
    assert "gaierror" in result.stderr or "Temporary failure in name resolution" in result.stderr or "Name or service not known" in result.stderr


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_container_outbound_tcp_blocked() -> None:
    """Verify outbound TCP connection inside --network=none container fails with network unreachable."""
    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    tcp_cmd = [
        "python",
        "-c",
        "import socket; s = socket.socket(); s.settimeout(2); s.connect(('1.1.1.1', 80))",
    ]

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        image,
    ] + tcp_cmd

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    # TCP connection MUST fail
    assert result.returncode != 0
    assert "Network is unreachable" in result.stderr or "timed out" in result.stderr or "OSError" in result.stderr


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_container_outbound_http_blocked() -> None:
    """Verify outbound HTTP request inside --network=none container fails."""
    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    http_cmd = [
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://1.1.1.1', timeout=2)",
    ]

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        image,
    ] + http_cmd

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "URLError" in result.stderr or "Network is unreachable" in result.stderr


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_container_outbound_https_blocked() -> None:
    """Verify outbound HTTPS request inside --network=none container fails."""
    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    https_cmd = [
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('https://1.1.1.1', timeout=2)",
    ]

    cmd = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        image,
    ] + https_cmd

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    assert result.returncode != 0
    assert "URLError" in result.stderr or "Network is unreachable" in result.stderr or "OSError" in result.stderr


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_container_host_local_access_blocked() -> None:
    """Verify container cannot access host-local HTTP services when isolated with --network=none."""
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    host_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        server.shutdown()
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    try:
        # Attempt connection to host.docker.internal and 127.0.0.1
        code = (
            "import urllib.request\n"
            f"for host in ['host.docker.internal', '127.0.0.1']:\n"
            "    try:\n"
            f"        urllib.request.urlopen(f'http://{{host}}:{host_port}', timeout=2)\n"
            "        print('REACHED')\n"
            "    except Exception as e:\n"
            "        print(f'BLOCKED:{type(e).__name__}')\n"
        )
        cmd = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            image,
            "python",
            "-c",
            code,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        # Host server MUST NOT be reached from inside container
        assert "REACHED" not in result.stdout
        assert "BLOCKED" in result.stdout or result.returncode != 0
    finally:
        server.shutdown()


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_container_loopback_behavior() -> None:
    """Verify container loopback 127.0.0.1 is isolated within its own network namespace."""
    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    # Inside container: start a simple socket listener on 127.0.0.1:45678, connect to it, send/recv
    code = (
        "import socket, threading\n"
        "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "s.bind(('127.0.0.1', 45678))\n"
        "s.listen(1)\n"
        "def server():\n"
        "    conn, _ = s.accept()\n"
        "    conn.sendall(b'PONG')\n"
        "    conn.close()\n"
        "t = threading.Thread(target=server, daemon=True)\n"
        "t.start()\n"
        "c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "c.connect(('127.0.0.1', 45678))\n"
        "data = c.recv(1024)\n"
        "c.close()\n"
        "s.close()\n"
        "print(data.decode('utf-8'))\n"
    )
    cmd = [
        "docker",
        "run",
        "--rm",
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        image,
        "python",
        "-c",
        code,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    assert result.returncode == 0
    assert result.stdout.strip() == "PONG"


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_inspected_container_runtime_configuration(tmp_path: Path) -> None:
    """Launch container and inspect Docker daemon runtime properties via `docker inspect`."""
    cfg = SandboxConfig(
        memory_limit="512m",
        cpu_limit="1.0",
        pids_limit=64,
        timeout_seconds=30,
        user="65534:65534",
    )
    if not client.check_image_exists(cfg.runtime_image):
        pytest.skip(f"Docker runtime image '{cfg.runtime_image}' not available locally")

    container_name = f"rb_inspect_test_{uuid.uuid4().hex[:8]}"
    staged_dir = tmp_path / "staged"
    staged_dir.mkdir()
    (staged_dir / "worker.py").write_text("import time; time.sleep(10)", encoding="utf-8")

    cmd = client.build_run_command(
        container_name=container_name,
        staged_dir=staged_dir,
        env_vars={"TEST_VAR": "1"},
        custom_config=cfg,
    )
    # Run in detached mode for inspection
    if "-i" in cmd:
        cmd[cmd.index("-i")] = "-d"
    else:
        cmd.insert(2, "-d")

    try:
        run_res = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        assert run_res.returncode == 0, f"Failed to start container: {run_res.stderr}"

        inspect_res = subprocess.run(["docker", "inspect", container_name], capture_output=True, text=True, timeout=20)
        assert inspect_res.returncode == 0
        inspect_data = json.loads(inspect_res.stdout)
        assert len(inspect_data) > 0
        container_info = inspect_data[0]
        host_config = container_info.get("HostConfig", {})
        config = container_info.get("Config", {})

        # 1. Network mode must be 'none'
        assert host_config.get("NetworkMode") == "none"

        # 2. Readonly root filesystem
        assert host_config.get("ReadonlyRootfs") is True

        # 3. Dropped capabilities
        assert "ALL" in host_config.get("CapDrop", [])

        # 4. No new privileges
        security_opts = host_config.get("SecurityOpt", [])
        assert any("no-new-privileges" in opt for opt in security_opts)

        # 5. Resource limits
        assert host_config.get("Memory") == 536870912  # 512m
        assert host_config.get("NanoCpus") == 1000000000  # 1.0 CPU
        assert host_config.get("PidsLimit") == 64

        # 6. Privileged mode disabled & no host namespaces/ports/devices
        assert host_config.get("Privileged") is False
        assert host_config.get("PublishAllPorts") is False
        assert host_config.get("PidMode") != "host"
        assert len(host_config.get("PortBindings") or {}) == 0
        assert len(host_config.get("Devices") or []) == 0

        # 7. Non-root user
        user = config.get("User", "")
        assert user in ("65534:65534", "nobody:nogroup", "nobody")

        # 8. Verify effective user inside running container process
        id_res = subprocess.run(["docker", "exec", container_name, "id", "-u"], capture_output=True, text=True, timeout=15)
        if id_res.returncode == 0:
            uid_str = id_res.stdout.strip()
            assert uid_str in ("65534", "nobody") or (uid_str.isdigit() and int(uid_str) != 0)

        # 9. Read-only workspace mount and no socket/device mounts
        mounts = container_info.get("Mounts", [])
        for m in mounts:
            if m.get("Destination") == "/workspace":
                assert m.get("RW") is False
                assert m.get("Mode") in ("ro", "ro,Z")
            src = m.get("Source", "")
            assert "docker.sock" not in src
            assert "/dev" not in src

    finally:
        client.kill_and_remove(container_name)


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_container_control_bridge_vs_isolated_comparison() -> None:
    """Verify non-isolated control container CAN reach host service, but isolated container CANNOT."""
    server = http.server.HTTPServer(("0.0.0.0", 0), http.server.SimpleHTTPRequestHandler)
    host_port = server.server_address[1]
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        server.shutdown()
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    try:
        # Step 1: Control Container with default/bridge network and host gateway
        control_cmd = [
            "docker",
            "run",
            "--rm",
            "--add-host=host.docker.internal:host-gateway",
            image,
            "python",
            "-c",
            f"import urllib.request; urllib.request.urlopen('http://host.docker.internal:{host_port}', timeout=3); print('CONTROL_REACHED')",
        ]
        control_res = subprocess.run(control_cmd, capture_output=True, text=True, timeout=25)

        # Step 2: Isolated Container with --network=none
        isolated_cmd = [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            image,
            "python",
            "-c",
            f"import urllib.request; urllib.request.urlopen('http://host.docker.internal:{host_port}', timeout=2); print('ISOLATED_REACHED')",
        ]
        isolated_res = subprocess.run(isolated_cmd, capture_output=True, text=True, timeout=25)

        # Contrast: Control succeeded if environment allowed, but isolated container MUST fail
        assert "ISOLATED_REACHED" not in isolated_res.stdout
        assert isolated_res.returncode != 0
        if control_res.returncode == 0:
            assert "CONTROL_REACHED" in control_res.stdout
    finally:
        server.shutdown()


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_docker_real_container_ipc_roundtrip_idempotent() -> None:
    """Execute IdempotentPaymentAgent through real Docker container and verify IPC + verdict."""
    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    scenario = ScenarioRegistry().get("payment_timeout_after_commit")
    assert scenario is not None

    ledger = SQLiteLedger(":memory:")
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    spec = AgentSpec.from_callable(IdempotentPaymentAgent)
    backend = DockerExecutionBackend()
    result = engine.run(scenario, spec, backend=backend)

    assert result.passed is True
    assert result.verdict == EvaluationVerdict.PASS.value
    assert (result.agent_raw_result or {}).get("status") == CompletionStatus.SUCCEEDED.value
    assert result.evaluation.idempotency_key_stable is True
    assert result.evaluation.duplicate_effect_count == 0

    # Verify ledger has preserved operation_id and attempt_ids
    events = ledger.get_events(result.run_id)
    assert len(events) >= 2
    for ev in events:
        assert ev.operation_id == f"op_{scenario.scenario_id}"
        assert ev.attempt_id is not None
        assert ev.attempt_id.startswith(f"att_{result.run_id}_")


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_docker_real_container_ipc_roundtrip_naive() -> None:
    """Execute NaivePaymentAgent through real Docker container and verify duplicate effect failure."""
    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    scenario = ScenarioRegistry().get("payment_timeout_after_commit")
    assert scenario is not None

    ledger = SQLiteLedger(":memory:")
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    spec = AgentSpec.from_callable(NaivePaymentAgent)
    backend = DockerExecutionBackend()
    result = engine.run(scenario, spec, backend=backend)

    assert result.passed is False
    assert result.verdict == EvaluationVerdict.GENUINE_FAILURE.value
    assert result.evaluation.duplicate_effect_count > 0


@pytest.mark.skipif(not docker_available, reason=f"Docker Engine unavailable in test environment: {docker_reason}")
def test_docker_real_container_ipc_unknown_not_converted(tmp_path: Path) -> None:
    """Execute an agent returning None in Docker and verify UNKNOWN completion prevents false success."""
    image = SandboxConfig().runtime_image
    if not client.check_image_exists(image):
        pytest.skip(f"Docker runtime image '{image}' not available locally")

    agent_file = tmp_path / "none_agent.py"
    agent_file.write_text(
        "def run_none_agent(task, tool):\n"
        "    try:\n"
        "        tool.call('payment_gateway', 'charge', {'amount': 100})\n"
        "    except Exception:\n"
        "        pass\n"
        "    return None\n",
        encoding="utf-8",
    )

    scenario = ScenarioRegistry().get("payment_timeout_after_commit")
    assert scenario is not None

    ledger = SQLiteLedger(":memory:")
    registry = ToolRegistry()
    registry.register(MockPaymentService())
    engine = ExecutionEngine(ledger=ledger, tool_registry=registry)

    spec = AgentSpec.from_file(agent_file, entrypoint="run_none_agent")
    backend = DockerExecutionBackend()
    result = engine.run(scenario, spec, backend=backend)

    assert result.passed is False
    assert (result.agent_raw_result or {}).get("status") == CompletionStatus.UNKNOWN.value
    assert result.verdict == EvaluationVerdict.INSUFFICIENT_EVIDENCE.value
