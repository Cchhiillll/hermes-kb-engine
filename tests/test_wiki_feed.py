import os
import time

import pytest

from conftest import LONG
from helpers import add_chunks


def _setup(kbenv, ids):
    kb = kbenv.load("kb")
    c = add_chunks(kb, [(i, "问" + LONG, LONG) for i in ids])
    wf = kbenv.load("wiki_feed")
    wf.conn()
    return wf, c


def _feed(wf, capsys):
    wf.feed("history", 10 ** 6)
    out = capsys.readouterr().out
    return out.split("批次号：")[1].split("（")[0]



def test_done_rejects_prefix_match(kbenv, capsys):
    """「无可记：x:12」不能放行 x:1（原来是子串判断）。"""
    wf, c = _setup(kbenv, ["tp-claude:s:1", "tp-claude:s:12"])
    b = _feed(wf, capsys)
    kbenv.write(f"{b}.log.md", "- 无可记：tp-claude:s:12（闲聊）\n", base=kbenv.brain / "kb" / "batches")
    with pytest.raises(SystemExit) as e:
        wf.done(b)
    assert e.value.code == 1
    assert "tp-claude:s:1" in capsys.readouterr().out
    assert wf.skip_ids("- 无可记：a-b:c:1、a-b:c:12（x）\n- 新建 a-b:c:3") == {"a-b:c:1", "a-b:c:12"}


def test_done_accepts_cited_and_skipped(kbenv, capsys):
    wf, c = _setup(kbenv, ["tp-claude:s:1", "tp-claude:s:2"])
    b = _feed(wf, capsys)
    kbenv.write("projects/demo.md", "# demo\n- 做法 ^[kb:tp-claude:s:1]\n", base=kbenv.wiki)
    kbenv.write(f"{b}.log.md", "# 记录\n- 更新 projects/demo\n- 无可记：tp-claude:s:2（闲聊）\n", base=kbenv.brain / "kb" / "batches")
    wf.done(b)
    out = capsys.readouterr().out
    assert f"已标记批次 {b}：2 段" in out
    assert f"ingest | {b}" in (kbenv.wiki / "log.md").read_text(encoding="utf-8")
    assert c.execute("select count(*) from hermes_read where done is not null").fetchone()[0] == 2


def test_cited_ranges(kbenv):
    wf = kbenv.load("wiki_feed")
    kbenv.write("concepts/x.md", "a ^[kb:tp-codex:abc:0-2] b ^[kb:page:projects/x:3]", base=kbenv.wiki)
    ids = wf.cited_ids()
    assert {"tp-codex:abc:0", "tp-codex:abc:1", "tp-codex:abc:2"} <= ids


def test_busy_without_state_db_and_stats(kbenv, capsys):
    wf, c = _setup(kbenv, ["tp-claude:s:1"])
    assert wf.busy_batches(c) == set()
    b = _feed(wf, capsys)
    assert wf.busy_batches(c) == {b}          # 刚发出的批次算在读
    c.execute("update hermes_read set fed=?", (time.time() - 3600,)); c.commit()
    assert wf.busy_batches(c) == set()        # 没有 state.db 时不再报错
    wf.stats()
    assert "0/1" in capsys.readouterr().out


def test_stats_empty_db(kbenv, capsys):
    kbenv.load("kb").conn()
    wf = kbenv.load("wiki_feed")
    wf.stats()
    assert "0/0" in capsys.readouterr().out


def test_feed_excludes_pages(kbenv, capsys):
    wf, c = _setup(kbenv, ["page:projects/x:0"])
    wf.feed("history", 10 ** 6)
    assert "NO_MATERIAL" in capsys.readouterr().out
