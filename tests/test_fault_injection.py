"""Unit tests for the Fault Injection Engine across all 6 supported fault modes."""

from recoverybench.errors import (
    MalformedResponseError,
    RateLimitError,
    ServiceUnavailableError,
    ToolTimeoutError,
)
from recoverybench.faults.injector import FaultInjector
from recoverybench.faults.types import FaultConfig, FaultType


def test_timeout_before_commit_injection() -> None:
    fault = FaultConfig(
        type=FaultType.TIMEOUT_BEFORE_COMMIT,
        target_tool="payment_gateway",
        target_action="charge",
        failure_count=1,
    )
    injector = FaultInjector([fault])

    # Attempt 1: Pre-commit should fail
    attempt = injector.register_attempt("payment_gateway", "charge")
    err_pre = injector.evaluate_pre_commit("payment_gateway", "charge", attempt)
    assert isinstance(err_pre, ToolTimeoutError)
    assert err_pre.committed is False

    # Attempt 2: Should succeed
    attempt = injector.register_attempt("payment_gateway", "charge")
    assert injector.evaluate_pre_commit("payment_gateway", "charge", attempt) is None


def test_timeout_after_commit_injection() -> None:
    fault = FaultConfig(
        type=FaultType.TIMEOUT_AFTER_COMMIT,
        target_tool="payment_gateway",
        target_action="charge",
        failure_count=1,
    )
    injector = FaultInjector([fault])

    attempt = injector.register_attempt("payment_gateway", "charge")
    # Pre-commit should NOT fail because commit is allowed
    assert injector.evaluate_pre_commit("payment_gateway", "charge", attempt) is None

    # Post-commit should inject timeout
    err_post = injector.evaluate_post_commit("payment_gateway", "charge", attempt)
    assert isinstance(err_post, ToolTimeoutError)
    assert err_post.committed is True

    # Attempt 2: Post-commit should succeed
    attempt = injector.register_attempt("payment_gateway", "charge")
    assert injector.evaluate_post_commit("payment_gateway", "charge", attempt) is None


def test_duplicate_delivery_injection() -> None:
    fault = FaultConfig(
        type=FaultType.DUPLICATE_DELIVERY,
        target_tool="payment_gateway",
        target_action="charge",
        failure_count=1,
        duplicate_count=2,
    )
    injector = FaultInjector([fault])

    attempt = injector.register_attempt("payment_gateway", "charge")
    is_dup, count = injector.is_duplicate_delivery("payment_gateway", "charge", attempt)
    assert is_dup is True
    assert count == 2

    # Attempt 2 should not duplicate
    attempt = injector.register_attempt("payment_gateway", "charge")
    is_dup2, count2 = injector.is_duplicate_delivery("payment_gateway", "charge", attempt)
    assert is_dup2 is False
    assert count2 == 0


def test_http_429_then_success() -> None:
    fault = FaultConfig(
        type=FaultType.HTTP_429_THEN_SUCCESS,
        target_tool="payment_gateway",
        failure_count=2,
        retry_after_seconds=2.5,
    )
    injector = FaultInjector([fault])

    # Attempts 1 and 2 fail with 429
    for _ in (1, 2):
        attempt = injector.register_attempt("payment_gateway", "charge")
        err = injector.evaluate_pre_commit("payment_gateway", "charge", attempt)
        assert isinstance(err, RateLimitError)
        assert err.retry_after_s == 2.5

    # Attempt 3 succeeds
    attempt = injector.register_attempt("payment_gateway", "charge")
    assert injector.evaluate_pre_commit("payment_gateway", "charge", attempt) is None


def test_http_500_transient() -> None:
    fault = FaultConfig(
        type=FaultType.HTTP_500_TRANSIENT,
        target_tool="payment_gateway",
        failure_count=1,
    )
    injector = FaultInjector([fault])

    attempt = injector.register_attempt("payment_gateway", "charge")
    err = injector.evaluate_pre_commit("payment_gateway", "charge", attempt)
    assert isinstance(err, ServiceUnavailableError)

    attempt = injector.register_attempt("payment_gateway", "charge")
    assert injector.evaluate_pre_commit("payment_gateway", "charge", attempt) is None


def test_malformed_tool_response_modes() -> None:
    # 1. Raise exception mode
    fault_exc = FaultConfig(
        type=FaultType.MALFORMED_TOOL_RESPONSE,
        target_tool="payment_gateway",
        target_action="charge",
        failure_count=1,
        malformed_mode="raise_exception",
    )
    injector_exc = FaultInjector([fault_exc])
    attempt = injector_exc.register_attempt("payment_gateway", "charge")
    err = injector_exc.evaluate_pre_commit("payment_gateway", "charge", attempt)
    assert isinstance(err, MalformedResponseError)

    # 2. Corrupt payload mode
    fault_corrupt = FaultConfig(
        type=FaultType.MALFORMED_TOOL_RESPONSE,
        target_tool="payment_gateway",
        target_action="charge",
        failure_count=1,
        malformed_mode="corrupt_payload",
    )
    injector_corrupt = FaultInjector([fault_corrupt])
    attempt = injector_corrupt.register_attempt("payment_gateway", "charge")
    # Pre-commit passes so tool can run
    assert injector_corrupt.evaluate_pre_commit("payment_gateway", "charge", attempt) is None
    # Payload evaluator corrupts the payload
    corrupted = injector_corrupt.evaluate_malformed_payload(
        "payment_gateway", "charge", attempt, {"charge_id": "ch_sim_01"}
    )
    assert corrupted is not None
    assert corrupted.get("_corrupted") is True
    assert "charge_id" not in corrupted


def test_deterministic_seed_consistency() -> None:
    # Seed 12345 with jitter
    fault1 = FaultConfig(
        type=FaultType.HTTP_429_THEN_SUCCESS,
        target_tool="payment_gateway",
        failure_count=3,
        retry_after_seconds=1.0,
        seed=12345,
        jitter_range=(0.1, 0.9),
    )
    fault2 = FaultConfig(
        type=FaultType.HTTP_429_THEN_SUCCESS,
        target_tool="payment_gateway",
        failure_count=3,
        retry_after_seconds=1.0,
        seed=12345,
        jitter_range=(0.1, 0.9),
    )

    inj1 = FaultInjector([fault1], seed=12345)
    inj2 = FaultInjector([fault2], seed=12345)

    # Identical seed produces identical jitter sequence
    for _ in range(3):
        att1 = inj1.register_attempt("payment_gateway", "charge")
        att2 = inj2.register_attempt("payment_gateway", "charge")
        err1 = inj1.evaluate_pre_commit("payment_gateway", "charge", att1)
        err2 = inj2.evaluate_pre_commit("payment_gateway", "charge", att2)
        assert isinstance(err1, RateLimitError)
        assert isinstance(err2, RateLimitError)
        assert err1.retry_after_s == err2.retry_after_s


def test_deterministic_probabilistic_failure_rate() -> None:
    # 50% failure rate with fixed seed
    fault = FaultConfig(
        type=FaultType.HTTP_500_TRANSIENT,
        target_tool="payment_gateway",
        failure_count=10,
        seed=999,
        failure_rate=0.5,
    )
    inj1 = FaultInjector([fault], seed=999)
    inj2 = FaultInjector([fault], seed=999)

    outcomes1 = [inj1.should_trigger(fault, att) for att in range(1, 11)]
    outcomes2 = [inj2.should_trigger(fault, att) for att in range(1, 11)]

    # Exact deterministic sequence match
    assert outcomes1 == outcomes2
    # Not all True and not all False (demonstrating probability with seed)
    assert any(outcomes1)
    assert not all(outcomes1)
