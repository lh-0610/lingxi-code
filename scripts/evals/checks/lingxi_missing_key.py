"""评测 011 的隐藏检查：key 全空时灵犀能启动，发消息给出"去设置里填 key"，而不是崩溃。

在结果目录（灵犀源码）里跑：临时写一份 key 全空的 config.json（原来有就先挪开、跑完放回），
子进程里 import src.agent 再对每个 DeepSeek 条目发一条消息。两种情况都要过：
  1. 默认模型是 DeepSeek；
  2. 默认模型找不到、回落到列表第一个（Claude Code）。
不联网：key 是空的，请求根本发不出去。
"""
import json
import os
import shutil
import subprocess
import sys

CHILD = r'''
import os, sys
sys.path.insert(0, os.getcwd())
import src.mcp_client as m; m.init_mcp = lambda: []
from langchain_core.messages import HumanMessage, SystemMessage
from src import agent, session
from src.subagent import HeadlessUI
from src.models import MODEL_LIST
print("IMPORT_OK")
for idx in [i for i, e in enumerate(MODEL_LIST) if e[1] in ("deepseek", "custom")]:
    s = session.Session(); s.agent_mode = "act"; s.project = os.getcwd(); s.current_model_index = idx
    s.is_subagent = True; s.worktree = os.getcwd()
    session.bind_thread(s)
    s.chat_history = [SystemMessage(content="x"), HumanMessage(content="你好")]
    ui = HeadlessUI()
    try:
        r = agent.agent_loop(ui)
        print("SEND", idx, getattr(r, "status", r), "|", ui.text().replace("\n", " ")[-200:])
    except Exception as e:
        print("SEND", idx, "EXCEPTION", type(e).__name__, str(e)[:200])
'''

BASE = {
    "custom_models": [{"name": "deepseek-v4.1-flash", "model_id": "deepseek-flash", "protocol": "responses",
                       "api_key": "", "base_url": "https://api.deepseek.com", "supports_thinking": True}],
    "deepseek_api_key": "", "deepseek_models": ["deepseek-flash"],
    "qwen_api_key": "", "anthropic_api_key": "", "google_api_key": "", "mimo_api_key": "",
    "responses_api_key": "", "web_search_api_key": "",
    "mimo_models": [], "anthropic_models": [], "qwen_cloud_models": [], "gemini_models": [],
    "ollama_models": [], "responses_models": [],
    "default_model_id": "deepseek-flash", "mcp_enabled": False, "mcp_servers": {},
    "notify": {"enabled": False}, "remote_control": {"enabled": False},
}


def run_variant(label, cfg):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    env = {**os.environ, "LINGXI_LOG_DIR": os.path.join(os.environ.get("TEMP", "."), "lx_eval_check_logs")}
    r = subprocess.run([sys.executable, "-X", "utf8", "-W", "ignore", "-c", CHILD], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=240, env=env,
                       stdin=subprocess.DEVNULL)
    lines = r.stdout.splitlines()
    sends = [ln for ln in lines if ln.startswith("SEND")]
    ok = (r.returncode == 0 and "IMPORT_OK" in lines and len(sends) >= 2
          and all(" failed |" in ln and "设置" in ln for ln in sends))
    print(f"{'✅' if ok else '❌'} {label}：退出码 {r.returncode}")
    for ln in sends or (r.stdout + r.stderr).strip().splitlines()[-4:]:
        print("   ", ln[:240])
    return ok


backup = None
if os.path.exists("config.json"):
    backup = "config.json.eval-backup"
    shutil.move("config.json", backup)
try:
    results = [run_variant("默认模型 DeepSeek，key 全空", BASE),
               run_variant("默认模型找不到，回落到列表第一个，key 全空", {**BASE, "default_model_id": "不存在的模型"})]
finally:
    if os.path.exists("config.json"):
        os.remove("config.json")
    if backup:
        shutil.move(backup, "config.json")
sys.exit(0 if all(results) else 1)
