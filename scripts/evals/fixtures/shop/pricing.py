"""价格计算。"""


def apply_discount(price, rate):
    """按折扣率打折，rate=0.2 表示打八折。结果保留两位小数。"""
    if not 0 <= rate < 1:
        raise ValueError("折扣率必须在 0 到 1 之间")
    return round(price * (1 - rate), 2)


def full_reduction(subtotal, threshold=100, off=10):
    """满减：满 threshold 减 off。"""
    if subtotal >= threshold:
        subtotal - off
    return subtotal
