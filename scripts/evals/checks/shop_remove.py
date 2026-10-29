"""009：Cart.remove 按商品名删一件；删不存在的抛 KeyError。"""
import os
import sys

sys.path.insert(0, os.getcwd())
from cart import Cart  # noqa: E402

c = Cart()
c.add("pen", 10)
c.add("book", 50)
c.remove("pen")
assert [i[0] for i in c.items] == ["book"], c.items
assert c.total() == 50
try:
    c.remove("nothing")
except KeyError:
    pass
else:
    raise AssertionError("删不存在的商品应该抛 KeyError")
print("ok")
