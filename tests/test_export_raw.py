import sqlite3

from helpers import add_chunks


def test_export_only_touches_own_files(kbenv, capsys):
    kb = kbenv.load("kb")
    add_chunks(kb, [("tp-claude:s1:0", "问题一", "回答一"), ("tp-claude:s1:1", "问题二", "回答二"),
                    ("page:projects/x:0", "页面", "页面内容")])
    raw = kbenv.brain / "raw" / "conversations"
    foreign = kbenv.write("tp-claude/my-notes.md", "# 我自己的笔记\n", base=raw)
    legacy = kbenv.write("tp-claude/2026-01-01_old.md", "---\nsource: tp-claude\nsession: old\nproject: -\nfrom: 2026-01-01\nto: 2026-01-01\n---\n# x\n", base=raw)
    er = kbenv.load("export_raw")
    er.main()
    out = raw / "tp-claude" / "2026-10-08_s1.md"
    text = out.read_text(encoding="utf-8")
    assert "generator: hermes-kb/export_raw" in text and "tp-claude:s1:1" in text
    assert foreign.exists()                    # 原来会被当成过期文件删掉
    assert not legacy.exists()                 # 旧版自己生成的过期文件照常清理
    assert not (raw / "page").exists()         # 知识页本身不导出
    capsys.readouterr()
    er.main()
    assert "写入 0，未变 1，删除 0" in capsys.readouterr().out


def test_legacy_raw_dir_fallback(kbenv):
    legacy = kbenv.wiki / "raw" / "conversations"
    legacy.mkdir(parents=True)
    er = kbenv.load("export_raw")
    assert er.OUT == str(legacy)
    kbenv.mp.setenv("RAW_DIR", str(kbenv.tmp / "r"))
    assert kbenv.load("export_raw").OUT == str(kbenv.tmp / "r")
