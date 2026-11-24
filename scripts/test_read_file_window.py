"""read_file / search_in_file 按需读（backlog：大文件全量 readlines）。

以前不管要几行都先 readlines() 把整个文件装进内存。现在只留要显示的那一段，后面的只数行数：
结果必须和原来逐字一样（行号、「共 N 行」、截断提示），内存不能随文件大小涨。
"""
import tracemalloc

import pytest

from src import tools


def _old_way(path, offset, limit, max_chars):
    with open(path, "r", encoding="utf-8") as f:
        return tools._paginate_lines(f.readlines(), offset, limit, max_chars)


CASES = {
    "末尾有换行": "a\nb\nc\n",
    "末尾没换行": "a\nb\nc",
    "Windows 换行": "a\r\nb\r\nc\r\n",
    "老 Mac 换行": "a\rb\rc",
    "空文件": "",
    "只有一个换行": "\n",
    "行尾一大串空格": "".join(f"第{i}行" + " " * 500 + "\n" for i in range(200)),
    "长行": "x" * 50_000 + "\nshort\n",
    "中文": "".join(f"中文第 {i} 行，内容\n" for i in range(3000)),
}


@pytest.mark.parametrize("name", list(CASES))
@pytest.mark.parametrize("offset,limit,max_chars", [
    (1, 2000, 30_000), (2, 1, 30_000), (3, 5, 30_000), (50, 20, 30_000),
    (1, 2000, 600), (10_000, 10, 30_000), (0, 0, 30_000),
])
def test_same_result_as_reading_everything(tmp_path, name, offset, limit, max_chars):
    path = tmp_path / "f.txt"
    path.write_bytes(CASES[name].encode("utf-8"))
    with open(path, "r", encoding="utf-8") as f:
        new = tools._paginate_lines(tools._read_window(f, offset, limit, max_chars), offset, limit, max_chars)
    assert new == _old_way(path, offset, limit, max_chars)


def test_big_file_is_not_loaded_into_memory(tmp_path):
    path = tmp_path / "big.log"
    line = "2026-11-24 10:00:00 [INFO] 一行普通的日志，带点中文和 English mixed content\n"
    with open(path, "w", encoding="utf-8") as f:
        for _ in range(800_000):
            f.write(line)
    size = path.stat().st_size
    assert size > 50_000_000
    tracemalloc.start()
    out = tools._read_one(str(path), 1, 50)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert "共 800000 行" in out and "还有 799950 行未读" in out
    # 只和数行数那一块缓冲有关，和文件多大无关（以前是整个文件 × 几倍）
    assert peak < 6_000_000, f"读前 50 行占了 {peak / 1e6:.1f}MB（文件 {size / 1e6:.0f}MB）"


def test_search_in_file_pages_without_keeping_every_match(tmp_path):
    path = tmp_path / "s.txt"
    path.write_text("".join(("hit" if i % 3 == 0 else "miss") + f" {i}\n" for i in range(30)),
                    encoding="utf-8")
    out = tools.search_in_file.func(str(path), "HIT", offset=2, limit=3)
    assert out.startswith("找到 10 处匹配") and "L7:" in out and "L13:" in out and "L16:" not in out
    assert "还有 5 处未列出；继续查看用 offset=5" in out
    assert "offset=12 已超出范围" in tools.search_in_file.func(str(path), "hit", offset=12)
