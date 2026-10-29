"""下单。"""
from cart import Cart
from pricing import full_reduction


def create_order(lines):
    """lines: [(商品名, 单价)]，返回 {"items": 件数, "total": 应付金额}。"""
    cart = Cart()
    for name, price in lines:
        cart.add(name, price)
    return {"items": len(cart.items), "total": full_reduction(cart.total())}
