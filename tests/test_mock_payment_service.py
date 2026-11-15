"""Unit tests for the mock payment service and idempotency caching."""

from recoverybench.tools.payment import MockPaymentService


def test_mock_payment_first_charge() -> None:
    service = MockPaymentService()
    result = service.execute(
        action="charge",
        params={"amount": 5000, "currency": "USD", "customer_id": "cust_1"},
        idempotency_key="key_abc",
    )
    assert result.success is True
    assert result.committed is True
    assert result.data["charge_id"] == "ch_sim_0001"
    assert result.data["idempotent_replay"] is False
    assert len(service.charges) == 1


def test_mock_payment_idempotent_replay() -> None:
    service = MockPaymentService()
    # First invocation: commits charge
    res1 = service.execute(
        action="charge",
        params={"amount": 5000, "currency": "USD", "customer_id": "cust_1"},
        idempotency_key="key_abc",
    )
    assert res1.committed is True
    assert len(service.charges) == 1

    # Second invocation with SAME idempotency key: dedupes without new commit
    res2 = service.execute(
        action="charge",
        params={"amount": 5000, "currency": "USD", "customer_id": "cust_1"},
        idempotency_key="key_abc",
    )
    assert res2.success is True
    assert res2.committed is False  # No new side effect
    assert res2.data["charge_id"] == "ch_sim_0001"
    assert res2.data["idempotent_replay"] is True
    assert len(service.charges) == 1  # Still exactly 1 charge in system


def test_mock_payment_different_keys_double_charge() -> None:
    service = MockPaymentService()
    # First invocation
    res1 = service.execute(
        action="charge",
        params={"amount": 5000, "currency": "USD", "customer_id": "cust_1"},
        idempotency_key="key_111",
    )
    # Second invocation with DIFFERENT key (e.g. naive retry)
    res2 = service.execute(
        action="charge",
        params={"amount": 5000, "currency": "USD", "customer_id": "cust_1"},
        idempotency_key="key_222",
    )
    assert res1.committed is True
    assert res2.committed is True
    assert res1.data["charge_id"] != res2.data["charge_id"]
    assert len(service.charges) == 2  # Double charge occurred!


def test_mock_payment_reset() -> None:
    service = MockPaymentService()
    service.execute(
        action="charge",
        params={"amount": 1000},
        idempotency_key="key_xyz",
    )
    assert len(service.charges) == 1
    service.reset()
    assert len(service.charges) == 0
    assert len(service.idempotency_map) == 0
