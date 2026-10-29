"""购物车。"""
from pricing import apply_discount


class Cart:
    def __init__(self):
        self.items = []          # [(商品名, 单价, 折扣率)]

    def add(self, name, price, discount_rate=0):
        self.items.append((name, price, discount_rate))

    def total(self):
        return round(sum(apply_discount(price, rate) for _, price, rate in self.items), 2)
