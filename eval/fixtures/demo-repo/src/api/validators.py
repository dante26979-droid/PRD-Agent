def validate_order_payload(payload: dict) -> None:
    if payload.get("amount", 0) <= 0:
        raise ValueError("amount must be positive")
    if payload.get("currency") not in {"CNY", "USD"}:
        raise ValueError("unsupported currency")
