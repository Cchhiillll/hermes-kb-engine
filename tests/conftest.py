"""测试公共夹具：每个测试一个独立的假 HOME，模块每次重新 import（模块级常量在 import 时读配置）。
只用合成数据，不连真模型、不连 QMD。"""
import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for sub in ("scripts", "kb"):
    p = os.path.join(REPO, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

PREFIXES = ("KB_", "OPENAI_", "MAC_", "YUQUE_", "GEMINI_")
EXACT = ("RAW_DIR", "WIKI_DIR", "HERMES_HOME", "LOG_PATH", "LOG_FILE", "QMD_BIN", "CONSOLIDATE_REC", "CONSOLIDATE_PLAN")


class Env:
    def __init__(self, home, tmp, monkeypatch):
        self.home, self.tmp, self.mp = home, tmp, monkeypatch
        self.brain = home / "brain"
        self.wiki = self.brain / "wiki"
        self.db = self.brain / "kb" / "kb.sqlite"

    def load(self, name):
        """重新 import 一个模块（连同它依赖的配置模块），让新的环境变量生效。"""
        for m in list(sys.modules):
            if m in _OWN_MODULES:
                sys.modules.pop(m, None)
        return importlib.import_module(name)

    def load_path(self, name, rel):
        spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel))
        mod = importlib.util.module_from_spec(spec)
        sys.modules.pop(name, None)
        spec.loader.exec_module(mod)
        return mod

    def write(self, rel, text, base=None):
        p = (base or self.home) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p


_OWN_MODULES = set()
for sub in ("scripts", "kb"):
    for f in os.listdir(os.path.join(REPO, sub)):
        if f.endswith(".py"):
            _OWN_MODULES.add(f[:-3])


@pytest.fixture
def kbenv(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for k in list(os.environ):
        if k.startswith(PREFIXES) or k in EXACT:
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KB_CONFIG", str(tmp_path / "no-config.toml"))
    env = Env(home, tmp_path, monkeypatch)
    (env.wiki).mkdir(parents=True)
    (env.wiki / "log.md").write_text("# log\n", encoding="utf-8")
    yield env
    for m in list(sys.modules):
        if m in _OWN_MODULES:
            sys.modules.pop(m, None)


# ---------- 合成数据生成 ----------
def claude_jsonl(path, turns, cwd="/tmp/x", ts="2026-10-08T01:00:00Z"):
    """turns: [(user, reply), ...]"""
    lines = []
    for u, r in turns:
        lines.append({"type": "user", "timestamp": ts, "cwd": cwd, "message": {"content": u}})
        lines.append({"type": "assistant", "timestamp": ts, "message": {"content": [{"type": "text", "text": r}]}})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n", encoding="utf-8")
    return path


def hermes_db(path, sessions):
    """sessions: {sid: (source, [(role, content), ...])}"""
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("create table sessions(id text primary key, source text, last_activity_at real, ended_at real)")
    c.execute("create table messages(id integer primary key autoincrement, session_id text, role text, content text, tool_calls text, timestamp real)")
    now = time.time()
    for sid, (source, msgs) in sessions.items():
        c.execute("insert into sessions values(?,?,?,?)", (sid, source, now, None))
        for role, content in msgs:
            c.execute("insert into messages(session_id, role, content, tool_calls, timestamp) values(?,?,?,?,?)",
                      (sid, role, content, None, now))
    c.commit(); c.close()
    return path


LONG = "这是一段足够长的回复内容，用来触发五百字闸门。" * 30
