"""Small read-only demo API surface used by Eval Cases."""


def create_order(payload: dict) -> dict:
    """Create an order after validation and return a pending order."""

    return {"status": "pending", "amount": payload["amount"]}
