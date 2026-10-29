"""008：apply_discount 已改名为 discounted_price，项目里一处旧名都不剩，新名字能用。"""
import os
import pathlib
import sys

sys.path.insert(0, os.getcwd())
left = [p.name for p in pathlib.Path(".").glob("*.py") if "apply_discount" in p.read_text(encoding="utf-8")]
assert not left, f"还有旧名字: {left}"
import pricing  # noqa: E402
assert pricing.discounted_price(100, 0.2) == 80
from cart import Cart  # noqa: E402
c = Cart()
c.add("book", 50, discount_rate=0.2)
assert c.total() == 40
print("ok")
