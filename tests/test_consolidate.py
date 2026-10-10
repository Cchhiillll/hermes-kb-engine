import re

import pytest

from helpers import add_chunks

ORIG = """---
title: demo
---
# demo
- 用 `qmd` 和 `kb.py` 跑通流程，这里是一段足够长的说明文字 ^[kb:tp-claude:s:0]
- 端口 `8181`，配置文件 `config.yaml`，这一条也写了足够多的说明文字 ^[kb:tp-claude:s:1]
"""

GOOD = """---
title: demo
---
# demo
## 现状
- 截至 2026-10-08 用 `qmd` 和 `kb.py` 跑通流程，这里是说明 ^[kb:tp-claude:s:0]
## 做法与排障
- 端口 `8181`，配置文件 `config.yaml`，这一条也写了足够多的说明文字 ^[kb:tp-claude:s:1]
"""


def _pick(kbenv, capsys):
    kb = kbenv.load("kb")
    add_chunks(kb, [("tp-claude:s:0", "问", "答"), ("tp-claude:s:1", "问", "答")])
    kbenv.write("projects/demo.md", ORIG, base=kbenv.wiki)
    wc = kbenv.load("wiki_consolidate")
    wc.pick()
    out = capsys.readouterr().out
    gid = re.search(r"组号：(\S+)", out).group(1)
    return wc, gid


def test_inline_ids_and_ticks(kbenv):
    wc = kbenv.load("wiki_consolidate")
    assert wc.inline_ids("- ^[kb:tp-claude:s:0]") == set()                  # 光秃秃的出处清单不算
    assert wc.inline_ids("- 这一条有足够长的正文内容用来支撑这个出处的说明 ^[kb:tp-claude:s:0]") == {"tp-claude:s:0"}
    assert wc.ticks_substantive("- `a1` `b2` `c3` `d4` `e5`") == set()       # 一行堆清单不算
    assert wc.ticks_substantive("- 用 `qmd` 跑") == {"qmd"}


def test_big_page_rejects_lost_details_and_future_date(kbenv, capsys):
    wc, gid = _pick(kbenv, capsys)
    assert gid.startswith("big-")
    bad = "# demo\n## 现状\n- 截至 2026-12-31 都好了，这里写一段足够长的说明 ^[kb:tp-claude:s:0]\n"
    open(f"{wc.STAGE}/{gid}/projects/demo.md", "w", encoding="utf-8").write(bad)
    with pytest.raises(SystemExit) as e:
        wc.done(gid)
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "具体项" in out and "截至 2026-12-31" in out
    assert (kbenv.wiki / "projects/demo.md").read_text(encoding="utf-8") == ORIG     # 知识库没被动


def test_big_page_commit(kbenv, capsys):
    wc, gid = _pick(kbenv, capsys)
    open(f"{wc.STAGE}/{gid}/projects/demo.md", "w", encoding="utf-8").write(GOOD)
    wc.done(gid, check_only=True)
    assert "检查通过" in capsys.readouterr().out
    wc.done(gid)
    assert (kbenv.wiki / "projects/demo.md").read_text(encoding="utf-8") == GOOD
    assert "restructure | projects/demo" in (kbenv.wiki / "log.md").read_text(encoding="utf-8")
    assert list((kbenv.wiki / "_archive/snapshots").glob("demo-*.md"))


def test_commit_aborts_if_page_changed(kbenv, capsys):
    wc, gid = _pick(kbenv, capsys)
    open(f"{wc.STAGE}/{gid}/projects/demo.md", "w", encoding="utf-8").write(GOOD)
    (kbenv.wiki / "projects/demo.md").write_text(ORIG + "- 别的任务刚加的一条\n", encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        wc.done(gid)
    assert e.value.code == 2
