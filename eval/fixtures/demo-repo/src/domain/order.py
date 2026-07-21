ORDER_STATES = ("pending", "paid", "cancelled")


def transition_order(state: str, event: str) -> str:
    transitions = {
        ("pending", "pay"): "paid",
        ("pending", "cancel"): "cancelled",
    }
    try:
        return transitions[(state, event)]
    except KeyError as exc:
        raise ValueError("invalid order transition") from exc
