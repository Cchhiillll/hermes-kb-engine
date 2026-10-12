import json
import os
import time

import pytest

from conftest import hermes_db
from helpers import add_chunks

NOW = time.mktime((2026, 10, 10, 12, 0, 0, 0, 0, -1))
DAY = 86400


def _add(text, src=("tp-claude:s1:0",), domain="nginx", basis="user", **kw):
    return dict(op="ADD", domain=domain, text=text, basis=basis, sources=list(src), **kw)


@pytest.fixture
def kl(kbenv):
    return kbenv.load("kb_lessons")


def test_add_requires_sources_and_text(kl):
    pb = kl.load()
    res = kl.apply_ops(pb, [_add("改完配置先 nginx -t 再 reload", src=[]), _add("短"), {"op": "FOO"},
                            _add("改完配置先 nginx -t 再 reload", src=["不是段id"])], NOW)
    assert [r[1] for r in res] == ["rejected"] * 4
    assert pb["lessons"] == {}


def test_add_dedupe_merges_sources_deterministically(kl):
    pb1, pb2 = kl.load(), kl.load()
    ops = [_add("改完 nginx 配置后先运行 nginx -t 再 reload", basis="agent"),
           _add("改完 nginx 配置后先运行 nginx -t 再 reload。", src=["tp-codex:s2:3"], basis="output")]
    r1, r2 = kl.apply_ops(pb1, ops, NOW), kl.apply_ops(pb2, ops, NOW)
    assert r1 == r2 and [x[1] for x in r1] == ["added", "merged"]
    l = pb1["lessons"][r1[0][2]]
    assert l["sources"] == ["tp-claude:s1:0", "tp-codex:s2:3"] and l["basis"] == ["agent", "output"]
    assert l["id"].startswith("L-") and len(l["id"]) == 10
    assert pb1 == pb2                                       # 同样的操作 → 同样的结果


def test_different_domain_not_merged(kl):
    pb = kl.load()
    res = kl.apply_ops(pb, [_add("部署前先备份数据库文件", domain="a"), _add("部署前先备份数据库文件", domain="b")], NOW)
    assert [r[1] for r in res] == ["added", "added"]


def test_conflict_flagged_and_blocks_promotion(kl):
    pb = kl.load()
    a = kl.apply_ops(pb, [_add("重启服务前通知用户确认窗口时间", confirmed=True)], NOW)[0][2]
    r = kl.apply_ops(pb, [_add("重启服务前不要通知用户确认窗口", confirmed=True)], NOW)[0]
    assert r[1] == "conflict"
    assert pb["lessons"][r[2]]["conflict_with"] == [a] and r[2] in pb["lessons"][a]["conflict_with"]
    assert set(kl.promote(pb, NOW, regressed=False)) == set()     # 两条都在冲突里，都不升级
    kl.apply_ops(pb, [{"op": "REMOVE", "id": r[2], "reason": "用户说要通知"}], NOW)
    assert kl.promote(pb, NOW, regressed=False) == [a]


def test_update_and_remove_rules(kl):
    pb = kl.load()
    lid = kl.apply_ops(pb, [_add("证书续期后检查 nginx 是否加载了新证书", confirmed=True)], NOW)[0][2]
    kl.promote(pb, NOW, regressed=False)
    assert pb["lessons"][lid]["status"] == "stable"
    r = kl.apply_ops(pb, [{"op": "UPDATE", "id": lid, "text": "x 改成 y 的说法", "basis": "agent", "sources": ["tp-claude:s9:1"]},
                          {"op": "UPDATE", "id": lid, "text": "证书续期后 reload nginx 并用 openssl 检查", "basis": "output"},
                          {"op": "UPDATE", "id": lid, "text": "证书续期后 reload nginx 并用 openssl 检查", "basis": "output", "sources": ["tp-claude:s9:1"]},
                          {"op": "REMOVE", "id": lid},
                          {"op": "REMOVE", "id": lid, "reason": "流程换了"},
                          {"op": "UPDATE", "id": lid, "sources": ["tp-claude:s9:2"]}], NOW)
    assert [x[1] for x in r] == ["rejected", "rejected", "updated", "rejected", "retired", "rejected"]
    l = pb["lessons"][lid]
    assert l["status"] == "retired" and l["retired_reason"] == "流程换了"
    assert any(h["op"] == "update" and "原说法" in h["note"] for h in l["history"])


def test_re_adding_retired_is_rejected(kl):
    pb = kl.load()
    lid = kl.apply_ops(pb, [_add("每次部署都清空缓存目录再启动")], NOW)[0][2]
    kl.apply_ops(pb, [{"op": "REMOVE", "id": lid, "reason": "错误做法"}], NOW)
    assert kl.apply_ops(pb, [_add("每次部署都清空缓存目录再启动")], NOW)[0][1] == "rejected"


def test_cap_retires_least_useful_draft(kl, monkeypatch):
    monkeypatch.setenv("KB_LESSONS_MAX", "2")
    pb = kl.load()
    first = kl.apply_ops(pb, [_add("第一条教训关于备份数据库", src=["tp-claude:s1:0"])], NOW)[0][2]
    pb["lessons"][first]["helpful"] = 3
    for i, t in enumerate(["第二条教训关于检查端口占用", "第三条教训关于日志轮换配置", "第四条教训关于证书过期提醒"]):
        kl.apply_ops(pb, [_add(t, src=[f"tp-claude:s{i + 2}:0"])], NOW + i)
    act = kl.active(pb, "nginx")
    assert len(act) == 2 and pb["lessons"][first]["status"] == "draft"     # 有人说有用的留下
    assert sum(1 for l in pb["lessons"].values() if l["status"] == "retired") == 2


def test_promotion_rules(kl):
    pb = kl.load()
    agent_only = kl.apply_ops(pb, [_add("agent 说重启能解决所有问题", basis="agent", src=["a-b:s1:0", "a-b:s2:0"])], NOW)[0][2]
    two_sess = kl.apply_ops(pb, [_add("迁移前先在测试库演练一遍", basis="output", src=["a-b:s1:0", "a-b:s2:0"])], NOW)[0][2]
    one_sess = kl.apply_ops(pb, [_add("看日志先用 journalctl -u 服务名", basis="user", src=["a-b:s1:0", "a-b:s1:1"])], NOW)[0][2]
    voted = kl.apply_ops(pb, [_add("配置改动前先 git commit 一次", basis="user")], NOW)[0][2]
    pb["lessons"][voted]["helpful"] = 2
    pb["lessons"][agent_only]["helpful"] = 5
    assert kl.promote(pb, NOW, regressed=True) == []           # 检索评测退步：暂停升级
    up = kl.promote(pb, NOW, regressed=False)
    assert set(up) == {two_sess, voted}
    assert pb["lessons"][agent_only]["status"] == "draft" and pb["lessons"][one_sess]["status"] == "draft"


def test_promote_reads_eval_regression(kbenv, kl):
    pb = kl.load()
    lid = kl.apply_ops(pb, [_add("确认过的教训内容足够长", confirmed=True)], NOW)[0][2]
    ev_dir = kbenv.brain / "kb/eval"
    ev_dir.mkdir(parents=True)
    (ev_dir / "history.jsonl").write_text(json.dumps({"regressed": True}) + "\n", encoding="utf-8")
    assert kl.promote(pb, NOW) == []
    (ev_dir / "history.jsonl").write_text(json.dumps({"regressed": False}) + "\n", encoding="utf-8")
    assert kl.promote(pb, NOW) == [lid]


def test_decay(kl):
    pb = kl.load()
    st = kl.apply_ops(pb, [_add("稳定的教训一开始是被确认的", confirmed=True)], NOW)[0][2]
    kl.promote(pb, NOW, regressed=False)
    bad = kl.apply_ops(pb, [_add("草稿教训被用户多次纠正过了")], NOW)[0][2]
    old = kl.apply_ops(pb, [_add("很久以前的草稿一直没人用到")], NOW - 100 * DAY)[0][2]
    kept = kl.apply_ops(pb, [_add("很久以前的草稿但最近被用到", src=["tp-claude:s7:0"])], NOW - 100 * DAY)[0][2]
    pb["lessons"][kept]["last_used"] = "2026-10-01"
    for i in (st, bad):
        pb["lessons"][i]["harmful"], pb["lessons"][i]["helpful"] = 2, 1
    res = dict(kl.decay(pb, NOW))
    assert res == {st: "demoted", bad: "retired", old: "retired"}
    assert pb["lessons"][st]["status"] == "draft" and not pb["lessons"][st]["confirmed"]
    assert pb["lessons"][kept]["status"] == "draft"


def test_render_pages(kbenv, kl):
    pb = kl.load()
    a = kl.apply_ops(pb, [_add("改完 nginx 配置先 nginx -t", confirmed=True)], NOW)[0][2]
    b = kl.apply_ops(pb, [_add("证书路径写绝对路径更稳妥", basis="agent", src=["tp-claude:s2:0", "tp-claude:s2:1"])], NOW)[0][2]
    c = kl.apply_ops(pb, [_add("通用的一条将被退役的教训", domain="通用")], NOW)[0][2]
    kl.apply_ops(pb, [{"op": "REMOVE", "id": c, "reason": "过时"}], NOW)
    kl.promote(pb, NOW, regressed=False)
    assert kl.render(pb, NOW) == 2
    page = (kbenv.wiki / "lessons/nginx.md").read_text(encoding="utf-8")
    st, dr = page.split("## 稳定")[1].split("## 草稿（未核实）")
    assert f"[{a}]" in st and f"[{b}]" in dr and "^[kb:tp-claude:s2:1]" in dr
    assert "👍0 👎0" in st
    assert f"~~通用的一条将被退役的教训~~（过时" in (kbenv.wiki / "lessons/通用.md").read_text(encoding="utf-8")
    kbenv.write("lessons/手写.md", "# 自己写的\n", base=kbenv.wiki)
    pb["lessons"] = {k: v for k, v in pb["lessons"].items() if v["domain"] == "nginx"}
    kl.render(pb, NOW)
    assert not (kbenv.wiki / "lessons/通用.md").exists() and (kbenv.wiki / "lessons/手写.md").exists()
    wf = kbenv.load("wiki_feed")
    assert "tp-claude:s2:1" in wf.cited_ids()                    # 教训页的出处也算「已有出处」


def test_feedback_from_hits_and_state_db(kbenv, kl):
    pb = kl.load()
    good = kl.apply_ops(pb, [_add("先 nginx -t 再 reload 配置")], NOW)[0][2]
    bad = kl.apply_ops(pb, [_add("直接 kill -9 掉 nginx 进程")], NOW)[0][2]
    t0 = NOW - 3600
    import sqlite3
    db = hermes_db(kbenv.home / ".hermes/state.db", {"S1": ("feishu", []), "S2": ("feishu", []), "S3": ("feishu", [])})
    c = sqlite3.connect(db)
    for sid, ts, text in [("S1", t0 + 5, "可以了，谢谢"), ("S2", t0 + 105, "不对，这样会丢连接")]:
        c.execute("insert into messages(session_id, role, content, timestamp) values(?, 'user', ?, ?)", (sid, text, ts))
    c.commit()
    log = kbenv.brain / "kb/recall_hits.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    recs = [{"ts": t0, "session": "S1", "lessons": [good]}, {"ts": t0 + 100, "session": "S2", "lessons": [bad]},
            {"ts": t0 + 200, "session": "", "lessons": [good]}, {"ts": t0 + 300, "session": "S3", "hits": []},
            {"ts": NOW - 60, "session": "S3", "lessons": [bad]}]
    log.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    n, up, down = kl.feedback(pb, NOW)
    assert (n, up, down) == (3, 1, 1)
    assert pb["lessons"][good]["helpful"] == 1 and pb["lessons"][bad]["harmful"] == 1
    assert pb["lessons"][good]["last_used"] == "2026-10-10"
    assert pb["cursor"] == t0 + 200                               # 最后一条还不到 10 分钟，下次再看
    assert kl.feedback(pb, NOW) == (0, 0, 0)                      # 不重复计数


def test_apply_or_queue_and_maintain(kbenv, kl):
    lock = kbenv.load("kb_lock")
    assert lock.acquire("merge", pid=str(os.getpid()))[0]
    kl = kbenv.load("kb_lessons")
    assert kl.apply_or_queue([_add("知识库忙的时候先排队的教训")]) is None
    assert (kbenv.brain / "kb/lessons_pending.jsonl").exists()
    assert kl.maintain()[0] is False
    lock.release("merge")
    ok, (res, fb, dc, up) = kl.maintain()
    assert ok and res[0][1] == "added"
    assert not (kbenv.brain / "kb/lessons_pending.jsonl").exists()
    assert kl.stats(kl.load()) == {"stable": 0, "draft": 1, "retired": 0}
    assert (kbenv.wiki / "lessons/nginx.md").exists()


def test_cli_apply(kbenv, kl, capsys):
    ops = kbenv.tmp / "ops.json"
    ops.write_text(json.dumps([_add("命令行应用的一条教训内容")]), encoding="utf-8")
    assert kl.main(["apply", str(ops)]) == 0
    assert "ADD added L-" in capsys.readouterr().out
    assert kl.main(["stats"]) == 0
    assert "草稿 1" in capsys.readouterr().out


def test_extract_saves_lessons(kbenv, monkeypatch):
    kbenv.mp.setenv("KB_MODEL", "m"); kbenv.mp.setenv("OPENAI_BASE_URL", "http://relay.invalid/v1")
    kb = kbenv.load("kb")
    add_chunks(kb, [("tp-claude:s:0", "不对，要先 nginx -t" * 5, "好的" * 30)])
    we = kbenv.load("wiki_extract")
    out = json.dumps({"items": [], "none": [], "lessons": [
        {"seg": ["tp-claude:s:0"], "domain": "nginx", "text": "改 nginx 配置后先 nginx -t 再 reload", "basis": "user", "correction": True},
        {"seg": "tp-claude:s:0", "domain": "nginx", "text": "agent 自己说的一条教训内容", "basis": "agent", "correction": True}]})
    monkeypatch.setattr(we.kb_llm, "chat", lambda *a, **k: (out, 1))
    monkeypatch.setattr("sys.argv", ["wiki_extract.py", "--max", "1"])
    we.main()
    pb = kbenv.load("kb_lessons").load()
    ls = sorted(pb["lessons"].values(), key=lambda l: l["text"])
    assert [l["confirmed"] for l in ls] == [False, True]          # 只有用户纠正得出的算确认
    assert {l["status"] for l in ls} == {"draft", "stable"}       # 确认的立即升级
    assert we.parse_notes('{"items": [], "lessons": "x"}')["lessons"] == []


def test_recall_injects_lessons_and_logs(kbenv, kl, monkeypatch):
    pb = kl.load()
    a = kl.apply_ops(pb, [_add("改 nginx 配置后先 nginx -t 再 reload", confirmed=True)], NOW)[0][2]
    kl.apply_ops(pb, [_add("证书续期之后检查有效期", src=["tp-claude:s3:0"])], NOW)
    kl.promote(pb, NOW, regressed=False); kl.render(pb, NOW)
    lg = kbenv.load_path("kb_recall_logic", "plugins/kb-recall/logic.py")
    monkeypatch.setattr(lg, "_query", lambda t, c: [{"file": "wiki/lessons/nginx.md", "score": 0.8}])
    out = lg.recall("nginx 配置改完要怎么 reload", platform="cli", session_id="S9")
    assert f"[{a}] 改 nginx 配置后先 nginx -t 再 reload" in out and "^[kb:" not in out.split("相关的教训")[1].split("\n这些是")[0]
    rec = json.loads((kbenv.brain / "kb/recall_hits.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["lessons"] == [a] and rec["session"] == "S9"
