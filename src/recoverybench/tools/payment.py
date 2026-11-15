"""Mock payment service simulating payment processing and idempotency."""

from typing import Any

from recoverybench.tools.base import ToolResult, ToolService


class MockPaymentService(ToolService):
    """Simulated payment gateway with stateful idempotency tracking."""

    name: str = "payment_gateway"

    def __init__(self) -> None:
        self.charges: list[dict[str, Any]] = []
        self.idempotency_map: dict[str, dict[str, Any]] = {}
        self._charge_counter: int = 1

    def reset(self) -> None:
        """Clear all transactions and idempotency state."""
        self.charges.clear()
        self.idempotency_map.clear()
        self._charge_counter = 1

    def execute(
        self,
        action: str,
        params: dict[str, Any],
        idempotency_key: str | None = None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        """Execute a payment action."""
        if action == "charge":
            return self._handle_charge(params, idempotency_key, operation_id, attempt_id)
        elif action == "get_charge":
            return self._handle_get_charge(params)
        elif action == "refund":
            return self._handle_refund(params, operation_id, attempt_id)
        else:
            raise ValueError(f"Unsupported payment action: {action}")

    def _handle_charge(
        self,
        params: dict[str, Any],
        idempotency_key: str | None,
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        amount = params.get("amount", 0)
        currency = params.get("currency", "USD")
        customer_id = params.get("customer_id", "cust_unknown")

        # 1. Idempotency Check: if key was already used, return previous charge without duplicating effect
        if idempotency_key and idempotency_key in self.idempotency_map:
            cached_charge = self.idempotency_map[idempotency_key]
            return ToolResult(
                success=True,
                data={
                    "charge_id": cached_charge["charge_id"],
                    "status": "succeeded",
                    "amount": cached_charge["amount"],
                    "currency": cached_charge["currency"],
                    "idempotent_replay": True,
                },
                committed=False,  # Replay of prior commit, no new side-effect created
                evidence_reference=cached_charge["charge_id"],
                effect_id=cached_charge["effect_id"],
            )

        # 2. Fresh Charge Commit
        charge_id = f"ch_sim_{self._charge_counter:04d}"
        effect_id = f"eff_{charge_id}"
        self._charge_counter += 1

        record = {
            "charge_id": charge_id,
            "effect_id": effect_id,
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "amount": amount,
            "currency": currency,
            "customer_id": customer_id,
            "idempotency_key": idempotency_key,
            "status": "succeeded",
        }

        self.charges.append(record)
        if idempotency_key:
            self.idempotency_map[idempotency_key] = record

        return ToolResult(
            success=True,
            data={
                "charge_id": charge_id,
                "status": "succeeded",
                "amount": amount,
                "currency": currency,
                "idempotent_replay": False,
            },
            committed=True,  # Brand new side-effect committed to state
            evidence_reference=charge_id,
            effect_id=effect_id,
        )

    def _handle_get_charge(self, params: dict[str, Any]) -> ToolResult:
        charge_id = params.get("charge_id")
        for c in self.charges:
            if c["charge_id"] == charge_id:
                return ToolResult(
                    success=True,
                    data=c,
                    committed=False,
                    evidence_reference=charge_id,
                    effect_id=c.get("effect_id"),
                )
        return ToolResult(
            success=False,
            data={"error": f"Charge {charge_id} not found"},
            committed=False,
        )

    def _handle_refund(
        self,
        params: dict[str, Any],
        operation_id: str | None = None,
        attempt_id: str | None = None,
    ) -> ToolResult:
        charge_id = params.get("charge_id")
        amount = params.get("amount")
        refund_id = f"ref_sim_{charge_id}"
        effect_id = f"eff_{refund_id}"
        return ToolResult(
            success=True,
            data={"refund_id": refund_id, "charge_id": charge_id, "amount": amount},
            committed=True,
            evidence_reference=refund_id,
            effect_id=effect_id,
        )
