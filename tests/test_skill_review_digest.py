import datetime as dt
import json
import os
import time

import pytest

from test_yuque import _export

YESTERDAY = (dt.date.today() - dt.timedelta(days=1)).isoformat()


def _chunks(kb, rows):
    c = kb.conn()
    for i, (cid, user, reply, cmds) in enumerate(rows):
        src, sess, seq = cid.rsplit(":", 2)
        c.execute("insert into chunks values(?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, src, sess, "", 1000 + i, YESTERDAY, int(seq), user, reply, json.dumps(cmds), cid))
    c.commit()
    return c


def _approval(kbenv, skills=True, memory=True):
    kbenv.write(".hermes/config.yaml", f"model: x\nskills:\n  write_approval: {str(skills).lower()}\n  dir: ~/.hermes/skills\n"
                                       f"memory:\n  # 注释\n  write_approval: {str(memory).lower()}\n")


def test_yaml_flag_fallback(kbenv):
    sr = kbenv.load("skill_review_feed")
    t = "skills:\n  write_approval: true\nmemory:\n  enabled: true\n"
    assert sr.yaml_flag(t, "skills", "write_approval") and not sr.yaml_flag(t, "memory", "write_approval")
    assert sr.yaml_flag("memory.write_approval: true\n", "memory", "write_approval")


def test_skill_review_requires_approval(kbenv, capsys):
    sr = kbenv.load("skill_review_feed")
    assert sr.feed([]) == 0
    assert "NO_TARGET" in capsys.readouterr().out
    _approval(kbenv, memory=False)
    sr.feed([])
    out = capsys.readouterr().out
    assert "NO_TARGET" in out and "memory.write_approval" in out


def test_skill_review_feed_and_done(kbenv, capsys):
    _approval(kbenv)
    kbenv.write(".hermes/skills/deploy/SKILL.md", "---\nname: deploy-site\ndescription: 部署网站\n---\n")
    old = kbenv.home / ".hermes/skills/deploy/SKILL.md"
    os.utime(old, (time.time() - 90 * 86400, time.time() - 90 * 86400))
    kb = kbenv.load("kb")
    _chunks(kb, [("tp-claude:good:0", "把服务迁到新机器", "完成", ["rsync a b", "systemctl stop x", "cp c d"]),
                 ("tp-claude:good:1", "继续", "好了", ["systemctl start x", "curl localhost"]),
                 ("tp-claude:fixed:0", "装证书", "做了", ["a", "b", "c", "d", "e"]),
                 ("tp-claude:fixed:1", "不对，证书路径错了", "改了", []),
                 ("tp-claude:short:0", "看下时间", "现在 3 点", ["date"])])
    sr = kbenv.load("skill_review_feed")
    assert sr.feed([]) == 0
    out = capsys.readouterr().out
    batch = out.split("批次号：")[1].split("（")[0]
    material = (kbenv.brain / f"kb/batches/{batch}.md").read_text(encoding="utf-8")
    assert "tp-claude:good" in material and "fixed" not in material and "short" not in material
    assert "deploy-site：部署网站" in material and "60 天没被用到的技能" in material
    assert sr.done(batch) == 1                                     # 没写记录不让交
    kbenv.write(f"brain/kb/batches/{batch}.log.md", "- 新建技能 migrate-service（待审）\n")
    assert sr.done(batch) == 0
    assert f"skill-review | {batch}" in (kbenv.wiki / "log.md").read_text(encoding="utf-8")
    capsys.readouterr()
    sr.feed([])
    assert "NO_TARGET" in capsys.readouterr().out                 # 复盘过的不再送


def test_yuque_digest_flow(kbenv, capsys):
    _export(kbenv)
    kbenv.load("clean_yuque").main([])
    yd = kbenv.load("yuque_digest")
    assert yd.feed([]) == 0
    assert "还没收录" in capsys.readouterr().out
    kb = kbenv.load("kb")
    kb.build()
    assert yd.sid("abcdef12-1111-2222-3333-444444444444") == kb.sid("abcdef12-1111-2222-3333-444444444444")
    capsys.readouterr()
    assert yd.feed([]) == 0
    out = capsys.readouterr().out
    batch = out.split("批次号：")[1].split("（")[0]
    material = (kbenv.brain / f"kb/batches/{batch}.md").read_text(encoding="utf-8")
    assert "文档键 12345" in material and "[yuque-doc:12345:0]" in material and "https://www.yuque.com/u/ops/overview" in material
    lock = kbenv.load("kb_lock")
    assert not lock.acquire("merge", pid=str(os.getpid()))[0]     # 送料后拿着写入锁
    yd = kbenv.load("yuque_digest")
    other = [l.split("文档键 ")[1] for l in material.splitlines() if "文档键" in l and "12345" not in l][0]
    assert yd.done(batch) == 1
    kbenv.write("entities/运维.md", "# 运维\n## 相关语雀文档\n- 《概览页》（语雀，更新于 2026-09-30）：总览 [原文](https://www.yuque.com/u/ops/overview) ^[kb:yuque-doc:12345:0]\n", base=kbenv.wiki)
    kbenv.write(f"brain/kb/batches/{batch}.log.md", f"- 无可记：{other}（安装细节，项目页已有）\n")
    assert yd.done(batch) == 0
    assert lock.acquire("merge", pid=str(os.getpid()))[0]          # 交卷后释放
    lock.release("merge")
    capsys.readouterr()
    yd.feed([])
    assert "NO_TARGET：没有新增或改过" in capsys.readouterr().out
    # 语雀文档改了 → 再次送料
    p = kbenv.brain / "sources/yuque-export/运维手册/概览.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n## 新小节\n新增内容\n", encoding="utf-8")
    kbenv.load("clean_yuque").main([])
    kbenv.load("kb").build()
    yd = kbenv.load("yuque_digest")
    capsys.readouterr()
    yd.feed([])
    assert "批次号：y" in capsys.readouterr().out
