import json

from helpers import add_chunks


def _logic(kbenv):
    return kbenv.load_path("kb_recall_logic", "plugins/kb-recall/logic.py")


def test_route_history_adds_raw(kbenv, monkeypatch):
    lg = _logic(kbenv)
    assert lg.route("nginx 怎么配置反向代理") == ["wiki"]
    assert lg.route("上次我们聊过的 nginx 方案是什么") == ["wiki", "raw"]
    monkeypatch.setenv("KB_RECALL_RAW", "0")
    assert lg.route("上次我们聊过的 nginx 方案是什么") == ["wiki"]


def test_hit_log_written_and_rotated(kbenv, monkeypatch):
    kbenv.write("projects/nginx.md", "---\ntitle: Nginx\n---\n> 一句话：反向代理配置\n", base=kbenv.wiki)
    kbenv.write("tp-claude/2026-10-08_s1.md", "---\nsource: tp-claude\n---\n# x\n\n**他**：之前的 nginx 讨论\n", base=kbenv.brain / "raw/conversations")
    lg = _logic(kbenv)
    seen = {}

    def fake_query(text, cols):
        seen["cols"] = cols
        return [{"file": "wiki/projects/nginx.md", "score": 0.91234}, {"file": "raw/tp-claude/2026-10-08_s1.md", "score": 0.5}]
    monkeypatch.setattr(lg, "_query", fake_query)
    out = lg.recall("上次 nginx 反向代理是怎么配的", platform="feishu", session_id="S1")
    assert seen["cols"] == ["wiki", "raw"]
    assert "〔原始对话〕tp-claude/2026-10-08_s1.md" in out and "《Nginx》" in out
    log = kbenv.brain / "kb/recall_hits.jsonl"
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["platform"] == "feishu" and rec["session"] == "S1" and len(rec["msg_sha1"]) == 16
    assert rec["hits"][0] == {"file": "wiki/projects/nginx.md", "score": 0.912}
    lg.HITLOG_MAX = 10
    lg.recall("nginx 反向代理怎么配置的", platform="cli")
    assert (kbenv.brain / "kb/recall_hits.jsonl.1").exists()
    assert len(log.read_text(encoding="utf-8").splitlines()) == 1


def test_no_hits_no_log(kbenv, monkeypatch):
    lg = _logic(kbenv)
    monkeypatch.setattr(lg, "_query", lambda t, c: [])
    assert lg.recall("完全没有相关内容的问题", platform="cli") is None
    assert not (kbenv.brain / "kb/recall_hits.jsonl").exists()


def test_eval_metrics(kbenv):
    ev = kbenv.load("kb_eval")
    items = [{"q": "a", "expect": ["wiki/x.md"]}, {"q": "b", "expect": ["concepts/y.md"]}, {"q": "c", "expect": ["z"]}]
    results = {"a": ["wiki/x.md"], "b": ["wiki/1.md", "wiki/2.md", "wiki/concepts/y.md"], "c": ["q"] * 10}
    summary, rows = ev.evaluate(items, searcher=lambda q, cols, k: results[q])
    assert summary["pass@1"] == round(1 / 3, 4) and summary["pass@5"] == round(2 / 3, 4)
    assert summary["mrr"] == round((1 + 1 / 3) / 3, 4)
    assert [r["rank"] for r in rows] == [1, 3, None]


def test_eval_kb_backend_and_regression(kbenv, capsys, monkeypatch):
    kb = kbenv.load("kb")
    c = add_chunks(kb, [("tp-claude:s:0", "语雀增量同步怎么做", "用 yuque-exporter 导出"),
                        ("tp-claude:s:1", "nginx 反向代理", "proxy_pass 配置")])
    for i, (cid, user, reply) in enumerate([("tp-claude:s:0", "语雀增量同步怎么做", "用 yuque-exporter 导出"),
                                             ("tp-claude:s:1", "nginx 反向代理", "proxy_pass 配置")]):
        rid = c.execute("insert into fts_map(id) values(?)", (cid,)).lastrowid
        c.execute("insert into fts(rowid, tok) values(?, ?)", (rid, " ".join(kb.toks(user + " " + reply))))
    c.commit()
    golden = kbenv.tmp / "golden.jsonl"
    golden.write_text('{"q": "语雀 增量同步", "expect": ["tp-claude:s:0"]}\n{"q": "nginx 反向代理", "expect": ["tp-claude:s:1"]}\n', encoding="utf-8")
    ev = kbenv.load("kb_eval")
    assert ev.main(["--golden", str(golden), "--backend", "kb"]) == 0
    assert "pass@1 100%" in capsys.readouterr().out
    assert ev.latest_regressed() is False
    monkeypatch.setattr(ev, "search_kb", lambda q, cols, k: [])
    ev.BACKENDS["kb"] = ev.search_kb
    assert ev.main(["--golden", str(golden), "--backend", "kb"]) == 2
    assert "退步了" in capsys.readouterr().out
    assert ev.latest_regressed() is True
    hist = (kbenv.brain / "kb/eval/history.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(hist) == 2 and json.loads(hist[-1])["regressed"] is True


def test_eval_missing_golden(kbenv, capsys):
    ev = kbenv.load("kb_eval")
    assert ev.main([]) == 1
    assert "没有评测集" in capsys.readouterr().out


def test_eval_qmd_backend_uses_recall_logic(kbenv, monkeypatch):
    ev = kbenv.load("kb_eval")
    lg = _logic(kbenv)
    monkeypatch.setattr(lg, "_query", lambda q, cols: [{"file": "wiki/a.md"}, {"file": "yuque/b.md"}])
    monkeypatch.setattr(ev, "_recall_logic", lambda: lg)
    assert ev.search_qmd("q", ["wiki", "yuque"], 10) == ["wiki/a.md", "yuque/b.md"]


def test_example_golden_is_valid(kbenv):
    import os
    from conftest import REPO
    ev = kbenv.load("kb_eval")
    assert len(ev.load_golden(os.path.join(REPO, "eval/golden.example.jsonl"))) == 3
