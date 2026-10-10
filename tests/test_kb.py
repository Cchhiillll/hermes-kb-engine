import json
import os
import sqlite3
import time

from conftest import claude_jsonl, hermes_db


def _chunks(db):
    c = sqlite3.connect(db)
    return c.execute("select id, src, session, seq, user, reply from chunks order by id").fetchall()


def test_sid_short_long_and_distinct(kbenv):
    kb = kbenv.load("kb")
    assert kb.sid("abc-123") == "abc-123"
    a = kb.sid("abcdef12-1111-2222-3333-444444444444")
    b = kb.sid("abcdef12-9999-2222-3333-444444444444")
    assert a.startswith("abcdef12-") and b.startswith("abcdef12-") and a != b
    assert kb.sid("20261009_123456_ab") == "20261009123456ab"
    assert all(ch.isalnum() or ch == "-" for ch in kb.sid("会话/中文:id" * 5))


def test_local_and_synced_claude_labeled_once(kbenv):
    claude_jsonl(kbenv.home / ".claude/projects/p/aaaa1111-0000-0000-0000-000000000001.jsonl", [("本机问题一", "本机回答一")])
    claude_jsonl(kbenv.home / "mac_agent_sync/claude/projects/p/bbbb2222-0000-0000-0000-000000000002.jsonl", [("同步问题二", "同步回答二")])
    kb = kbenv.load("kb")
    srcs = [t[0] for t in kb.sources()]
    keys = [t[3] for t in kb.sources()]
    assert sorted(srcs) == ["mac-claude", "tp-claude"]
    assert len(keys) == len(set(keys))           # 同一文件只出现一次（原来本机文件出现两次）
    kb.build()
    rows = _chunks(kbenv.db)
    assert {r[1] for r in rows} == {"tp-claude", "mac-claude"}
    assert len(rows) == 2


def test_prefixes_configurable(kbenv):
    kbenv.mp.setenv("KB_LOCAL_PREFIX", "mac")
    kbenv.mp.setenv("KB_SYNC_PREFIX", "tp")
    claude_jsonl(kbenv.home / ".claude/projects/p/x1.jsonl", [("问", "答")])
    kb = kbenv.load("kb")
    assert [t[0] for t in kb.sources()] == ["mac-claude"]


def test_session_prefix_collision_does_not_delete(kbenv):
    d = kbenv.home / ".claude/projects/p"
    f1 = claude_jsonl(d / "abcdef12-1111-0000-0000-000000000001.jsonl", [("第一个会话的问题", "回答甲")])
    claude_jsonl(d / "abcdef12-2222-0000-0000-000000000002.jsonl", [("第二个会话的问题", "回答乙")])
    kb = kbenv.load("kb")
    kb.build()
    assert len(_chunks(kbenv.db)) == 2
    claude_jsonl(f1, [("第一个会话的问题", "回答甲"), ("追加的问题", "追加的回答")])
    os.utime(f1, (time.time() + 10, time.time() + 10))
    kb.build()
    users = {r[4] for r in _chunks(kbenv.db)}
    assert users == {"第一个会话的问题", "追加的问题", "第二个会话的问题"}   # 原来 8 位 ID 撞车会删掉第二个会话


def test_legacy_identity_preserved(kbenv):
    """旧库：片段记成 mac-claude:abcdef12:N，files 表因旧 bug 记成 tp-claude。文件变化后重建，出处 ID 不变。"""
    f = claude_jsonl(kbenv.home / ".claude/projects/p/abcdef12-aaaa-0000-0000-000000000000.jsonl", [("老问题", "老回答")])
    kb = kbenv.load("kb")
    c = kb.conn()
    c.execute("insert into chunks values(?,?,?,?,?,?,?,?,?,?,?)",
              ("mac-claude:abcdef12:0", "mac-claude", "abcdef12", "", 0, "2026-10-01", 0, "老问题", "老回答", "[]", "h-old"))
    c.execute("insert into files values(?,?,?,?)", (str(f), "tp-claude", "abcdef12", 1.0))
    c.commit()
    claude_jsonl(f, [("老问题", "老回答"), ("新问题", "新回答")])
    kb.build()
    ids = [r[0] for r in _chunks(kbenv.db)]
    assert ids == ["mac-claude:abcdef12:0", "mac-claude:abcdef12:1"]


def test_incremental_and_redaction(kbenv, capsys):
    claude_jsonl(kbenv.home / ".claude/projects/p/s1.jsonl",
                 [("我的 token=sk-abcdef1234567890abcdef 和 password: hunter22", "语雀导出可以用增量同步")])
    kb = kbenv.load("kb")
    kb.build()
    kb.build()
    out = capsys.readouterr().out
    assert "处理 0 个会话" in out.splitlines()[-1]
    user = _chunks(kbenv.db)[0][4]
    assert "sk-abcdef1234567890" not in user and "hunter22" not in user
    kb.cmd_search(["语雀", "增量"])
    out = capsys.readouterr().out
    assert "tp-claude:s1:0" in out
    kb.cmd_open(["tp-claude:s1:0"])
    assert "语雀导出" in capsys.readouterr().out


def test_project_root_config(kbenv):
    kbenv.mp.setenv("KB_PROJECT_ROOT", "/work/AI Native")
    claude_jsonl(kbenv.home / ".claude/projects/p/s2.jsonl", [("问", "答")], cwd="/work/AI Native/projA/sub")
    kb = kbenv.load("kb")
    assert next(kb.sources())[2] == "projA"
    assert kb.DSH_MARK == "AI~0020Native-"
    kbenv.mp.delenv("KB_PROJECT_ROOT")
    kb = kbenv.load("kb")
    assert kb.project_of("/work/AI Native/projA") == ""


def test_hermes_profiles_and_source_filter(kbenv):
    hermes_db(kbenv.home / ".hermes/state.db", {
        "20261009_100000_aaaa": ("feishu", [("user", "飞书里问的问题"), ("assistant", "飞书回答")]),
        "20261009_100000_bbbb": ("cron", [("user", "定时任务的输入"), ("assistant", "不该收")]),
    })
    hermes_db(kbenv.home / ".hermes/profiles/sylphy/state.db", {
        "s9": ("discord", [("user", "discord 问题"), ("assistant", "discord 回答")]),
    })
    kb = kbenv.load("kb")
    got = {(t[0], t[3]) for t in kb.sources()}
    assert ("tp-hermes", "hermes:20261009_100000_aaaa") in got
    assert ("tp-sylphy", "sylphy:s9") in got
    assert not any("bbbb" in k for _, k in got)
    kb.build()
    ids = [r[0] for r in _chunks(kbenv.db)]
    assert "tp-sylphy:s9:0" in ids


def test_codex_openclaw_grok_parsers(kbenv):
    codex = kbenv.write(".codex/sessions/2026/rollout-2026-10-08T01-00-00-11111111-2222-3333-4444-555555555555.jsonl", "\n".join(json.dumps(x) for x in [
        {"type": "event_msg", "timestamp": "2026-10-08T01:00:00Z", "payload": {"item": {"type": "UserMessage", "content": "codex 的问题"}}},
        {"type": "response_item", "timestamp": "2026-10-08T01:00:01Z", "payload": {"type": "function_call", "arguments": json.dumps({"cmd": ["ls", "-la"]})}},
        {"type": "response_item", "timestamp": "2026-10-08T01:00:02Z", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "codex 的回答"}]}},
    ]))
    kbenv.mp.setenv("KB_OPENCLAW_USER_PREFIX", "alice")
    kbenv.write(".openclaw/agents/main/sessions/oc1.jsonl", "\n".join(json.dumps(x) for x in [
        {"type": "message", "timestamp": 1760000000000, "message": {"role": "user", "content": "[message_id: 1] alice: openclaw 问题"}},
        {"type": "message", "timestamp": 1760000001000, "message": {"role": "assistant", "content": [{"type": "text", "text": "openclaw 回答"}]}},
    ]))
    kbenv.write(".grok/sessions/proj/g1/updates.jsonl", "\n".join(json.dumps(x) for x in [
        {"timestamp": 1760000000, "params": {"update": {"sessionUpdate": "user_message_chunk", "content": {"text": "grok 问"}}}},
        {"timestamp": 1760000001, "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "grok 答"}}}},
    ]))
    kb = kbenv.load("kb")
    evs = list(kb.ev_codex(str(codex)))
    assert ("user", "codex 的问题") in [(e[1], e[2]) for e in evs]
    assert any(e[1] == "cmd" and e[2] == "ls -la" for e in evs)
    kb.build()
    rows = {r[1]: r for r in _chunks(kbenv.db)}
    assert rows["tp-openclaw"][4] == "openclaw 问题"
    assert rows["tp-grok"][5] == "grok 答"
    assert rows["tp-codex"][5] == "codex 的回答"


def test_zcode_db(kbenv):
    db = kbenv.home / ".zcode/cli/db/db.sqlite"
    db.parent.mkdir(parents=True)
    c = sqlite3.connect(db)
    c.execute("create table session(id text, directory text, time_updated real)")
    c.execute("create table message(id text, session_id text, time_created real, sequence int, data text)")
    c.execute("create table part(id text, message_id text, sequence int, data text)")
    c.execute("insert into session values('sess_abc', '/tmp', 1760000000000)")
    c.execute("insert into message values('m1','sess_abc',1760000000000,0,?)", (json.dumps({"role": "user"}),))
    c.execute("insert into message values('m2','sess_abc',1760000001000,1,?)", (json.dumps({"role": "assistant"}),))
    c.execute("insert into part values('p1','m1',0,?)", (json.dumps({"type": "text", "text": "zcode 问"}),))
    c.execute("insert into part values('p2','m2',0,?)", (json.dumps({"type": "text", "text": "zcode 答"}),))
    c.commit(); c.close()
    kb = kbenv.load("kb")
    srcs = [t[0] for t in kb.sources()]
    assert srcs.count("tp-zcode") == 1 and "mac-zcode" not in srcs     # 原来本机库会被扫两遍
    kb.build()
    assert _chunks(kbenv.db)[0][0] == "tp-zcode:abc:0"


def test_exchanges_noise_and_long_reply(kbenv):
    kb = kbenv.load("kb")
    evs = [(1, "user", "问一"), (1, "reply", "x" * 13000), (1, "cmd", "ls"), (2, "user", "[Request interrupted by user]"), (3, "user", "问二")]
    out = list(kb.exchanges(evs))
    assert [o[2] for o in out] == ["问一", "问二"]
    assert "中间过程省略" in out[0][3] and len(out[0][3]) < 13000
    assert out[0][4] == ["ls"]


def test_toks_cjk_bigrams(kbenv):
    kb = kbenv.load("kb")
    assert kb.toks("语雀导出 API_Key") == ["语雀", "雀导", "导出", "api_key"]
