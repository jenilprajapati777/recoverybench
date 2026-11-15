"""Registry for discovering and loading RecoveryBench scenarios."""

from pathlib import Path

from recoverybench.config import get_settings
from recoverybench.errors import ConfigurationError
from recoverybench.faults.types import FaultConfig, FaultType
from recoverybench.scenarios.schema import ScenarioAssertions, ScenarioModel
from recoverybench.scenarios.validator import ScenarioValidator


class ScenarioRegistry:
    """Discovers and caches built-in and user-defined scenarios."""

    _default_instance: "ScenarioRegistry | None" = None

    @classmethod
    def get_default(cls) -> "ScenarioRegistry":
        """Return a cached default instance of ScenarioRegistry."""
        if cls._default_instance is None:
            cls._default_instance = cls()
        return cls._default_instance

    def __init__(self, scenarios_dir: Path | None = None) -> None:
        self.scenarios_dir = scenarios_dir or get_settings().scenarios_dir
        self._scenarios: dict[str, ScenarioModel] = {}
        self._register_builtins()

    def _register_builtins(self) -> None:
        """Register default built-in reference scenarios."""
        # Scenario 1: Timeout After Commit (Double Charge hazard)
        timeout_after = ScenarioModel(
            scenario_id="payment_timeout_after_commit",
            name="Payment Timeout After Commit (Two Generals' Hazard)",
            description=(
                "Simulates a payment transaction committing on the server, but the network connection "
                "dropping before the client receives confirmation. The agent must recover safely using a "
                "stable idempotency key to prevent double charging."
            ),
            target_tool="payment_gateway",
            target_action="charge",
            faults=[
                FaultConfig(
                    type=FaultType.TIMEOUT_AFTER_COMMIT,
                    target_tool="payment_gateway",
                    target_action="charge",
                    failure_count=1,
                    error_message="Network dropped before acknowledgement returned",
                )
            ],
            task={"amount": 5000, "currency": "USD", "customer_id": "cust_alice_42"},
            assertions=ScenarioAssertions(
                expected_side_effects=1,
                max_duplicate_effects=0,
                require_stable_idempotency_key=True,
                max_allowed_retries=3,
                expect_agent_success=True,
            ),
        )
        self._scenarios[timeout_after.scenario_id] = timeout_after

        # Scenario 2: Timeout Before Commit
        timeout_before = ScenarioModel(
            scenario_id="payment_timeout_before_commit",
            name="Payment Timeout Before Commit",
            description=(
                "Simulates a network timeout occurring before the payment service commits any state. "
                "The agent must retry safely to ensure the charge is actually committed."
            ),
            target_tool="payment_gateway",
            target_action="charge",
            faults=[
                FaultConfig(
                    type=FaultType.TIMEOUT_BEFORE_COMMIT,
                    target_tool="payment_gateway",
                    target_action="charge",
                    failure_count=1,
                    error_message="Socket connect timeout to payment gateway",
                )
            ],
            task={"amount": 3500, "currency": "USD", "customer_id": "cust_bob_17"},
            assertions=ScenarioAssertions(
                expected_side_effects=1,
                max_duplicate_effects=0,
                require_stable_idempotency_key=True,
                max_allowed_retries=3,
                expect_agent_success=True,
            ),
        )
        self._scenarios[timeout_before.scenario_id] = timeout_before

        # Scenario 3: HTTP 429 Transient Rate Limit
        rate_limit = ScenarioModel(
            scenario_id="payment_rate_limit_429",
            name="Payment HTTP 429 Rate Limit Recovery",
            description=(
                "Simulates transient rate-limiting (HTTP 429). The agent must backoff and retry "
                "successfully without abandoning the transaction."
            ),
            target_tool="payment_gateway",
            target_action="charge",
            faults=[
                FaultConfig(
                    type=FaultType.HTTP_429_THEN_SUCCESS,
                    target_tool="payment_gateway",
                    target_action="charge",
                    failure_count=2,
                    retry_after_seconds=0.5,
                )
            ],
            task={"amount": 1200, "currency": "USD", "customer_id": "cust_carol_88"},
            assertions=ScenarioAssertions(
                expected_side_effects=1,
                max_duplicate_effects=0,
                require_stable_idempotency_key=True,
                max_allowed_retries=4,
                expect_agent_success=True,
            ),
        )
        self._scenarios[rate_limit.scenario_id] = rate_limit

        # Scenario 4: Duplicate Delivery
        duplicate_del = ScenarioModel(
            scenario_id="payment_duplicate_delivery",
            name="Payment Duplicate Request Delivery",
            description=(
                "Simulates at-least-once queue or proxy network redelivery of identical request packets. "
                "The agent's stable idempotency key must protect the simulated store from duplicating commits."
            ),
            target_tool="payment_gateway",
            target_action="charge",
            faults=[
                FaultConfig(
                    type=FaultType.DUPLICATE_DELIVERY,
                    target_tool="payment_gateway",
                    target_action="charge",
                    failure_count=1,
                    duplicate_count=1,
                )
            ],
            task={"amount": 7500, "currency": "USD", "customer_id": "cust_dave_101"},
            assertions=ScenarioAssertions(
                expected_side_effects=1,
                max_duplicate_effects=0,
                require_stable_idempotency_key=True,
                max_allowed_retries=3,
                expect_agent_success=True,
            ),
        )
        self._scenarios[duplicate_del.scenario_id] = duplicate_del

        # Scenario 5: HTTP 500 Transient Server Error
        http_500 = ScenarioModel(
            scenario_id="payment_http_500_transient",
            name="Payment HTTP 500 Transient Failure",
            description=(
                "Simulates transient 500 internal server error. The agent must retry with backoff "
                "and complete the transaction once the service recovers."
            ),
            target_tool="payment_gateway",
            target_action="charge",
            faults=[
                FaultConfig(
                    type=FaultType.HTTP_500_TRANSIENT,
                    target_tool="payment_gateway",
                    target_action="charge",
                    failure_count=2,
                )
            ],
            task={"amount": 4500, "currency": "USD", "customer_id": "cust_eve_202"},
            assertions=ScenarioAssertions(
                expected_side_effects=1,
                max_duplicate_effects=0,
                require_stable_idempotency_key=True,
                max_allowed_retries=4,
                expect_agent_success=True,
            ),
        )
        self._scenarios[http_500.scenario_id] = http_500

        # Scenario 6: Malformed Tool Response
        malformed = ScenarioModel(
            scenario_id="payment_malformed_response",
            name="Payment Malformed Tool Response",
            description=(
                "Simulates a tool returning corrupt or malformed output payload. "
                "The agent must validate the response and recover safely with idempotency."
            ),
            target_tool="payment_gateway",
            target_action="charge",
            faults=[
                FaultConfig(
                    type=FaultType.MALFORMED_TOOL_RESPONSE,
                    target_tool="payment_gateway",
                    target_action="charge",
                    failure_count=1,
                    malformed_mode="corrupt_payload",
                )
            ],
            task={"amount": 8900, "currency": "USD", "customer_id": "cust_frank_303"},
            assertions=ScenarioAssertions(
                expected_side_effects=1,
                max_duplicate_effects=0,
                require_stable_idempotency_key=True,
                max_allowed_retries=3,
                expect_agent_success=True,
            ),
        )
        self._scenarios[malformed.scenario_id] = malformed

    def load_user_scenarios(self) -> None:
        """Scan scenarios_dir and load all .yaml and .yml files."""
        if not self.scenarios_dir.exists():
            return

        for path in self.scenarios_dir.glob("*.y*ml"):
            try:
                scenario = ScenarioValidator.validate_yaml_file(path)
                self._scenarios[scenario.scenario_id] = scenario
            except Exception as e:
                # Log or bubble up error
                raise ConfigurationError(f"Error loading scenario from {path}: {e}") from e

    def get(self, scenario_id: str) -> ScenarioModel | None:
        """Retrieve a scenario by its ID, loading user directory first if needed."""
        if scenario_id not in self._scenarios:
            self.load_user_scenarios()
        return self._scenarios.get(scenario_id)

    def list_all(self) -> list[ScenarioModel]:
        """Return all available scenarios."""
        self.load_user_scenarios()
        return list(self._scenarios.values())
