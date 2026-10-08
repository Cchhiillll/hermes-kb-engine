#!/usr/bin/env python3
"""LLM 维基原始资料层：把 kb.sqlite 里的全部对话按会话导出成 Markdown → ~/brain/wiki/raw/conversations/<来源>/<日期>_<会话>.md
内容没变的文件不重写（qmd update 据此只重扫变化的）。qmd 负责检索；这里只做格式转换。"""
import os, re, sqlite3, collections
DB = os.path.expanduser("~/brain/kb/kb.sqlite")
OUT = os.path.expanduser("~/brain/wiki/raw/conversations")

def main():
    c = sqlite3.connect(DB)
    rows = c.execute("""select src, session, project, id, date, user, reply from chunks
                        where src != 'page' order by src, session, seq""").fetchall()
    by = collections.OrderedDict()
    for r in rows: by.setdefault((r[0], r[1]), []).append(r)
    wrote = same = 0; keep = set()
    for (src, session), rs in by.items():
        d0, d1, project = rs[0][4], rs[-1][4], next((r[2] for r in rs if r[2]), "")
        sid = re.sub(r"[^0-9A-Za-z-]", "", session)[:40]
        name = f"{d0}_{sid}.md"
        path = os.path.join(OUT, src, name); keep.add(path)
        body = [f"---\nsource: {src}\nsession: {session}\nproject: {project or '-'}\nfrom: {d0}\nto: {d1}\n---\n",
                f"# {project or src} · {d0}" + (f" ~ {d1}" if d1 != d0 else "") + f"（{src}）\n"]
        for _, _, _, cid, date, user, reply in rs:
            body.append(f"\n## {date} · {cid}\n\n**他**：{user.strip()}\n")
            if reply.strip(): body.append(f"\n**agent**：{reply.strip()}\n")
        text = "".join(body)
        if os.path.exists(path) and open(path).read() == text: same += 1; continue
        os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w").write(text); wrote += 1
    gone = 0
    for root, _, fs in os.walk(OUT):
        for f in fs:
            p = os.path.join(root, f)
            if f.endswith(".md") and p not in keep: os.remove(p); gone += 1
    print(f"原始对话导出：{len(by)} 个会话，写入 {wrote}，未变 {same}，删除 {gone}")

if __name__ == "__main__":
    main()
