from src.domain.order import transition_order


def test_pending_order_can_be_paid():
    assert transition_order("pending", "pay") == "paid"
