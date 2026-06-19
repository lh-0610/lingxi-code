# -*- mode: python ; coding: utf-8 -*-

import os
import shutil as _shutil

from PyInstaller.utils.hooks import collect_submodules, collect_data_files

# 随包发 ruff（完整 lint）：构建时定位系统 ruff，打进包根目录（运行时落在 _MEIPASS，
# tools._bundled_ruff() 会优先找它）。找不到就跳过——产物退化到内置 py_compile 语法检查，
# 不影响打包。要指定特定 ruff 用环境变量 RUFF_PATH。
_ruff_path = os.environ.get("RUFF_PATH") or _shutil.which("ruff")
if _ruff_path and os.path.isfile(_ruff_path):
    _ruff_datas = [(_ruff_path, '.')]
    print(f"[lingxi.spec] 随包打入 ruff: {_ruff_path}")
else:
    _ruff_datas = []
    print("[lingxi.spec] 未找到 ruff，跳过；产物将退化到 py_compile 语法检查。"
          "（pip install ruff 或设 RUFF_PATH 可启用完整 lint）")

# MCP 客户端（可选功能）：mcp SDK 的子模块大多是函数内懒导入
# （如 from mcp.client.sse import sse_client），PyInstaller 静态分析抓不到，
# 这里全量收集子模块；jsonschema_specifications 还带 JSON 数据文件要一起打进去。
# 没装 mcp 时 collect_* 返回空，不影响打包。
try:
    _mcp_hiddenimports = collect_submodules('mcp')
    _mcp_datas = collect_data_files('jsonschema_specifications')
except Exception:
    _mcp_hiddenimports = []
    _mcp_datas = []

# jedi 代码导航（可选）：PyInstaller 静态分析可能漏掉 jedi 的子模块
try:
    _jedi_hiddenimports = collect_submodules('jedi')
    _jedi_datas = collect_data_files('jedi')
except Exception:
    _jedi_hiddenimports = []
    _jedi_datas = []

# tree-sitter 多语言代码分析（可选）：PyInstaller 静态分析抓不到懒导入的语言模块；
# tree-sitter-javascript / tree-sitter-typescript 等带 .so / .dll 二进制 + 数据文件。
# 没装时 collect_* 返回空，不影响打包。
try:
    _ts_hiddenimports = collect_submodules('tree_sitter')
    _ts_datas = (collect_data_files('tree_sitter_python')
                 + collect_data_files('tree_sitter_javascript')
                 + collect_data_files('tree_sitter_typescript'))
except Exception:
    _ts_hiddenimports = []
    _ts_datas = []


a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('icon.ico', '.'),                  # 应用图标
        ('icons', 'icons'),                 # SVG 按钮图标（顶栏/设置/搜索等，走 BASE_DIR/_MEIPASS 读）
        ('roles', 'roles'),                 # 默认角色卡目录
        ('config.example.json', '.'),       # 配置模板，首次启动时复制成 config.json
    ] + _ruff_datas + _mcp_datas + _jedi_datas + _ts_datas,
    hiddenimports=[
        # LangChain 各 provider 包，PyInstaller 静态分析有时识别不到
        'langchain_anthropic',
        'langchain_openai',
        'langchain_ollama',
        'langchain_google_genai',
        'langchain_core',
        'markdown',
        # MCP 及其依赖（懒导入 + 第三方传输库，静态分析容易漏）
        'mcp',
        'mcp.client.sse',
        'mcp.client.stdio',
        'mcp.client.streamable_http',
        'mcp.client.session',
        'sse_starlette',
        'httpx_sse',
        'python_multipart',
        'jsonschema',
        'jsonschema_specifications',
        'referencing',
        'rpds',
    ] + _mcp_hiddenimports + _jedi_hiddenimports + _ts_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# onedir 模式（产物是 dist/灵犀Code/ 文件夹，里面有 灵犀Code.exe + _internal/）。
# 相比 onefile：启动**秒开**（不必每次把 175MB 解压到临时目录），代价是分发的是文件夹
# 而非单个 exe。打 zip 发布即可。EXE 只装引导器 + 脚本，二进制/数据交给 COLLECT。
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,        # onedir：二进制不塞进 exe，由 COLLECT 收进 _internal/
    name='灵犀Code',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='灵犀Code',
)
