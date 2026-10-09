#!/usr/bin/env python3
"""Hermes 通用对话知识库引擎：多 Agent 历史会话 → 一问一答切片 → SQLite 全文与混合检索。

使用命令：
  kb build              增量收录（按文件修改时间，只重做变动的会话）；--full 全量重建
  kb search 查询词 [-k 8] [--src 来源] [--project 项目名] [--since 日期]
                        返回最相关片段
  kb open <id> [--around 2]   查看该条一问一答全文及上下文
  kb stats              各来源片段数与日期范围

数据源适配：
  - 本地 Hermes 会话：~/.hermes/state.db 及 profiles/*/state.db
  - 本地/远程同步的 Claude Code 会话：~/.claude/ 或同步目录
  - 本地/远程同步的 Codex 会话：~/.codex/sessions/
  - 外部通用会话层：~/brain/raw/conversations/
  - 知识库已有页面：~/brain/wiki/*/*.md
"""
import os, re, sys, glob, json, math, time, sqlite3, hashlib, subprocess, datetime as dt
from urllib.parse import unquote

HOME = os.path.expanduser("~")
DB = os.environ.get("KB_DB_PATH", os.path.expanduser("~/brain/kb/kb.sqlite"))
SYNC = os.environ.get("KB_SYNC_DIR", os.path.expanduser("~/brain/raw/conversations"))
REPLY_MAX = 12000
NOISE = ("[Your previous response", "Current runtime context", "No response requested", "<turn_aborted", "[Request interrupted")
SYS_PREFIX = (
    '<command-', '<local-command', '<system-reminder', '<bash-', '<task-notification',
    '<user-prompt-submit', '<ide_', '[Request interrupted', 'This session is being continued',
    'Caveat: The messages below', '[System note', '[SYSTEM', '<environment_context',
    '<user_instructions', '# AGENTS.md', '<permissions'
)

# ---------------- 脱敏与清洗辅助 ----------------
def redact(s):
    if not s: return ""
    s = re.sub(r"(sk-[A-Za-z0-9_\-]{6})[A-Za-z0-9_\-]{10,}", r"\1***", s)
    s = re.sub(r"\b[0-9a-f]{32,}\b", "<hex>", s)
    s = re.sub(r"(?i)(password|passwd|secret|token|api[_-]?key|authorization)(\s*[:=]\s*|\s+bearer\s+)(\S+)", r"\1\2<redacted>", s)
    s = re.sub(r"eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}", "<jwt>", s)
    s = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----", "<privkey>", s)
    return s

def cut(s, n):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[:n] + "…"

def txt(c):
    if isinstance(c, str): return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text"))
    return ""

def iso2ep(s):
    try: return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception: return None

# ---------------- 切词 ----------------
CJK = r"㐀-鿿豈-﫿"
def toks(s):
    out = []
    for m in re.finditer(rf"[{CJK}]+|[A-Za-z0-9][A-Za-z0-9._\-]*", s or ""):
        w = m.group(0)
        if re.match(rf"[{CJK}]", w):
            out += [w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)]
        else:
            w = w.lower().strip("._-")
            if w: out.append(w)
    return out

# ---------------- 各通用来源解析器 ----------------
def ev_claude(f):
    for l in open(f, errors="ignore"):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("isSidechain"): continue
        m = d.get("message") or {}; c = m.get("content"); ts = iso2ep(d.get("timestamp"))
        if d.get("type") == "user":
            if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c): continue
            t = txt(c).strip()
            if t and not any(t.startswith(p) for p in SYS_PREFIX): yield ts, "user", t
        elif d.get("type") == "assistant" and isinstance(c, list):
            for x in c:
                if not isinstance(x, dict): continue
                if x.get("type") == "text" and x.get("text", "").strip(): yield ts, "reply", x["text"]
                elif x.get("type") == "tool_use":
                    i = x.get("input") or {}
                    yield ts, "cmd", str(i.get("command") or i.get("file_path") or i.get("description") or x.get("name"))

def ev_codex(f):
    for l in open(f, errors="ignore"):
        try: d = json.loads(l)
        except Exception: continue
        p = d.get("payload") or {}; ts = iso2ep(d.get("timestamp")); it = p.get("item") or {}
        if d.get("type") == "event_msg" and it.get("type") == "UserMessage":
            t = re.sub(r"\[@\w+\]\(plugin://[^)]*\)\s*", "", txt(it.get("content"))).strip()
            if t and not any(t.startswith(p) for p in SYS_PREFIX): yield ts, "user", t
        elif d.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "assistant":
            t = txt(p.get("content")).strip()
            if t: yield ts, "reply", t
        elif d.get("type") == "response_item" and p.get("type") == "function_call":
            try: a = json.loads(p.get("arguments") or "{}")
            except Exception: a = {}
            cmd = a.get("cmd") or a.get("command") or a.get("path") or p.get("name")
            yield ts, "cmd", " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)

def hermes_sessions(db):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True); out = {}
    for sid, role, content, tcs, ts in con.execute("select session_id, role, content, tool_calls, timestamp from messages order by id"):
        ev = out.setdefault(sid, [])
        if role == "user" and content and not any(str(content).startswith(p) for p in SYS_PREFIX):
            ev.append((ts, "user", str(content)))
        elif role == "assistant":
            if content and str(content).strip(): ev.append((ts, "reply", str(content)))
            try:
                for tc in json.loads(tcs or "[]") or []:
                    a = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                    ev.append((ts, "cmd", str(a.get("command") or a.get("path") or (tc.get("function") or {}).get("name"))))
            except Exception: pass
    return out

def ev_page(f, topic):
    ts = os.path.getmtime(f)
    for sec in re.split(r"(?m)^(?=## )", open(f, encoding="utf-8", errors="ignore").read()):
        title = sec.splitlines()[0].lstrip("#> ").strip() if sec.strip() else ""
        body = "\n".join(sec.splitlines()[1:]).strip()
        if body and body != "暂无":
            yield ts, "user", f"【整理页】{topic}｜{title}"
            yield ts, "reply", body

def sources():
    """生成所有待收录会话: (来源类型, 会话ID, 项目/归属, 文件或键, 修改时间, 事件生成器)"""
    g = lambda p, r=False: glob.glob(p, recursive=r)

    # 1. 本地及多 profile 的 Hermes 会话
    hermes_dbs = [("hermes-main", f"{HOME}/.hermes/state.db")]
    for p_dir in g(f"{HOME}/.hermes/profiles/*/"):
        p_name = os.path.basename(p_dir.rstrip("/"))
        p_db = os.path.join(p_dir, "state.db")
        if os.path.exists(p_db): hermes_dbs.append((f"hermes-{p_name}", p_db))

    for src_tag, db_path in hermes_dbs:
        if not os.path.exists(db_path): continue
        try:
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            sess_meta = {r[0]: (r[1] or "default", float(r[2] or 0)) for r in con.execute("select id, source, last_activity_at from sessions")}
            hs = hermes_sessions(db_path)
            for sid, (src_name, la) in sess_meta.items():
                if sid in hs:
                    yield src_tag, str(sid)[-8:], src_name, f"{db_path}:{sid}", la, lambda e=hs[sid]: iter(e)
        except Exception: pass

    # 2. Claude Code 会话
    for f in g(f"{HOME}/.claude/projects/*/*.jsonl") + g(f"{SYNC}/claude/**/*.jsonl", True):
        proj = os.path.basename(os.path.dirname(f))
        yield "claude", os.path.basename(f)[:8], proj, f, os.path.getmtime(f), lambda f=f: ev_claude(f)

    # 3. Codex 会话
    for f in g(f"{HOME}/.codex/sessions/**/*.jsonl", True) + g(f"{SYNC}/codex/**/*.jsonl", True):
        sid = os.path.basename(f)[:16]
        yield "codex", sid, "codex", f, os.path.getmtime(f), lambda f=f: ev_codex(f)

    # 4. 外部同步的通用 Raw 会话层
    for f in g(f"{SYNC}/**/*.jsonl", True):
        if "/claude/" in f or "/codex/" in f: continue
        rel = os.path.relpath(f, SYNC)
        src = rel.split(os.sep)[0]
        yield src, os.path.basename(f)[:12], src, f, os.path.getmtime(f), lambda f=f: ev_claude(f)

    # 5. 知识库已有 Wiki 页面
    for f in g(os.path.expanduser("~/brain/wiki/*/*.md")):
        if "/_archive/" in f or "/_meta/" in f: continue
        topic = os.path.basename(f)[:-3]
        yield "page", topic, topic, f, os.path.getmtime(f), lambda f=f, topic=topic: ev_page(f, topic)

# ---------------- 一问一答切片 ----------------
def norm(s): return re.sub(r"\s+", " ", s or "").strip()

def exchanges(evs):
    evs = [e for e in evs if e[2] and str(e[2]).strip() and not str(e[2]).lstrip().startswith(NOISE)]
    us = [i for i, e in enumerate(evs) if e[1] == "user"]
    for n, a in enumerate(us):
        b = us[n + 1] if n + 1 < len(us) else len(evs)
        mid = evs[a + 1:b]
        reply = "\n".join(e[2] for e in mid if e[1] == "reply").strip()
        cmds = [cut(e[2], 140) for e in mid if e[1] == "cmd"][:6]
        if len(reply) > REPLY_MAX: reply = reply[:REPLY_MAX - 4000] + "\n…（中间过程省略）…\n" + reply[-4000:]
        yield n, evs[a][0], evs[a][2], reply, cmds

# ---------------- 数据库管理 ----------------
def conn():
    os.makedirs(os.path.dirname(DB), exist_ok=True)
    c = sqlite3.connect(DB)
    c.executescript("""
    create table if not exists chunks(id text primary key, src text, session text, project text, ts real, date text,
        seq integer, user text, reply text, cmds text, h text unique);
    create index if not exists chunks_sess on chunks(src, session, seq);
    create virtual table if not exists fts using fts5(tok, content='', tokenize='unicode61 remove_diacritics 0');
    create table if not exists fts_map(rid integer primary key, id text unique);
    create table if not exists files(key text primary key, src text, session text, mtime real);
    """)
    return c

def drop_session(c, src, session):
    ids = [r[0] for r in c.execute("select id from chunks where src=? and session=?", (src, session))]
    for i in ids:
        r = c.execute("select rid from fts_map where id=?", (i,)).fetchone()
        if r:
            old = c.execute("select user, reply from chunks where id=?", (i,)).fetchone()
            if old:
                c.execute("insert into fts(fts, rowid, tok) values('delete', ?, ?)", (r[0], " ".join(toks(old[0] + " " + old[1]))))
            c.execute("delete from fts_map where rid=?", (r[0],))
    c.execute("delete from chunks where src=? and session=?", (src, session))

def build(full=False):
    c = conn()
    if full:
        c.executescript("delete from chunks; insert into fts(fts) values('delete-all'); delete from fts_map; delete from files;")
    seen = {r[0]: r[1] for r in c.execute("select key, mtime from files")}
    todo = sorted(sources(), key=lambda s: s[4])
    added = skipped = sess = 0
    for src, session, project, key, mtime, evf in todo:
        if seen.get(key) == mtime: continue
        drop_session(c, src, session); sess += 1
        for seq, ts, user, reply, cmds in exchanges(list(evf())):
            user, reply = redact(user).strip(), redact(reply)
            if len(user) < 2 and not reply: continue
            h = hashlib.sha1((norm(user)[:400] + "\x00" + norm(reply)[:400]).encode()).hexdigest()
            cid = f"{src}:{session}:{seq}"
            date = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            try:
                c.execute("insert into chunks values(?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, src, session, project, ts or 0, date, seq, user, reply, json.dumps([redact(x) for x in cmds], ensure_ascii=False), h))
            except sqlite3.IntegrityError:
                skipped += 1; continue
            rid = c.execute("insert into fts_map(id) values(?)", (cid,)).lastrowid
            c.execute("insert into fts(rowid, tok) values(?, ?)", (rid, " ".join(toks(user + " " + reply + " " + project))))
            added += 1
        c.execute("insert or replace into files values(?,?,?,?)", (key, src, session, mtime))
        if sess % 50 == 0: c.commit()
    c.commit()
    print(f"收录完成：处理 {sess} 个会话，新增片段 {added}，重复跳过 {skipped}")

# ---------------- 检索 ----------------
def _snip(text, qt, n):
    t = norm(text)
    pos = min([p for p in (t.lower().find(x) for x in qt) if p >= 0] or [0])
    a = max(0, pos - n // 3)
    return ("…" if a else "") + t[a:a + n] + ("…" if a + n < len(t) else "")

def search(q, k=8, src=None, project=None, since=None):
    c = conn()
    qt = list(dict.fromkeys(toks(q)))
    if not qt: return []
    match = " OR ".join('"' + t.replace('"', '') + '"' for t in qt)
    rows = c.execute("""select m.id, bm25(fts) from fts join fts_map m on m.rid = fts.rowid
                        where fts match ? order by bm25(fts) limit 400""", (match,)).fetchall()
    now = dt.datetime.now().timestamp(); out = []
    for cid, bm in rows:
        r = c.execute("select src, project, ts, date, user, reply from chunks where id=?", (cid,)).fetchone()
        if not r: continue
        s_, p_, ts, date, user, reply = r
        if src and s_ != src: continue
        if project and project not in (p_ or ""): continue
        if since and date < since: continue
        body = (user + " " + reply).lower()
        cover = sum(1 for t in qt if t in body) / len(qt)
        age = max(0, (now - (ts or 0)) / 86400)
        score = -bm * (0.5 + cover) * (1 + 0.5 * math.exp(-age / 60))
        out.append((score, cid, s_, p_, date, user, reply))
    out.sort(key=lambda x: -x[0])
    return out[:k]

def cmd_search(argv):
    k = int(argv[argv.index("-k") + 1]) if "-k" in argv else 8
    opt = lambda f: argv[argv.index(f) + 1] if f in argv else None
    skip = {i for f in ("-k", "--src", "--project", "--since") if f in argv for i in (argv.index(f), argv.index(f) + 1)}
    q = " ".join(a for i, a in enumerate(argv) if i not in skip).strip()
    res = search(q, k, opt("--src"), opt("--project"), opt("--since"))
    if not res: print("未检索到匹配内容。请调整关键词。"); return
    qt = [t.lower() for t in toks(q)]
    for n, (sc, cid, s_, p_, date, user, reply) in enumerate(res, 1):
        print(f"[{n}] {date} · {s_} · {p_ or '-'} · {cid}")
        print(f"    问：{_snip(user, qt, 150)}")
        if reply: print(f"    答：{_snip(reply, qt, 220)}")
    print("\n查看全文：kb open <id> [--around 2]")

def cmd_open(argv):
    cid = argv[0]; around = int(argv[argv.index("--around") + 1]) if "--around" in argv else 0
    c = conn()
    r = c.execute("select src, session, seq from chunks where id=?", (cid,)).fetchone()
    if not r: print("未找到该 ID 对应的记录"); return
    for x in c.execute("select id, date, project, user, reply, cmds from chunks where src=? and session=? and seq between ? and ? order by seq",
                       (r[0], r[1], r[2] - around, r[2] + around)):
        mark = "▶ " if x[0] == cid else ""
        print(f"===== {mark}{x[0]} · {x[1]} · {x[2] or '-'}\n问：{x[3]}\n")
        cm = json.loads(x[5] or "[]")
        if cm: print("命令调用：" + " | ".join(cm) + "\n")
        print(f"答：{x[4]}\n")

def cmd_stats():
    c = conn()
    for s, n, a, b in c.execute("select src, count(*), min(date), max(date) from chunks group by src order by src"):
        print(f"{s:16} {n:>7} 条  {a} ~ {b}")
    print("合计总片段数:", c.execute("select count(*) from chunks").fetchone()[0])

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit(0)
    if a[0] == "build": build("--full" in a)
    elif a[0] == "search": cmd_search(a[1:])
    elif a[0] == "open": cmd_open(a[1:])
    elif a[0] == "stats": cmd_stats()
    else: print(__doc__)
