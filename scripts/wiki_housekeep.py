#!/usr/bin/env python3
"""知识库的索引、地图、日志由脚本维护（2026-10-04 起取代「知识库-导航更新」让模型每小时写 index.md）。

原来：模型每小时给每页写一句摘要堆进 index.md（471 页、139KB，读一次就很贵），一天用掉约 1000 万 token；
log.md 写着「超过 500 条就轮换」但没人做，长到 1.2MB。
现在（no-agent，不调模型）：
  - index.md：按目录分区，每页一行「[[目录/页]]：一句话」，一句话取页首「> 一句话：…」，没有就取标题；
  - _meta/map.md：≤30 行的地图（几大块、各多少页、怎么找）；
  - log.md：只留最近 150 条，更早的追加进 log-2026.md。
只在内容有变化时写文件。
"""
import fcntl, glob, os, re, time

W = os.path.expanduser("~/brain/wiki")
KEEP_LAST = 150                     # log.md 只留最近 150 条（10-01~04 三天就写了 887 条、1.2MB）
SECTIONS = [("projects", "Projects", "每个项目一页：是什么、现状、规则、决定、做法与排障、历史"),
            ("entities", "Entities", "机器、服务/容器、网站、渠道和账号、工具/agent，各自的做法与排障也在页里"),
            ("concepts", "Concepts", "跨项目通用的方法和经验"),
            ("queries", "Queries", "值得留下的问答结论")]


def one_liner(f):
    try:
        head = open(f, encoding="utf-8").read(6000)
    except OSError:
        return ""
    m = re.search(r"^>\s*一句话[:：]\s*(.+)$", head, re.M)
    if not m:
        m = re.search(r"^title:\s*(.+)$", head, re.M)
    t = re.sub(r"\s+", " ", (m.group(1) if m else "")).strip().strip('"')
    t = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", t)
    return t[:60] + ("…" if len(t) > 60 else "")


def write_if_changed(path, text):
    old = open(path, encoding="utf-8").read() if os.path.exists(path) else None
    body = lambda s: re.sub(r"^Last updated:.*$", "", s or "", flags=re.M)
    if old is not None and body(old) == body(text):
        return False
    open(path, "w", encoding="utf-8").write(text)
    return True


def build_index():
    parts, counts, total = [], {}, 0
    for d, title, desc in SECTIONS:
        files = sorted(glob.glob(f"{W}/{d}/*.md"))
        counts[d] = len(files); total += len(files)
        lines = [f"- [[{d}/{os.path.basename(f)[:-3]}]]：{one_liner(f)}" for f in files]
        parts.append(f"## {title}（{len(files)}）\n> {desc}\n" + "\n".join(lines) + "\n")
    text = ("# Wiki Index\n\n> 自动生成（wiki_housekeep.py 每小时）：每页一行，摘要取各页页首「> 一句话」。不要手改。"
            "找东西先用 qmd query \"问题\" -c wiki。\n"
            f"Last updated: {time.strftime('%Y-%m-%d %H:%M')} | Total pages: {total}\n\n" + "\n".join(parts))
    return write_if_changed(f"{W}/index.md", text), counts, total


def build_map(counts, total):
    projects = [os.path.basename(f)[:-3] for f in sorted(glob.glob(f"{W}/projects/*.md"))]
    proj = "、".join(projects)
    text = ("# 知识库地图（自动生成，≤30 行）\n\n"
            f"共 {total} 页。沉淀的系统与工程知识：项目、架构与服务、接口账号、工具、规则、踩坑与解决方案。\n\n"
            + "".join(f"- `{d}/` {counts[d]} 页：{desc}\n" for d, _, desc in SECTIONS)
            + "\n找东西：先 `qmd query \"问题\" -c wiki` → 打开页面；要原始对话用 `-c raw`。全部页面清单在 index.md。\n"
            + f"\n项目：{proj[:600]}{'…' if len(proj) > 600 else ''}\n")
    return write_if_changed(f"{W}/_meta/map.md", text)


def rotate_log():
    path = f"{W}/log.md"
    if not os.path.exists(path):
        return 0
    with open(path, "r+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)           # 和 wiki_feed/wiki_target/wiki_consolidate 追加日志用同一把锁
        s = f.read()
        blocks = re.split(r"(?m)^(?=## \[\d{4}-\d{2}-\d{2}\])", s)
        head = blocks[0] if blocks and not blocks[0].startswith("## [") else ""
        entries = [b for b in blocks if b.startswith("## [")]
        if len(entries) <= KEEP_LAST:
            return 0
        newest = set(sorted(range(len(entries)), key=lambda i: (entries[i][4:14], i))[-KEEP_LAST:])
        old = [b for i, b in enumerate(entries) if i not in newest]
        keep = [b for i, b in enumerate(entries) if i in newest]
        with open(f"{W}/log-2026.md", "a", encoding="utf-8") as a:
            a.write("".join(b if b.endswith("\n") else b + "\n" for b in old))
        f.seek(0); f.truncate(); f.write(head + "".join(keep))
    return len(old)


if __name__ == "__main__":
    changed, counts, total = build_index()
    m = build_map(counts, total)
    r = rotate_log()
    if changed or m or r:
        print(f"index {'更新' if changed else '不变'}（{total} 页）；map {'更新' if m else '不变'}；log 轮换 {r} 条")
