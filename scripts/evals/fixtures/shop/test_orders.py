from orders import create_order


def test_small_order_has_no_reduction():
    assert create_order([("pen", 30)])["total"] == 30
