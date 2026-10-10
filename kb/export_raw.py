#!/usr/bin/env python3
"""LLM 维基原始资料层：把 kb.sqlite 里的全部对话按会话导出成 Markdown → <raw_dir>/<来源>/<日期>_<会话>.md
内容没变的文件不重写（qmd update 据此只重扫变化的）。qmd 负责检索；这里只做格式转换。

10-10：只删除本脚本自己生成的文件（frontmatter 带 generator 标记，或旧版生成的 source/session/from/to 四项齐全的文件），
目录里别的 Markdown 一律不动（原来会把不在本次清单里的 .md 全删掉）。"""
import os, re, sys, sqlite3, collections

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "scripts"), os.path.expanduser("~/.hermes/scripts")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import kb_config

GENERATOR = "hermes-kb/export_raw"
CFG = kb_config.load()
DB = CFG.db
OUT = CFG.raw_dir
_LEGACY = os.path.join(CFG.wiki_dir, "raw", "conversations")      # 旧布局：raw 放在 wiki 下面
if not os.environ.get("RAW_DIR") and not os.path.exists(OUT) and os.path.exists(_LEGACY):
    OUT = _LEGACY


def is_ours(path):
    """是不是本脚本（新旧版本）生成的文件。"""
    try:
        head = open(path, encoding="utf-8", errors="ignore").read(600)
    except OSError:
        return False
    m = re.match(r"^---\n(.*?)\n---\n", head, re.S)
    if not m:
        return False
    fm = m.group(1)
    if f"generator: {GENERATOR}" in fm:
        return True
    return all(re.search(rf"^{k}: ", fm, re.M) for k in ("source", "session", "from", "to"))


def main():
    c = sqlite3.connect(DB)
    rows = c.execute(f"""select src, session, project, id, date, user, reply from chunks
                         where {kb_config.non_conversation_sql()} order by src, session, seq""").fetchall()
    by = collections.OrderedDict()
    for r in rows: by.setdefault((r[0], r[1]), []).append(r)
    wrote = same = 0; keep = set()
    for (src, session), rs in by.items():
        d0, d1, project = rs[0][4], rs[-1][4], next((r[2] for r in rs if r[2]), "")
        sid = re.sub(r"[^0-9A-Za-z-]", "", session)[:40]
        name = f"{d0}_{sid}.md"
        path = os.path.join(OUT, src, name); keep.add(path)
        body = [f"---\nsource: {src}\nsession: {session}\nproject: {project or '-'}\nfrom: {d0}\nto: {d1}\ngenerator: {GENERATOR}\n---\n",
                f"# {project or src} · {d0}" + (f" ~ {d1}" if d1 != d0 else "") + f"（{src}）\n"]
        for _, _, _, cid, date, user, reply in rs:
            body.append(f"\n## {date} · {cid}\n\n**他**：{user.strip()}\n")
            if reply.strip(): body.append(f"\n**agent**：{reply.strip()}\n")
        text = "".join(body)
        if os.path.exists(path) and open(path, encoding="utf-8").read() == text: same += 1; continue
        os.makedirs(os.path.dirname(path), exist_ok=True); open(path, "w", encoding="utf-8").write(text); wrote += 1
    gone = 0
    for root, _, fs in os.walk(OUT):
        for f in fs:
            p = os.path.join(root, f)
            if f.endswith(".md") and p not in keep and is_ours(p):
                os.remove(p); gone += 1
    print(f"原始对话导出：{len(by)} 个会话，写入 {wrote}，未变 {same}，删除 {gone}")


if __name__ == "__main__":
    main()
