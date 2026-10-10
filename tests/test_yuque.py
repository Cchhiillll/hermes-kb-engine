import json
import os
import sqlite3
import subprocess
import sys

from conftest import REPO
from test_shell import _env, _stub

RAW = """<a name="Abc12"></a>
#安装指南
<font style="color:red">注意</font>：先装 `node`<br />再装 `elog`。&nbsp;
<!-- lake card -->
![](https://cdn.nlark.com/yuque/0/2026/png/1/a.png#averageHue=%23f9f8f8&clientId=u1)
![本地](../img/b.png)

##步骤
1. 运行 `elog init`
```bash
## 这不是标题
<span>代码里原样保留</span>
```
## 排障
<span style="x">端口</span> `8181` 被占用时换端口。
"""


def _export(kbenv):
    exp = kbenv.brain / "sources" / "yuque-export"
    kbenv.write("运维手册/部署/安装指南.md", RAW, base=exp)
    kbenv.write("运维手册/概览.md", "---\ntitle: \"概览页\"\nid: 12345\nurlname: overview\nupdated: 2026-09-30T08:00:00Z\nurl: https://www.yuque.com/u/ops/overview\n---\n简介正文\n", base=exp)
    kbenv.write(".export_records.json", "{}", base=exp)
    return exp


def test_clean_body_strips_yuque_html(kbenv):
    cy = kbenv.load("clean_yuque")
    out = cy.clean_body(RAW, "/x/运维手册/部署/安装指南.md")
    assert "<a name" not in out and "<font" not in out and "<br" not in out and "lake card" not in out
    assert "注意：先装 `node`\n再装 `elog`。" in out
    assert "# 安装指南" in out and "## 步骤" in out
    assert "a.png)" in out and "averageHue" not in out
    assert "](/x/运维手册/img/b.png)" in out
    assert "## 这不是标题\n<span>代码里原样保留</span>" in out      # 代码块不动


def test_convert_frontmatter_and_context(kbenv):
    exp = _export(kbenv)
    cy = kbenv.load("clean_yuque")
    text = cy.convert(str(exp / "运维手册/部署/安装指南.md"), "运维手册/部署/安装指南.md")
    km = kbenv.load("kb_md")
    fm, body = km.split_frontmatter(text)
    assert fm["title"] == "安装指南" and fm["book"] == "运维手册" and fm["toc_path"] == "部署"
    assert fm["generator"] == cy.GENERATOR and fm["doc_key"].startswith("h")
    assert body.lstrip().startswith("# 安装指南\n\n> 上下文：运维手册 › 部署 › 安装指南")
    assert "## 步骤\n\n> 上下文：安装指南 › 步骤" in body
    assert "## 这不是标题\n<span>" in body                       # 代码块里的 ## 不加上下文
    t2 = cy.convert(str(exp / "运维手册/概览.md"), "运维手册/概览.md")
    fm2, _ = km.split_frontmatter(t2)
    assert fm2["doc_key"] == "12345" and fm2["slug"] == "overview" and fm2["url"].endswith("/overview")
    assert fm2["updated_at"] == "2026-09-30T08:00:00Z" and fm2["title"] == "概览页"


def test_run_idempotent_and_safe_delete(kbenv, capsys):
    exp = _export(kbenv)
    out = kbenv.brain / "sources" / "yuque"
    mine = kbenv.write("my-own-note.md", "# 我自己放的\n", base=out)
    cy = kbenv.load("clean_yuque")
    assert cy.main([]) == 0
    assert "2 篇，写入 2" in capsys.readouterr().out
    cy.main([])
    assert "写入 0，未变 2，删除 0" in capsys.readouterr().out
    os.remove(exp / "运维手册/部署/安装指南.md")
    cy.main([])
    assert "删除 1" in capsys.readouterr().out
    assert mine.exists() and not (out / "运维手册/部署").exists()
    assert not (out / ".export_records.json").exists()


def test_kb_indexes_yuque_but_not_feed(kbenv, capsys):
    _export(kbenv)
    kbenv.load("clean_yuque").main([])
    kb = kbenv.load("kb")
    kb.build()
    c = sqlite3.connect(kbenv.db)
    rows = c.execute("select id, project, user, reply, date from chunks where src='yuque-doc' order by id").fetchall()
    ids = [r[0] for r in rows]
    assert "yuque-doc:12345:0" in ids
    sec = [r for r in rows if r[2].endswith("｜排障")][0]
    assert sec[1] == "运维手册" and "`8181`" in sec[3] and "上下文：安装指南 › 排障" in sec[3]
    assert [r for r in rows if r[0] == "yuque-doc:12345:0"][0][4] == "2026-09-30"
    wf = kbenv.load("wiki_feed")
    assert wf.progress(wf.conn()) == (0, 0)                     # 语雀文档不进「读历史/提炼」队列
    kbenv.load("export_raw").main()
    assert not (kbenv.brain / "raw/conversations/yuque-doc").exists()
    kb.cmd_search(["8181", "端口"])
    assert "yuque-doc:" in capsys.readouterr().out
    # 语雀那边删掉的文档：重建时片段一起删
    os.remove(kbenv.brain / "sources/yuque-export/运维手册/概览.md")
    kbenv.load("clean_yuque").main([])
    kb.build()
    assert not c.execute("select 1 from chunks where id like 'yuque-doc:12345:%'").fetchone()
    assert c.execute("select count(*) from chunks where src='yuque-doc'").fetchone()[0] > 0
    wc = kbenv.load("wiki_consolidate")
    assert wc.KB_ID.findall("x ^[kb:yuque-doc:h0123456789:2]") == ["yuque-doc:h0123456789:2"]


def test_sync_yuque_unconfigured_skips(kbenv):
    r = subprocess.run(["bash", f"{REPO}/tools/sync_yuque.sh"], env=_env(kbenv), capture_output=True, text=True)
    assert r.returncode == 0
    assert "跳过语雀同步" in (kbenv.brain / ".state/yuque_sync.log").read_text(encoding="utf-8")


def test_sync_yuque_exporter_stub(kbenv):
    stubs = _stub(kbenv, "yuque-exporter", f'''echo "$@" >> {kbenv.tmp}/ye.calls
out=""; repo=""
while [ $# -gt 0 ]; do case "$1" in -output) out=$2; shift;; -repo) repo=$2; shift;; esac; shift; done
mkdir -p "$out/$repo"; printf '# 文档\\n正文 %s\\n' "$repo" > "$out/$repo/doc.md"''')
    env = _env(kbenv, stubs, KB_YUQUE_TOOL="yuque-exporter", YUQUE_TOKEN="t-test", YUQUE_USER="me", YUQUE_REPOS="ops dev")
    r = subprocess.run(["bash", f"{REPO}/tools/sync_yuque.sh"], env=env, capture_output=True, text=True)
    log = (kbenv.brain / ".state/yuque_sync.log").read_text(encoding="utf-8")
    assert r.returncode == 0, log
    calls = (kbenv.tmp / "ye.calls").read_text().splitlines()
    assert len(calls) == 2 and "-repo ops" in calls[0] and "-user me" in calls[0]
    assert (kbenv.brain / "sources/yuque/ops/doc.md").exists() and (kbenv.brain / "sources/yuque/dev/doc.md").exists()
    assert "t-test" not in log                                    # 日志里不出现 token


def test_sync_yuque_missing_token_fails(kbenv):
    stubs = _stub(kbenv, "yuque-exporter", "exit 0")
    env = _env(kbenv, stubs, KB_YUQUE_TOOL="yuque-exporter")
    r = subprocess.run(["bash", f"{REPO}/tools/sync_yuque.sh"], env=env, capture_output=True, text=True)
    assert r.returncode == 1
    assert "缺少 YUQUE_TOKEN" in (kbenv.brain / ".state/yuque_sync.log").read_text(encoding="utf-8")


def test_sync_yuque_elog_stub(kbenv):
    elog_dir = kbenv.tmp / "elogcfg"
    elog_dir.mkdir()
    exp = kbenv.brain / "sources/yuque-export"
    stubs = _stub(kbenv, "elog", f'echo "$PWD $@" >> {kbenv.tmp}/elog.calls; mkdir -p {exp}/书; echo "# 甲" > {exp}/书/甲.md')
    env = _env(kbenv, stubs, KB_YUQUE_TOOL="elog", KB_YUQUE_ELOG_DIR=str(elog_dir))
    r = subprocess.run(["bash", f"{REPO}/tools/sync_yuque.sh"], env=env, capture_output=True, text=True)
    assert r.returncode == 0
    call = (kbenv.tmp / "elog.calls").read_text()
    assert call.startswith(str(elog_dir)) and "sync -c elog.config.js -e .elog.env" in call
    assert (kbenv.brain / "sources/yuque/书/甲.md").exists()


def test_recall_yuque_hits_and_fallback(kbenv, monkeypatch):
    _export(kbenv)
    kbenv.load("clean_yuque").main([])
    lg = kbenv.load_path("kb_recall_logic", "plugins/kb-recall/logic.py")
    assert lg.COLLECTIONS == ["wiki", "yuque"]                    # 语雀目录存在时默认一起查
    tried = []

    def fake_query(text, cols):
        tried.append(list(cols))
        if "yuque" in cols:
            raise RuntimeError("collection not found")
        return [{"file": "yuque/运维手册/概览.md", "score": 0.8}]
    monkeypatch.setattr(lg, "_query", fake_query)
    out = lg.recall("语雀里的运维手册概览讲了什么", platform="cli")
    assert tried == [["wiki", "yuque"], ["wiki"]]
    assert "〔语雀〕《概览页》（yuque/运维手册/概览.md，语雀更新于 2026-09-30，原文 https://www.yuque.com/u/ops/overview）：简介正文" in out
