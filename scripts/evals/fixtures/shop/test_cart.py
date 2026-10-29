from cart import Cart


def test_total_with_discount():
    cart = Cart()
    cart.add("pen", 10)
    cart.add("book", 50, discount_rate=0.2)
    assert cart.total() == 50


def test_empty_cart():
    assert Cart().total() == 0
