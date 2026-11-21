"""Tests for the declarative mock tool registration API (@mock_tool)."""

import concurrent.futures
from typing import Any

import pytest

import recoverybench as rb
from recoverybench.tools import DeclarativeToolService, mock_tool


def test_mock_tool_decorator_basic_registration() -> None:
    """Verify basic @mock_tool declaration and direct invocation."""
    @mock_tool(name="order_service")
    def create_order(item_id: str, quantity: int = 1) -> dict[str, Any]:
        return {"order_id": "ord_1001", "item_id": item_id, "quantity": quantity}

    assert isinstance(create_order, DeclarativeToolService)
    assert create_order.name == "order_service"

    # Direct function invocation works
    res = create_order(item_id="sku_abc", quantity=5)
    assert res == {"order_id": "ord_1001", "item_id": "sku_abc", "quantity": 5}


def test_mock_tool_parameter_validation() -> None:
    """Verify argument validation fails with clear error messages on missing or invalid args."""
    @mock_tool(name="email_service", action="send")
    def send_email(recipient: str, subject: str) -> dict[str, Any]:
        return {"sent": True}

    # Missing required argument 'subject'
    with pytest.raises(TypeError) as exc_info:
        send_email.execute("send", {"recipient": "user@example.com"})
    assert "Invalid arguments for tool 'email_service.send'" in str(exc_info.value)
    assert "missing" in str(exc_info.value).lower()


def test_mock_tool_effect_lifecycle_and_idempotency() -> None:
    """Verify automatic effect_id, evidence_reference generation and idempotency caching."""
    @mock_tool(name="payment_service", action="charge", mutates=True)
    def charge_customer(amount: float) -> dict[str, Any]:
        return {"charged": amount, "status": "ok"}

    # Attempt 1: First commit
    res1 = charge_customer.execute(
        "charge",
        {"amount": 50.0},
        idempotency_key="key_001",
        operation_id="op_1",
        attempt_id="att_1",
    )
    assert res1.success is True
    assert res1.committed is True
    assert res1.evidence_reference == "payment_service_charge_0001"
    assert res1.effect_id == "eff_payment_service_charge_0001"
    assert res1.data["idempotent_replay"] is False

    # Attempt 2: Idempotent replay with same key
    res2 = charge_customer.execute(
        "charge",
        {"amount": 50.0},
        idempotency_key="key_001",
        operation_id="op_1",
        attempt_id="att_2",
    )
    assert res2.success is True
    assert res2.committed is False  # Replay! No new commit
    assert res2.evidence_reference == "payment_service_charge_0001"
    assert res2.effect_id == "eff_payment_service_charge_0001"
    assert res2.data["idempotent_replay"] is True

    # Attempt 3: Unsafe retry with DIFFERENT key triggers second distinct commit
    res3 = charge_customer.execute(
        "charge",
        {"amount": 50.0},
        idempotency_key="key_002",
        operation_id="op_1",
        attempt_id="att_3",
    )
    assert res3.success is True
    assert res3.committed is True  # Second commit!
    assert res3.evidence_reference == "payment_service_charge_0002"
    assert res3.effect_id == "eff_payment_service_charge_0002"


def test_mock_tool_read_only_action_not_committed() -> None:
    """Verify read-only actions (mutates=False) do not generate committed side effects."""
    @mock_tool(name="account_service", action="get_balance", mutates=False)
    def get_balance(account_id: str) -> dict[str, Any]:
        return {"balance": 1500.0}

    res = get_balance.execute("get_balance", {"account_id": "acc_1"})
    assert res.success is True
    assert res.committed is False
    assert res.effect_id is None


def test_mock_tool_multiple_actions() -> None:
    """Verify adding multiple actions to the same mock tool service."""
    service = DeclarativeToolService(name="inventory_service")

    @service.action("reserve", mutates=True)
    def reserve(item_id: str, qty: int) -> dict[str, Any]:
        return {"reserved": True, "item_id": item_id}

    @service.action("check_stock", mutates=False)
    def check_stock(item_id: str) -> dict[str, Any]:
        return {"available": 100}

    res_check = service.execute("check_stock", {"item_id": "sku_1"})
    assert res_check.committed is False
    assert res_check.data["available"] == 100

    res_resv = service.execute("reserve", {"item_id": "sku_1", "qty": 2})
    assert res_resv.committed is True
    assert res_resv.data["reserved"] is True


def test_mock_tool_in_test_agent_and_tester() -> None:
    """Verify using declarative mock tools directly in test_agent and RecoveryBenchTester."""
    @mock_tool(name="billing_gateway", action="charge", mutates=True)
    def charge_tool(amount: float) -> dict[str, Any]:
        return {"status": "paid", "amount": amount}

    @rb.agent
    def billing_agent(task: rb.TaskContext, tool_call: rb.ToolCaller) -> rb.AgentResult:
        res = tool_call("billing_gateway", "charge", {"amount": 75.0}, idempotency_key="billing_key_1")
        return rb.AgentResult(success=True, data=res)

    # 1. Pass as single ToolService to test_agent
    res1 = rb.test_agent(
        "payment_timeout_before_commit",
        billing_agent,
        custom_tools=charge_tool,
    )
    assert res1.passed
    assert res1.verdict == "PASS"

    # 2. Pass in a list of ToolServices to RecoveryBenchTester
    with rb.RecoveryBenchTester(custom_tools=[charge_tool]) as tester:
        res2 = tester.test(
            "payment_timeout_before_commit",
            billing_agent,
        )
        assert res2.passed


def test_idempotency_scoped_to_operation_and_action() -> None:
    """Verify that idempotency caching is strictly scoped to (operation_id, action, idempotency_key)."""
    service = DeclarativeToolService(name="multi_action_gateway")

    @service.action("reserve", mutates=True)
    def reserve(sku: str, qty: int) -> dict[str, Any]:
        return {"reserved_sku": sku, "qty": qty}

    @service.action("charge", mutates=True)
    def charge(amount: float) -> dict[str, Any]:
        return {"charged_amount": amount}

    shared_key = "idempotency_shared_key_100"

    # 1. Action 1 under Operation 1: First commit
    res_reserve_1 = service.execute(
        "reserve",
        {"sku": "SKU_A", "qty": 1},
        idempotency_key=shared_key,
        operation_id="op_alpha",
    )
    assert res_reserve_1.committed is True
    assert res_reserve_1.data["idempotent_replay"] is False
    assert res_reserve_1.data["reserved_sku"] == "SKU_A"
    reserve_effect_id = res_reserve_1.effect_id

    # 2. Action 1 under Operation 1 (Retry with exact same op, action, key): Idempotent replay!
    res_reserve_replay = service.execute(
        "reserve",
        {"sku": "SKU_A", "qty": 1},
        idempotency_key=shared_key,
        operation_id="op_alpha",
    )
    assert res_reserve_replay.committed is False
    assert res_reserve_replay.data["idempotent_replay"] is True
    assert res_reserve_replay.effect_id == reserve_effect_id
    assert res_reserve_replay.evidence_reference == res_reserve_1.evidence_reference

    # 3. Action 2 under Operation 1 (Different action, SAME idempotency key): Must NOT hit reserve's cache!
    res_charge_1 = service.execute(
        "charge",
        {"amount": 250.0},
        idempotency_key=shared_key,
        operation_id="op_alpha",
    )
    assert res_charge_1.committed is True
    assert res_charge_1.data["idempotent_replay"] is False
    assert res_charge_1.data["charged_amount"] == 250.0
    assert res_charge_1.effect_id != reserve_effect_id

    # 4. Action 2 under Operation 2 (Different operation, SAME action and key): Must NOT hit Operation 1's cache!
    res_charge_op_beta = service.execute(
        "charge",
        {"amount": 250.0},
        idempotency_key=shared_key,
        operation_id="op_beta",
    )
    assert res_charge_op_beta.committed is True
    assert res_charge_op_beta.data["idempotent_replay"] is False
    assert res_charge_op_beta.effect_id != res_charge_1.effect_id

    # 5. Different key under same operation and action: Independent commit
    res_charge_diff_key = service.execute(
        "charge",
        {"amount": 250.0},
        idempotency_key="different_key_200",
        operation_id="op_alpha",
    )
    assert res_charge_diff_key.committed is True
    assert res_charge_diff_key.data["idempotent_replay"] is False
    assert res_charge_diff_key.effect_id != res_charge_1.effect_id


def test_idempotency_reset_and_thread_safety() -> None:
    """Verify that reset clears cache and that concurrent access is thread-safe."""
    service = DeclarativeToolService(name="concurrent_service")

    @service.action("mutate", mutates=True)
    def mutate(counter: int) -> dict[str, Any]:
        return {"val": counter}

    # Verify reset clears cache
    res1 = service.execute("mutate", {"counter": 1}, idempotency_key="k1", operation_id="op1")
    assert res1.committed is True
    res_cached = service.execute("mutate", {"counter": 1}, idempotency_key="k1", operation_id="op1")
    assert res_cached.committed is False

    service.reset()
    res_after_reset = service.execute("mutate", {"counter": 1}, idempotency_key="k1", operation_id="op1")
    assert res_after_reset.committed is True
    assert res_after_reset.effect_id == "eff_concurrent_service_mutate_0001"

    # Thread-safety under concurrent calls
    def worker(i: int) -> bool:
        # Half workers use same key, half use distinct keys
        key = "shared_thread_key" if i % 2 == 0 else f"thread_key_{i}"
        r = service.execute("mutate", {"counter": i}, idempotency_key=key, operation_id="op_conc")
        return r.success

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(worker, range(20)))

    assert all(results)
    assert len(service._history) > 0
