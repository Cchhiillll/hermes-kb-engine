#!/usr/bin/env python3
"""chillwang 的对话知识库：所有 agent 的历史对话 → 一问一答切片 → SQLite 全文检索。给 Hermes 用。

  kb build              增量收录（按文件修改时间，只重做变过的会话）；--full 全量重建
  kb search 查询词 [-k 8] [--src mac-codex] [--project 名] [--since 2026-09-01]
                        返回最相关的片段：日期 · 来源 · 项目 · id，你说的 / 它答的 摘要
  kb open <id> [--around 2]   看这条一问一答的全文，以及同一会话前后各 N 轮
  kb stats              各来源片段数、最新日期

来源：Mac 同步来的 Claude Code / Codex / DSH / Grok / ZCode；ThinkPad 上的小火龙(openclaw)、Hermes(飞书)、佐佐木(Antigravity)、tp-claude、ZCode。
清理：去掉工具输出和系统提示，剥 cc-connect 外壳，遮密钥；同一问一答在多个会话里出现只收最早那份。
中文检索：CJK 按双字切词 + 英文数字按词，FTS5 BM25 排序，再按新旧加权。"""
import os, re, sys, glob, json, math, time, sqlite3, hashlib, subprocess, datetime as dt
from urllib.parse import unquote
sys.path.insert(0, os.path.expanduser("~/brain/tools"))
import archive as ar

HOME = os.path.expanduser("~")
DB = os.path.expanduser("~/brain/kb/kb.sqlite")
SYNC = ar.SYNC
ROOT = "/Users/wangyipeng/Documents/文稿 - 王一澎的笔记本电脑/AI Native/"
RANK = {"page": -1, "mac-claude": 0, "tp-claude": 1, "mac-dsh": 2, "mac-codex": 3, "tp-agy": 4, "tp-openclaw": 5, "tp-hermes": 6, "tp-sylphy": 7, "tp-roxy": 8, "tp-zcode": 9, "mac-grok": 10, "mac-zcode": 11}
REPLY_MAX = 12000          # 回复超长时保留开头 8000 + 结尾 4000（结论常在结尾）；10-01 由 2400 放宽
NOISE = ("[Your previous response", "Current runtime context", "No response requested", "<turn_aborted", "[Request interrupted")

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

# ---------------- 各来源 → (ts, kind, text) ----------------
def project_of(cwd):
    if cwd and cwd.startswith(ROOT): return cwd[len(ROOT):].split("/")[0] or ""
    return ""

def first_cwd(f, key):
    for l in open(f, errors="ignore"):
        try: d = json.loads(l)
        except Exception: continue
        c = d.get("cwd") if key == "claude" else ((d.get("payload") or {}).get("cwd") if d.get("type") == "session_meta" else None)
        if c: return c
    return ""

def ev_openclaw(f):
    for ts, k, t in ar.ev_openclaw(f):
        if k == "user": t = re.sub(r"^\[message_id:[^\]]*\]\s*", "", t); t = re.sub(r"^chill:\s*", "", t)
        yield ts, k, t

def ev_dsh(f):
    try: raw = subprocess.run(["zstd", "-dc", f], capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")
    except Exception: return
    for l in raw.splitlines():
        try: d = json.loads(l)
        except Exception: continue
        ts = (d.get("time") or 0) / 1000 or None; t = d.get("type"); data = d.get("data") or {}
        if t == "user/message":
            s = ar.txt(data.get("content")).strip()
            if s and not s.startswith(ar.SYS_PREFIX): yield ts, "user", s
        elif t == "assistant/message":
            for x in (data.get("message") or {}).get("content") or []:
                if isinstance(x, dict) and x.get("type") == "text" and x.get("text", "").strip(): yield ts, "reply", x["text"]
                elif isinstance(x, dict) and x.get("type") in ("toolCall", "tool_use"):
                    a = x.get("arguments") or x.get("input") or {}
                    yield ts, "cmd", str((a.get("command") if isinstance(a, dict) else None) or x.get("name"))

# Antigravity（佐佐木）：每个对话一个 SQLite，steps.step_payload 是 protobuf。没有 schema，按线格式把字符串抠出来。
def _pb(b, depth=0):
    i, n = 0, len(b)
    def varint():
        nonlocal i
        v = s = 0
        while i < n:
            c = b[i]; i += 1; v |= (c & 0x7f) << s; s += 7
            if c < 0x80: return v
        raise ValueError
    try:
        while i < n:
            key = varint(); wt = key & 7
            if wt == 0: yield "int", varint()
            elif wt == 1: i += 8
            elif wt == 5: i += 4
            elif wt == 2:
                ln = varint(); seg = b[i:i + ln]; i += ln
                if len(seg) != ln: return
                try:
                    s = seg.decode("utf-8")
                    if s and sum(ch.isprintable() or ch in "\n\t" for ch in s) / len(s) > 0.95: yield "str", s
                except UnicodeDecodeError: pass
                if depth < 8: yield from _pb(seg, depth + 1)
            else: return
    except (ValueError, IndexError): return

def _pb_time(b):
    for k, v in _pb(b or b""):
        if k == "int" and 1_700_000_000 < v < 1_900_000_000: return float(v)
    return None

def ev_agy(f):
    try: con = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
    except Exception: return
    last = os.path.getmtime(f)
    try: rows = con.execute("select idx, step_type, metadata, step_payload from steps order by idx").fetchall()
    except Exception: return
    for idx, st, meta, p in rows:
        if st not in (14, 15) or not p: continue
        ts = _pb_time(meta) or last
        strs = [s for k, s in _pb(p) if k == "str"]
        if st == 14:                     # 用户输入：cc-connect 外壳 + "User request:" 后面才是他的话
            u = [s for s in strs if "User request:" in s or "User message:" in s]
            if not u: continue
            t = re.split(r"User (?:request|message):", max(u, key=len))[-1]
            t = re.sub(r"^\s*\[cc-connect[^\]]*\]\s*", "", t).strip()
            if t: yield ts, "user", t
        else:                            # 模型输出：同一步里有中文回复和英文内部思考，取中文为主、不是工具参数的最长一段
            cj = lambda s: len(re.findall(rf"[{CJK}]", s))
            nat = [s for s in strs if len(s) >= 4 and cj(s) >= 0.2 * len(s) and not s.lstrip().startswith(("{", "["))]
            if nat: yield ts, "reply", max(nat, key=len)

def ev_page(f, topic):
    ts = os.path.getmtime(f)
    for sec in re.split(r"(?m)^(?=## )", open(f).read()):
        title = sec.splitlines()[0].lstrip("#> ").strip() if sec.strip() else ""
        body = "\n".join(sec.splitlines()[1:]).strip()
        if body and body != "暂无":
            yield ts, "user", f"【整理页】{topic}｜{title}"
            yield ts, "reply", body

def sources():
    """(来源, 会话, 项目, 文件或键, 修改时间, 事件生成器)"""
    g = lambda p, r=False: glob.glob(p, recursive=r)
    for f in g(f"{SYNC}/claude/projects/*/*.jsonl") + g(f"{SYNC}/claude/backups/*/*/*.jsonl"):
        yield "mac-claude", os.path.basename(f)[:8], project_of(first_cwd(f, "claude")), f, os.path.getmtime(f), lambda f=f: ar.ev_claude(f)
    for f in g(f"{SYNC}/codex/sessions/**/rollout-*.jsonl", True) + g(f"{SYNC}/codex/archived_sessions/**/*.jsonl", True):
        yield "mac-codex", os.path.basename(f)[28:64], project_of(first_cwd(f, "codex")), f, os.path.getmtime(f), lambda f=f: ar.ev_codex(f)
    for f in g(f"{SYNC}/dsh/sessions/**/session*.jsonl.zstd", True):
        if "session.v2" not in f and os.path.exists(f.replace("session.jsonl.zstd", "session.v2.jsonl.zstd")): continue
        m = re.search(r"session-([0-9a-f\-]{8})", f); d = os.path.basename(os.path.dirname(os.path.dirname(f)))
        proj = d.split("AI~0020Native-")[-1].strip("-") if "AI~0020Native-" in d else ""
        yield "mac-dsh", m.group(1) if m else f[-20:], proj, f, os.path.getmtime(f), lambda f=f: ev_dsh(f)
    for f in g(f"{HOME}/.claude/projects/*/*.jsonl"):
        yield "tp-claude", os.path.basename(f)[:8], "佐佐木/田山", f, os.path.getmtime(f), lambda f=f: ar.ev_claude(f)
    for f in g(f"{HOME}/.gemini/antigravity-cli/conversations/*.db"):
        yield "tp-agy", os.path.basename(f)[:8], "佐佐木(Antigravity)", f, os.path.getmtime(f), lambda f=f: ev_agy(f)
    for f in g(f"{HOME}/.openclaw/agents/main/sessions/*.jsonl*"):
        if ".trajectory" in f or f.endswith(".json"): continue
        agent = f.split("/agents/", 1)[1].split("/", 1)[0]   # 10-04：openclaw 多 agent，按 agent 标（main=小火龙，roxy=洛琪希）
        who = {"main": "小火龙(openclaw)", "roxy": "洛琪希(openclaw)"}.get(agent, f"{agent}(openclaw)")
        yield "tp-openclaw", os.path.basename(f)[:8], who, f, os.path.getmtime(f), lambda f=f: ev_openclaw(f)
    for f in g(os.path.expanduser("~/brain/wiki/*/*.md")):       # Hermes 维护的知识库页：每个小节一条，标题当“他”，内容当“答”
        if "/_archive/" in f: continue
        topic = os.path.basename(f)[:-3]
        yield "page", topic, topic, f, os.path.getmtime(f), lambda f=f, topic=topic: ev_page(f, topic)
    # Hermes 本体（飞书 + Discord，10-04 起收 Discord）和分身希露菲（Discord 文字/原语音模式）；定时任务、子代理、命令行不收
    # 洛琪希只收 Hermes 上的 Discord。小火龙目录里 10-04 下午那几段按当时的约定不收。
    for src, db, names in (("tp-hermes", f"{HOME}/.hermes/state.db", {"feishu": "Hermes(飞书)", "discord": "Hermes(Discord)"}),
                           ("tp-sylphy", f"{HOME}/.hermes/profiles/sylphy/state.db", {"discord": "希露菲(Discord)"}),
                           ("tp-roxy", f"{HOME}/.hermes/profiles/roxy/state.db", {"discord": "洛琪希(Discord)"})):
        if not os.path.exists(db): continue
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        keep = {r[0]: (r[1], r[2] or 0) for r in con.execute(
            "select id, source, last_activity_at from sessions where source in (%s)" % ",".join("?" * len(names)), list(names))}
        hs = hermes_sessions(db)
        pre = {"tp-hermes": "hermes:", "tp-sylphy": "sylphy:", "tp-roxy": "roxy:"}.get(src, src + ":")
        for sid, (source, la) in keep.items():
            if sid in hs: yield src, str(sid)[-8:], names[source], pre + sid, float(la), lambda e=hs[sid]: iter(e)
    # 希露菲实时通话（Gemini Live 插件逐段记的双方原话，一通电话一个文件；刚改动 2 分钟内的可能还在通话，下次再收）
    for f in g(f"{HOME}/.hermes/profiles/sylphy/voice-live-notes/*.jsonl"):
        mt = os.path.getmtime(f)
        if time.time() - mt < 120: continue
        yield "tp-sylphy", "call-" + os.path.basename(f)[11:26], "希露菲(实时通话)", f, mt, lambda f=f: ev_voice_live(f)
    zdb = f"{HOME}/.zcode/cli/db/db.sqlite"
    if os.path.exists(zdb):
        zc = sqlite3.connect(f"file:{zdb}?mode=ro", uri=True)
        for sid, updated in zc.execute("select id, time_updated from session"):
            mt = float(updated) / 1000.0 if updated and float(updated) > 1e12 else float(updated or 0)
            short = sid.replace("sess_", "")[:8]
            yield "tp-zcode", short, "ZCode", zdb + ":" + sid, mt, lambda db=zdb, sid=sid: ev_zcode(db, sid)

    for f in g(f"{SYNC}/grok/**/updates.jsonl", True):
        sid = os.path.basename(os.path.dirname(f))
        parent = os.path.basename(os.path.dirname(os.path.dirname(f)))
        yield "mac-grok", sid[:36], project_of(unquote(parent)) or "Grok", f, os.path.getmtime(f), lambda f=f: ev_grok(f)
    mz = f"{SYNC}/zcode/db.sqlite"
    if os.path.exists(mz):
        mc = sqlite3.connect(f"file:{mz}?mode=ro", uri=True)
        for sid, directory, updated in mc.execute("select id, directory, time_updated from session"):
            mt = float(updated) / 1000.0 if updated and float(updated) > 1e12 else float(updated or 0)
            short = sid.replace("sess_", "")[:8]
            yield "mac-zcode", short, project_of(directory or "") or "ZCode", mz + ":" + sid, mt, lambda db=mz, sid=sid: ev_zcode(db, sid)

def ev_zcode(db, sid):
    """ZCode 本地库：只收用户和助手的正文，不收工具调用和思考过程。时间是毫秒。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select m.id, m.time_created, json_extract(m.data, '$.role'), p.data "
        "from message m join part p on p.message_id = m.id "
        "where m.session_id=? and json_extract(p.data, '$.type')='text' "
        "order by m.time_created, m.sequence, p.sequence", (sid,))
    msgs = {}
    order = []
    for mid, ts, role, pdata in rows:
        if mid not in msgs:
            msgs[mid] = [ts, role, []]
            order.append(mid)
        try:
            text = json.loads(pdata).get("text") or ""
        except Exception:
            continue
        if str(text).strip():
            msgs[mid][2].append(str(text))
    for mid in order:
        ts, role, parts = msgs[mid]
        kind = {"user": "user", "assistant": "reply"}.get(role)
        text = "\n".join(parts).strip()
        if not kind or not text:
            continue
        t = float(ts) / 1000.0 if ts and float(ts) > 1e12 else float(ts or 0)
        yield t, kind, text

def ev_grok(f):
    """Grok 命令行完整记录：只拼用户和助手的正文。工具、思考、系统提示不收。"""
    out = []
    cur = None
    buf = []
    ts0 = None
    def flush():
        nonlocal cur, buf, ts0
        if cur and buf:
            text = "".join(buf).strip()
            if text:
                out.append((ts0 or 0, cur, text))
        buf = []
        cur = None
        ts0 = None
    for line in open(f, encoding="utf-8"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        u = (d.get("params") or {}).get("update") or {}
        role = {"user_message_chunk": "user", "agent_message_chunk": "reply"}.get(u.get("sessionUpdate"))
        if not role:
            continue
        c = u.get("content") or {}
        text = c.get("text") if isinstance(c, dict) else (c if isinstance(c, str) else "")
        if role != cur:
            flush()
            cur = role
            ts0 = d.get("timestamp") or 0
        if text:
            buf.append(text)
    flush()
    for item in out:
        yield item

def hermes_sessions(db):
    """和 archive.hermes_sessions 同口径，只是库路径可指定（分身各有自己的 state.db）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True); out = {}
    for sid, role, content, tcs, ts in con.execute("select session_id, role, content, tool_calls, timestamp from messages order by id"):
        ev = out.setdefault(sid, [])
        if role == "user" and content and not str(content).startswith(ar.SYS_PREFIX): ev.append((ts, "user", str(content)))
        elif role == "assistant":
            if content and str(content).strip(): ev.append((ts, "reply", str(content)))
            try:
                for tc in json.loads(tcs or "[]") or []:
                    a = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                    ev.append((ts, "cmd", str(a.get("command") or a.get("path") or (tc.get("function") or {}).get("name"))))
            except Exception: pass
    return out

_CJK_SP = re.compile(r"(?<=[\u3000-\u9fff\uff00-\uffef])\s+(?=[\u3000-\u9fff\uff00-\uffef])")

def ev_voice_live(f):
    """逐段记录 → 合并成一句一句：连续同一方的片段拼起来；输入转写的字间空格去掉。"""
    turns = []
    for line in open(f, encoding="utf-8"):
        try: d = json.loads(line)
        except Exception: continue
        role = {"input": "user", "output": "reply"}.get(d.get("direction"))
        text = d.get("text") or ""
        if not role or not text.strip(): continue
        try: ts = dt.datetime.fromisoformat(d["ts"].replace("Z", "+00:00")).timestamp()
        except Exception: ts = os.path.getmtime(f)
        if turns and turns[-1][1] == role: turns[-1][2] += text
        else: turns.append([ts, role, text])
    for ts, role, text in turns:
        yield ts, role, _CJK_SP.sub("", text).strip()

# ---------------- 一问一答切片 ----------------
def norm(s): return re.sub(r"\s+", " ", s or "").strip()

def exchanges(evs):
    evs = [e for e in evs if e[2] and str(e[2]).strip() and not str(e[2]).lstrip().startswith(NOISE)]
    us = [i for i, e in enumerate(evs) if e[1] == "user"]
    for n, a in enumerate(us):
        b = us[n + 1] if n + 1 < len(us) else len(evs)
        mid = evs[a + 1:b]
        reply = "\n".join(e[2] for e in mid if e[1] == "reply").strip()
        cmds = [ar.cut(e[2], 140) for e in mid if e[1] == "cmd"][:6]
        if len(reply) > REPLY_MAX: reply = reply[:REPLY_MAX - 4000] + "\n…（中间过程省略）…\n" + reply[-4000:]
        yield n, evs[a][0], evs[a][2], reply, cmds

# ---------------- 库 ----------------
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
    create table if not exists vecs(id text primary key, v blob);
    """)
    return c

def drop_session(c, src, session):
    ids = [r[0] for r in c.execute("select id from chunks where src=? and session=?", (src, session))]
    for i in ids:
        r = c.execute("select rid from fts_map where id=?", (i,)).fetchone()
        if r:
            old = c.execute("select user, reply from chunks where id=?", (i,)).fetchone()
            c.execute("insert into fts(fts, rowid, tok) values('delete', ?, ?)", (r[0], " ".join(toks(old[0] + " " + old[1]))))
            c.execute("delete from fts_map where rid=?", (r[0],))
        c.execute("delete from vecs where id=?", (i,))
    c.execute("delete from chunks where src=? and session=?", (src, session))

def build(full=False):
    c = conn()
    if full:
        c.executescript("delete from chunks; insert into fts(fts) values('delete-all'); delete from fts_map; delete from files; delete from vecs;")
    seen = {r[0]: r[1] for r in c.execute("select key, mtime from files")}
    todo = sorted(sources(), key=lambda s: (RANK.get(s[0], 9), s[4]))
    added = skipped = sess = 0
    for src, session, project, key, mtime, evf in todo:
        if seen.get(key) == mtime: continue
        drop_session(c, src, session); sess += 1
        for seq, ts, user, reply, cmds in exchanges(list(evf())):
            user, reply = ar.redact(user).strip(), ar.redact(reply)
            if len(user) < 2 and not reply: continue
            h = hashlib.sha1((norm(user)[:400] + "\x00" + norm(reply)[:400]).encode()).hexdigest()
            cid = f"{src}:{session}:{seq}"
            date = dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""
            try:
                c.execute("insert into chunks values(?,?,?,?,?,?,?,?,?,?,?)",
                          (cid, src, session, project, ts or 0, date, seq, user, reply, json.dumps([ar.redact(x) for x in cmds], ensure_ascii=False), h))
            except sqlite3.IntegrityError:
                skipped += 1; continue
            rid = c.execute("insert into fts_map(id) values(?)", (cid,)).lastrowid
            c.execute("insert into fts(rowid, tok) values(?, ?)", (rid, " ".join(toks(user + " " + reply + " " + project))))
            added += 1
        c.execute("insert or replace into files values(?,?,?,?)", (key, src, session, mtime))
        if sess % 50 == 0: c.commit()
    c.commit()
    print(f"收录：处理 {sess} 个会话，新增片段 {added}，重复跳过 {skipped}")

# ---------------- 语义向量（本机 embeddinggemma-300m，经 node-llama-cpp） ----------------
NODE = os.path.expanduser("~/.local/openclaw-node-v22/bin/node")
EMBED_JS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embed.mjs")

def _embed(texts):
    """[(id, text)] → {id: 单位化 float32 向量}。先找常驻服务（kb-embed.service），不在就临时起一个进程"""
    import numpy as np
    if len(texts) <= 20:
        try:
            import urllib.request
            req = urllib.request.Request("http://127.0.0.1:18997/embed", data=json.dumps({"texts": [t for _, t in texts]}).encode(),
                                         headers={"Content-Type": "application/json"})
            vs = json.load(urllib.request.urlopen(req, timeout=20))["vectors"]
            out = {}
            for (i, _), v in zip(texts, vs):
                v = np.asarray(v, dtype=np.float32); out[i] = v / (np.linalg.norm(v) or 1)
            return out
        except Exception:
            pass
    p = subprocess.Popen([NODE, EMBED_JS], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    import threading
    def feed():                                  # 另起线程写入，避免 stdin/stdout 管道互相塞满卡死
        for i, t in texts: p.stdin.write(json.dumps({"id": i, "text": t}, ensure_ascii=False) + "\n")
        p.stdin.close()
    threading.Thread(target=feed, daemon=True).start(); out = {}
    for l in p.stdout:
        d = json.loads(l); v = np.asarray(d["v"], dtype=np.float32); out[d["id"]] = v / (np.linalg.norm(v) or 1)
    p.wait(); return out

def doc_text(project, user, reply):
    return f"title: {project or 'none'} | text: {norm(user)[:300]}\n{norm(reply)[:700]}"

def embed_missing(batch=200):
    c = conn(); done = 0
    while True:
        rows = c.execute("select id, project, user, reply from chunks where id not in (select id from vecs) limit ?", (batch,)).fetchall()
        if not rows: break
        vs = _embed([(r[0], doc_text(r[1], r[2], r[3])) for r in rows])
        c.executemany("insert or replace into vecs values(?, ?)", [(i, v.tobytes()) for i, v in vs.items()])
        c.commit(); done += len(vs)
        if len(vs) < len(rows): break
    print(f"向量：新增 {done}，共 {c.execute('select count(*) from vecs').fetchone()[0]}")

def vsearch(q, n=50):
    import numpy as np
    c = conn(); rows = c.execute("select id, v from vecs").fetchall()
    if not rows: return []
    qv = _embed([(0, f"task: search result | query: {q}")]).get(0)
    if qv is None: return []
    m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(len(rows), -1)
    sims = m @ qv; top = np.argsort(-sims)[:n]
    return [(rows[i][0], float(sims[i])) for i in top]

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
        cover = sum(1 for t in qt if t in body) / len(qt)            # 查询词覆盖率
        age = max(0, (now - (ts or 0)) / 86400)
        score = -bm * (0.5 + cover) * (1 + 0.5 * math.exp(-age / 60))
        out.append((score, cid, s_, p_, date, user, reply))
    out.sort(key=lambda x: -x[0])
    return out[:k]

def hybrid(q, k=8, src=None, project=None, since=None):
    """关键词 + 语义两路，按名次倒数融合（RRF），同分时新的在前"""
    kw = search(q, 60, src, project, since)
    try: vs = vsearch(q, 60)
    except Exception: vs = []
    c = conn(); score = {}; info = {x[1]: x for x in kw}
    for r, x in enumerate(kw): score[x[1]] = score.get(x[1], 0) + 1 / (60 + r)
    now = dt.datetime.now().timestamp()
    for r, (cid, sim) in enumerate(vs):
        if cid not in info:
            row = c.execute("select src, project, ts, date, user, reply from chunks where id=?", (cid,)).fetchone()
            if not row: continue
            s_, p_, ts, date, user, reply = row
            if (src and s_ != src) or (project and project not in (p_ or "")) or (since and date < since): continue
            info[cid] = (0, cid, s_, p_, date, user, reply)
        score[cid] = score.get(cid, 0) + 1 / (60 + r)
    def fresh(cid):
        ts = c.execute("select ts from chunks where id=?", (cid,)).fetchone()[0] or 0
        return 1 + 0.15 * math.exp(-max(0, now - ts) / 86400 / 60)
    ranked, seen = [], set()
    for i in sorted(score, key=lambda i: -score[i] * fresh(i)):
        d = (norm(info[i][5])[:120], info[i][4])          # 同一天同一句话（Claude/Codex 各存一份）只留一条
        if d in seen: continue
        if info[i][2] == "page" and sum(info[j][2] == "page" for j in ranked) >= 2: continue   # 整理页最多占 2 条
        seen.add(d); ranked.append(i)
        if len(ranked) >= k: break
    return [(score[i],) + tuple(info[i][1:]) for i in ranked]

def cmd_search(argv):
    k = int(argv[argv.index("-k") + 1]) if "-k" in argv else 8
    opt = lambda f: argv[argv.index(f) + 1] if f in argv else None
    skip = {i for f in ("-k", "--src", "--project", "--since") if f in argv for i in (argv.index(f), argv.index(f) + 1)}
    q = " ".join(a for i, a in enumerate(argv) if i not in skip)
    q = q.replace("--kw", "").strip()
    res = (search if "--kw" in argv else hybrid)(q, k, opt("--src"), opt("--project"), opt("--since"))
    if not res: print("没搜到。换个说法，或少用几个词。"); return
    qt = [t.lower() for t in toks(q)]
    for n, (sc, cid, s_, p_, date, user, reply) in enumerate(res, 1):
        print(f"[{n}] {date} · {s_} · {p_ or '-'} · {cid}")
        print(f"    他：{_snip(user, qt, 150)}")
        if reply: print(f"    答：{_snip(reply, qt, 220)}")
    print("\n看全文：kb open <id> [--around 2]")

def cmd_open(argv):
    cid = argv[0]; around = int(argv[argv.index("--around") + 1]) if "--around" in argv else 0
    c = conn()
    r = c.execute("select src, session, seq from chunks where id=?", (cid,)).fetchone()
    if not r: print("没有这个 id"); return
    for x in c.execute("select id, date, project, user, reply, cmds from chunks where src=? and session=? and seq between ? and ? order by seq",
                       (r[0], r[1], r[2] - around, r[2] + around)):
        mark = "▶ " if x[0] == cid else ""
        print(f"===== {mark}{x[0]} · {x[1]} · {x[2] or '-'}\n他：{x[3]}\n")
        cm = json.loads(x[5] or "[]")
        if cm: print("它执行：" + " | ".join(cm) + "\n")
        print(f"它答：{x[4]}\n")

def cmd_stats():
    c = conn()
    for s, n, a, b in c.execute("select src, count(*), min(date), max(date) from chunks group by src order by src"):
        print(f"{s:12} {n:>7} 条  {a} ~ {b}")
    print("合计", c.execute("select count(*) from chunks").fetchone()[0])

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit(0)
    if a[0] == "build":
        build("--full" in a)
        if "--no-embed" not in a: embed_missing()
    elif a[0] == "search": cmd_search(a[1:])
    elif a[0] == "open": cmd_open(a[1:])
    elif a[0] == "stats": cmd_stats()
    else: print(__doc__)
