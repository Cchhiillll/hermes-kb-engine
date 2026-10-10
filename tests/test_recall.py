import json
import os

from conftest import REPO


def _logic(kbenv, **env):
    for k, v in env.items():
        kbenv.mp.setenv(k, v)
    return kbenv.load_path("kb_recall_logic", "plugins/kb-recall/logic.py")


def test_platform_filter_and_short(kbenv):
    lg = _logic(kbenv)
    assert lg.recall("语雀怎么同步到知识库", platform="cron") is None
    assert lg.recall("hi", platform="cli") is None


def test_hits_formatted_and_status(kbenv, monkeypatch):
    kbenv.write("projects/yuque.md", "---\ntitle: 语雀同步\nupdated: 2026-10-01\n---\n> 一句话：用 yuque-exporter 增量导出 ^[kb:tp-claude:s:0]\n", base=kbenv.wiki)
    (kbenv.brain / "kb").mkdir(parents=True, exist_ok=True)
    (kbenv.brain / "kb/now.md").write_text("服务全部正常", encoding="utf-8")
    lg = _logic(kbenv)
    monkeypatch.setattr(lg, "_search", lambda t: [
        {"file": "wiki/projects/yuque.md", "score": 0.9},
        {"file": "wiki/index.md", "score": 0.9},
        {"file": "wiki/projects/low.md", "score": 0.1}])
    out = lg.recall("现在语雀同步状态怎么样", platform="cli")
    assert "服务全部正常" in out
    assert "《语雀同步》（projects/yuque.md，页面最后更新 2026-10-01）：用 yuque-exporter 增量导出" in out
    assert "index.md" not in out and "low.md" not in out


def test_search_errors_are_silent_and_collections_configurable(kbenv, monkeypatch):
    lg = _logic(kbenv, KB_RECALL_COLLECTIONS="wiki,raw", KB_MCP_URL="http://127.0.0.1:9/mcp")
    assert lg.COLLECTIONS == ["wiki", "raw"] and lg.MCP == "http://127.0.0.1:9/mcp"
    sent = []

    def fake_post(payload, sid=None):
        sent.append(payload)
        if payload.get("method") == "tools/call":
            return None, json.dumps({"result": {"structuredContent": {"results": []}}})
        return "sid1", ""
    monkeypatch.setattr(lg, "_post", fake_post)
    assert lg.recall("语雀怎么同步到知识库", platform="cli") is None
    assert sent[-1]["params"]["arguments"]["collections"] == ["wiki", "raw"]
    monkeypatch.setattr(lg, "_post", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert lg.recall("语雀怎么同步到知识库", platform="cli") is None
