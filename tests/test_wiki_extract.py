import json

import pytest

from helpers import add_chunks

GOOD = '好的，结果如下：\n```json\n{"items":[{"seg":["tp-claude:s:0"],"kind":"new","basis":"user","topic":"t","text":"用户要用语雀"},' \
       '{"seg":["tp-claude:s:1"],"kind":"new","basis":"agent","topic":"t","text":"说已部署"}],"none":[]}\n```'


def _env(kbenv, n=2):
    kbenv.mp.setenv("KB_MODEL", "m"); kbenv.mp.setenv("OPENAI_BASE_URL", "http://relay.invalid/v1")
    kb = kbenv.load("kb")
    c = add_chunks(kb, [(f"tp-claude:s:{i}", f"问题{i}" * 50, f"回答{i}" * 50) for i in range(n)])
    we = kbenv.load("wiki_extract")
    return we, we.conn()


def test_parse_notes_tolerant(kbenv):
    we = kbenv.load("wiki_extract")
    n = we.parse_notes(GOOD)
    assert len(n["items"]) == 2 and n["none"] == []
    assert we.parse_notes('{"items": [{"text": ""}, {"text": "a"}]}')["items"] == [{"text": "a"}]
    for bad in ("", "没有 JSON", "{bad json}", "[1,2]", '{"items": 3}'):
        with pytest.raises(we.BadOutput):
            we.parse_notes(bad)


def test_tag_unverified(kbenv):
    we = kbenv.load("wiki_extract")
    n = we.tag_unverified({"items": [{"basis": "agent", "text": "x"}, {"basis": "output", "text": "y"}, {"text": "z"}]})
    assert n["items"][0]["text"].startswith("（未核实）") and n["items"][1]["text"] == "y"
    assert n["items"][2]["basis"] == "agent"


def test_main_success(kbenv, monkeypatch, capsys):
    we, c = _env(kbenv)
    monkeypatch.setattr(we.kb_llm, "chat", lambda *a, **k: (GOOD, 10))
    monkeypatch.setattr("sys.argv", ["wiki_extract.py", "--max", "1"])
    we.main()
    assert c.execute("select count(*) from extracted").fetchone()[0] == 2
    notes = json.loads(c.execute("select notes from extract_batches").fetchone()[0])
    assert notes["items"][1]["text"].startswith("（未核实）")


def test_main_bad_output_retries_then_records(kbenv, monkeypatch, capsys):
    we, c = _env(kbenv)
    calls = []
    monkeypatch.setattr(we.kb_llm, "chat", lambda *a, **k: (calls.append(1), ("抱歉我做不到", 1))[1])
    monkeypatch.setattr("sys.argv", ["wiki_extract.py"])
    we.main()                                           # 原来 json.loads 抛异常整个任务崩掉
    assert len(calls) == we.RETRIES + 1
    assert c.execute("select count(*) from extracted").fetchone()[0] == 0
    assert we.fail_count(c, "tp-claude:s:0") == 1
    assert "提炼失败" in capsys.readouterr().out


def test_budget_halves_after_failures(kbenv, monkeypatch):
    we, c = _env(kbenv)
    for _ in range(2):
        we.record_fail(c, "tp-claude:s:0", "x")
    budgets = []
    real = we.select
    monkeypatch.setattr(we, "select", lambda c, d=None, b=we.BUDGET: (budgets.append(b), real(c, d, b))[1])
    monkeypatch.setattr(we.kb_llm, "chat", lambda *a, **k: (GOOD, 1))
    monkeypatch.setattr("sys.argv", ["wiki_extract.py", "--max", "1"])
    we.main()
    assert budgets[1] == we.BUDGET >> 2
    assert we.fail_count(c, "tp-claude:s:0") == 0      # 成功后清零


def test_no_target_does_not_mark(kbenv, monkeypatch, capsys):
    kb = kbenv.load("kb")
    c = add_chunks(kb, [("tp-claude:s:0", "问", "答")])
    we = kbenv.load("wiki_extract")
    monkeypatch.setattr("sys.argv", ["wiki_extract.py"])
    we.main()
    assert "NO_TARGET" in capsys.readouterr().out
    assert c.execute("select count(*) from extracted").fetchone()[0] == 0


def test_select_date_param(kbenv):
    we, c = _env(kbenv)
    assert we.select(c, "2026-10-08")[0] and not we.select(c, "2026-01-01' or '1'='1")[0]
