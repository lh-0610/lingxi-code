"""010：满 100 减 10 生效，没满的不减。"""
import os
import sys

sys.path.insert(0, os.getcwd())
from orders import create_order  # noqa: E402

assert create_order([("book", 120)])["total"] == 110, create_order([("book", 120)])
assert create_order([("book", 100)])["total"] == 90
assert create_order([("pen", 30)])["total"] == 30
print("ok")
