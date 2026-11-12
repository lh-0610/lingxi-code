"""读图与非文本读取（read_image / read_file 的 binary·pdf·ipynb 分支）的回归。

分四层：
- read_image：成功识别、question 透传、base64 正确、没配视觉模型 / 识别出错 / 空结果的明确文案、
  非图片扩展名 / 缺失 / 空文件 / 超大拒绝、子 Agent 沙箱；
- read_file 二进制：xlsx/zip/exe 给清晰说明而不是乱码，图片指向 read_image，纯文本行为不变；
- read_file 结构化：ipynb（markdown/code/stream/error/display_data、坏 JSON、分页）、
  pdf（多页、无文字页、加密、提取失败、没装 pypdf）；
- 闸门：read_image 进 Plan 只读 / 可并行 / 不建运行记录，遥控只读拦得住，explore 子代理不给它。

read_image 的视觉桥接一律打桩（models.describe_images_with_vision），不碰真实模型 / 网络。
"""
import base64
import json
import sys

import pytest

from src import config, models, permission_modes as pm, session, state, tools, tools_image

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-raster-bytes"


@pytest.fixture(autouse=True)
def _no_real_models(monkeypatch):
    from src import agent, claude_code

    def _forbidden(*a, **k):
        raise AssertionError("测试不许调用真实模型 / Claude CLI / 网络")

    monkeypatch.setattr(models, "_create_llm", _forbidden)
    monkeypatch.setattr(agent, "_claude_code_loop", _forbidden)
    monkeypatch.setattr(claude_code, "claude_code_loop", _forbidden)
    import socket
    monkeypatch.setattr(socket.socket, "connect", _forbidden)


@pytest.fixture()
def ws(project_dir):
    return project_dir


def _stub_vision(monkeypatch, desc="图里有一行报错：NullPointerException at Foo.java:42",
                 name="TestVL", idx=0):
    """打桩视觉桥接，记录收到的 (question, images)。"""
    seen = {}

    def _fake(user_text, images):
        seen["question"] = user_text
        seen["images"] = images
        return name, desc

    monkeypatch.setattr(models, "get_vision_model_index", lambda: idx)
    monkeypatch.setattr(models, "describe_images_with_vision", _fake)
    return seen


# ══════════════════════════════════════════════════════════════
# read_image
# ══════════════════════════════════════════════════════════════

def test_read_image_returns_vision_description(ws, monkeypatch):
    (ws / "shot.png").write_bytes(PNG)
    seen = _stub_vision(monkeypatch)
    out = tools.read_image.func("shot.png")
    assert out.startswith("[图片识别结果 · TestVL · shot.png]"), out
    assert "NullPointerException" in out
    assert len(seen["images"]) == 1
    path, b64 = seen["images"][0]
    assert path.endswith("shot.png")
    assert base64.b64decode(b64) == PNG, "交去的必须是文件原始字节的 base64"


def test_read_image_passes_question_through(ws, monkeypatch):
    (ws / "shot.png").write_bytes(PNG)
    seen = _stub_vision(monkeypatch)
    tools.read_image.func("shot.png", question="读出完整报错和行号")
    assert seen["question"] == "读出完整报错和行号"


def test_read_image_without_vision_model_points_to_settings(ws, monkeypatch):
    (ws / "shot.png").write_bytes(PNG)
    monkeypatch.setattr(models, "get_vision_model_index", lambda: -1)
    out = tools.read_image.func("shot.png")
    assert "图片识别模型" in out and "设置" in out


def test_read_image_bridge_error_is_reported(ws, monkeypatch):
    (ws / "shot.png").write_bytes(PNG)
    _stub_vision(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("视觉服务 503")

    monkeypatch.setattr(models, "describe_images_with_vision", _boom)
    out = tools.read_image.func("shot.png")
    assert "读图失败" in out and "503" in out


def test_read_image_empty_description(ws, monkeypatch):
    (ws / "shot.png").write_bytes(PNG)
    _stub_vision(monkeypatch, desc="")
    out = tools.read_image.func("shot.png")
    assert "没返回可识别的内容" in out


def test_read_image_rejects_non_image_extension(ws, monkeypatch):
    (ws / "notes.txt").write_text("hello", encoding="utf-8")
    _stub_vision(monkeypatch)
    out = tools.read_image.func("notes.txt")
    assert "不是支持的图片格式" in out and "read_file" in out


def test_read_image_rejects_svg_as_text(ws, monkeypatch):
    (ws / "icon.svg").write_text("<svg></svg>", encoding="utf-8")
    _stub_vision(monkeypatch)
    out = tools.read_image.func("icon.svg")
    assert "read_file" in out, "SVG 是文本，应指向 read_file 而不是当栅格图识别"


def test_read_image_missing_file(ws, monkeypatch):
    _stub_vision(monkeypatch)
    out = tools.read_image.func("nope.png")
    assert "不是文件或不存在" in out


def test_read_image_empty_file(ws, monkeypatch):
    (ws / "empty.png").write_bytes(b"")
    _stub_vision(monkeypatch)
    out = tools.read_image.func("empty.png")
    assert "空文件" in out


def test_read_image_oversized_file(ws, monkeypatch):
    (ws / "big.png").write_bytes(PNG)
    _stub_vision(monkeypatch)
    monkeypatch.setattr(tools_image, "_MAX_IMAGE_BYTES", 4)
    out = tools.read_image.func("big.png")
    assert "太大" in out


def test_read_image_subagent_cannot_leave_worktree(ws, monkeypatch):
    _stub_vision(monkeypatch)
    wt = ws / "wt"
    wt.mkdir()
    outside = ws / "outside.png"
    outside.write_bytes(PNG)
    sess = session.get_active()
    sess.is_subagent = True
    sess.worktree = str(wt)
    out = tools.read_image.func(str(outside))
    assert "只能访问自己的 worktree" in out


# ══════════════════════════════════════════════════════════════
# read_file：二进制与图片指针
# ══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name,head,kind", [
    ("book.xlsx", b"PK\x03\x04\x00\x00", "Excel"),
    ("pack.zip", b"PK\x03\x04\x00\x00", "压缩包"),
    ("app.exe", b"MZ\x00\x00", "可执行文件"),
    ("blob.dat", b"\x00\x01\x02\x00", "二进制文件"),
])
def test_read_file_binary_gives_clear_note(ws, name, head, kind):
    (ws / name).write_bytes(head + b"rest-of-binary")
    out = tools.read_file.func(name)
    assert kind in out and "read_file 只读文本" in out
    assert "\ufffd" not in out, "不该出现替换字符乱码"


def test_read_file_image_points_to_read_image(ws):
    (ws / "pic.png").write_bytes(PNG)
    out = tools.read_file.func("pic.png")
    assert "read_image" in out and "图片文件" in out


def test_read_file_plain_text_unchanged(ws):
    (ws / "a.txt").write_text("hello\nworld\n", encoding="utf-8")
    out = tools.read_file.func("a.txt")
    assert "1: hello" in out and "2: world" in out
    assert "[完整文件，共 2 行]" in out


def test_read_files_batch_dispatches_binary_and_notebook(ws):
    (ws / "book.xlsx").write_bytes(b"PK\x03\x04\x00\x00")
    (ws / "n.ipynb").write_text(json.dumps(
        {"cells": [{"cell_type": "code", "source": ["print(1)"]}]}), encoding="utf-8")
    out = tools.read_files.func(["book.xlsx", "n.ipynb"])
    assert "Excel" in out and "Cell 1 [code]" in out


# ══════════════════════════════════════════════════════════════
# read_file：notebook
# ══════════════════════════════════════════════════════════════

def _nb(cells):
    return json.dumps({"cells": cells}, ensure_ascii=False)


def test_read_ipynb_markdown_code_and_stream(ws):
    (ws / "n.ipynb").write_text(_nb([
        {"cell_type": "markdown", "source": ["# 标题\n", "第二行"]},
        {"cell_type": "code", "source": ["print(1)"],
         "outputs": [{"output_type": "stream", "text": ["1\n"]}]},
    ]), encoding="utf-8")
    out = tools.read_file.func("n.ipynb")
    assert "Cell 1 [markdown]" in out and "# 标题" in out
    assert "Cell 2 [code]" in out and "print(1)" in out
    assert "输出 [stream]" in out


def test_read_ipynb_error_output(ws):
    (ws / "n.ipynb").write_text(_nb([
        {"cell_type": "code", "source": ["1/0"],
         "outputs": [{"output_type": "error", "ename": "ZeroDivisionError",
                      "evalue": "division by zero", "traceback": ["line1", "line2"]}]},
    ]), encoding="utf-8")
    out = tools.read_file.func("n.ipynb")
    assert "ZeroDivisionError: division by zero" in out and "line1" in out


def test_read_ipynb_display_data_text_plain(ws):
    (ws / "n.ipynb").write_text(_nb([
        {"cell_type": "code", "source": ["df"],
         "outputs": [{"output_type": "display_data",
                      "data": {"text/plain": ["   col\n0    1"]}}]},
    ]), encoding="utf-8")
    out = tools.read_file.func("n.ipynb")
    assert "col" in out and "0    1" in out


def test_read_ipynb_invalid_json(ws):
    (ws / "bad.ipynb").write_text("{not json", encoding="utf-8")
    out = tools.read_file.func("bad.ipynb")
    assert "读 notebook 失败" in out


def test_read_ipynb_missing_cells(ws):
    (ws / "bad.ipynb").write_text(json.dumps({"metadata": {}}), encoding="utf-8")
    out = tools.read_file.func("bad.ipynb")
    assert "不是有效的 .ipynb" in out


def test_read_ipynb_empty(ws):
    (ws / "empty.ipynb").write_text(_nb([]), encoding="utf-8")
    out = tools.read_file.func("empty.ipynb")
    assert "没有 cell" in out


def test_read_ipynb_pagination(ws):
    cells = [{"cell_type": "code", "source": [f"x{i} = {i}"]} for i in range(30)]
    (ws / "n.ipynb").write_text(_nb(cells), encoding="utf-8")
    out = tools.read_file.func("n.ipynb", offset=1, limit=5)
    assert "还有" in out and "offset=" in out, "notebook 也走按行分页"


# ══════════════════════════════════════════════════════════════
# read_file：PDF
# ══════════════════════════════════════════════════════════════

class _FakePage:
    def __init__(self, text, boom=False):
        self._t, self._boom = text, boom

    def extract_text(self):
        if self._boom:
            raise RuntimeError("提取炸了")
        return self._t


def _patch_reader(monkeypatch, pages, encrypted=False, decrypt_ok=True):
    import pypdf

    class _Reader:
        def __init__(self, path):
            self.is_encrypted = encrypted
            self.pages = pages

        def decrypt(self, pw):
            if not decrypt_ok:
                raise RuntimeError("密码不对")
            return 1

    monkeypatch.setattr(pypdf, "PdfReader", _Reader)


def test_read_pdf_multipage(ws, monkeypatch):
    pytest.importorskip("pypdf")
    (ws / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    _patch_reader(monkeypatch, [_FakePage("第一页文字"), _FakePage("第二页文字")])
    out = tools.read_file.func("doc.pdf")
    assert "第 1 页" in out and "第一页文字" in out
    assert "第 2 页" in out and "第二页文字" in out


def test_read_pdf_no_text_page(ws, monkeypatch):
    pytest.importorskip("pypdf")
    (ws / "scan.pdf").write_bytes(b"%PDF-1.4 fake")
    _patch_reader(monkeypatch, [_FakePage("")])
    out = tools.read_file.func("scan.pdf")
    assert "未提取到文字" in out


def test_read_pdf_extract_failure_is_per_page(ws, monkeypatch):
    pytest.importorskip("pypdf")
    (ws / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    _patch_reader(monkeypatch, [_FakePage("", boom=True), _FakePage("好的页")])
    out = tools.read_file.func("doc.pdf")
    assert "本页提取失败" in out and "好的页" in out


def test_read_pdf_encrypted(ws, monkeypatch):
    pytest.importorskip("pypdf")
    (ws / "enc.pdf").write_bytes(b"%PDF-1.4 fake")
    _patch_reader(monkeypatch, [_FakePage("x")], encrypted=True, decrypt_ok=False)
    out = tools.read_file.func("enc.pdf")
    assert "已加密" in out


def test_read_pdf_without_pypdf(ws, monkeypatch):
    (ws / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setitem(sys.modules, "pypdf", None)
    out = tools.read_file.func("doc.pdf")
    assert "pypdf" in out and "pip install" in out


def test_read_pdf_real_file(ws):
    """真 PDF 集成：用 pymupdf/fitz 生成（没装就跳过），pypdf 提取。"""
    pytest.importorskip("pypdf")
    try:
        import pymupdf as fitz
    except ImportError:
        try:
            import fitz
        except ImportError:
            pytest.skip("需要 pymupdf/fitz 生成测试 PDF")
    pdf = ws / "real.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello PDF page one")
    doc.save(str(pdf))
    doc.close()
    out = tools.read_file.func("real.pdf")
    assert "第 1 页" in out and "Hello PDF page one" in out


# ══════════════════════════════════════════════════════════════
# 闸门
# ══════════════════════════════════════════════════════════════

class _UI:
    def __init__(self):
        self.messages = []

    def show_message(self, text, tag=""):
        self.messages.append((tag, str(text)))


@pytest.fixture()
def sess(isolated_memory, project_dir):
    s = session.get_active()
    session.bind_thread(s)
    yield s
    session.unbind_thread()
    state.ui_ref = None


def _drive(name, args):
    from src import streaming
    ui = _UI()
    state.ui_ref = ui
    s = session.current_session()
    tc = {"name": name, "args": args, "id": "call-img-1"}
    streaming._execute_tool(tc, ui)
    msg = s.chat_history[-1]
    assert msg.type == "tool"
    return msg.content, ui


def test_read_image_in_readonly_gate_lists():
    from src import run_records, streaming
    assert "read_image" in streaming.PLAN_MODE_READONLY_TOOLS
    assert "read_image" in streaming.PARALLEL_SAFE_TOOLS
    assert "read_image" in run_records.SIDE_EFFECT_FREE_TOOLS
    assert not run_records.needs_record("read_image")


def test_read_image_not_given_to_remote_or_explore():
    from src import streaming
    from src.tools_explore import EXPLORE_TOOL_NAMES
    assert "read_image" not in EXPLORE_TOOL_NAMES, "explore 子代理不给网络工具"
    # 遥控只读白名单在 _execute_tool 内联，这里核对它确实不含 read_image
    import inspect
    src_text = inspect.getsource(streaming._execute_tool)
    assert '"read_image"' not in src_text.split("_RO_ALLOWED = {")[1].split("}")[0]


def test_plan_mode_allows_read_image(sess, project_dir, monkeypatch):
    (project_dir / "pic.png").write_bytes(PNG)
    _stub_vision(monkeypatch)
    pm.apply(sess, pm.PLAN)
    out, _ = _drive("read_image", {"path": "pic.png"})
    assert "图片识别结果" in out, "Plan 模式应放行只读读图"


def test_remote_readonly_blocks_read_image(sess, project_dir, monkeypatch):
    (project_dir / "pic.png").write_bytes(PNG)
    _stub_vision(monkeypatch)
    monkeypatch.setattr(state, "remote_session", True)
    monkeypatch.setattr(config, "REMOTE_MODE", "safe_readonly")
    out, _ = _drive("read_image", {"path": "pic.png"})
    assert "只读模式" in out, "网络外发默认不给远程"


def test_describe_tool_call_read_image():
    from src import streaming
    name, detail = streaming.describe_tool_call("read_image", {"path": "a.png", "question": "读报错"})
    assert name == "🖼 读取图片"
    assert "a.png" in detail and "读报错" in detail