#!/usr/bin/env python3
"""Hermes 对话知识库引擎：多 Agent 历史对话 → 一问一答切片 → SQLite 全文检索与向量检索。

生产 1:1 完整实现，自包含所有数据源解析器，不依赖外部未打包模块。

来源支持：
  1. Mac 远程同步端 (~/mac_agent_sync/)：
     - Claude Code: ~/.claude/projects/*/*.jsonl
     - Codex: ~/.codex/sessions/**/rollout-*.jsonl 及 archived_sessions/**/*.jsonl
     - DSH: ~/.dsh/sessions/**/session*.jsonl.zstd (zstd 深度解压)
     - Grok: ~/.grok/sessions/**/updates.jsonl
     - ZCode: Mac 本地命令行会话库快照 (zcode/db.sqlite)
  2. 本地 Agent 端：
     - 小火龙 (OpenClaw): ~/.openclaw/agents/main/sessions/*.jsonl* (清洗 message_id 与 agent 前缀)
     - 佐佐木 (Antigravity): ~/.gemini/antigravity-cli/conversations/*.db (无 schema SQLite 原生 protobuf 线格式解析)
     - Hermes 本体及多 Profile: ~/.hermes/state.db 及 profiles/*/state.db (飞书/Discord/实时通话)
     - 工作 Agent: tp-claude, tp-zcode
  3. 知识库现有页面: ~/brain/wiki/*/*.md

使用方法：
  kb build [--full] [--no-embed]   增量收录变动会话；--full 全量重建
  kb search 查询词 [-k 8]          检索一问一答切片
  kb open <id> [--around 2]        查看切片全文及上下文
  kb stats                         查看各来源片段统计
"""
import os, re, sys, glob, json, math, time, sqlite3, hashlib, subprocess, datetime as dt
from urllib.parse import unquote

HOME = os.path.expanduser("~")
DB = os.environ.get("KB_DB_PATH", os.path.expanduser("~/brain/kb/kb.sqlite"))
SYNC = os.environ.get("MAC_SYNC_DIR", os.path.expanduser("~/mac_agent_sync"))
ROOT = "/Users/wangyipeng/Documents/文稿 - 王一澎的笔记本电脑/AI Native/"
RANK = {
    "page": -1, "mac-claude": 0, "tp-claude": 1, "mac-dsh": 2, "mac-codex": 3,
    "tp-agy": 4, "tp-openclaw": 5, "tp-hermes": 6, "tp-sylphy": 7, "tp-roxy": 8,
    "tp-zcode": 9, "mac-grok": 10, "mac-zcode": 11
}
REPLY_MAX = 12000
NOISE = ("[Your previous response", "Current runtime context", "No response requested", "<turn_aborted", "[Request interrupted")
SYS_PREFIX = (
    "<command-", "<local-command", "<system-reminder", "<bash-", "<task-notification",
    "<user-prompt-submit", "<ide_", "[Request interrupted", "This session is being continued",
    "Caveat: The messages below", "[System note", "[SYSTEM", "<environment_context",
    "<user_instructions", "# AGENTS.md", "<permissions"
)

# ---------------- 基础清洗、脱敏与辅助函数 ----------------
def iso2ep(s):
    try: return dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception: return None

def txt(c):
    if isinstance(c, str): return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text"))
    return ""

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

def unwrap(t):
    """佐佐木 (cc-connect) 外壳剥除：真话在最后一个 'User message:' 之后。"""
    if "cc-connect" in t and "User message:" in t:
        t = re.sub(r"^\s*\[cc-connect[^\]]*\]\s*", "", t.rsplit("User message:", 1)[1])
    return t.strip()

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

# ---------------- 各来源解析器 ----------------
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

def ev_claude(f):
    for l in open(f, errors="ignore"):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("isSidechain"): continue
        m = d.get("message") or {}; c = m.get("content"); ts = iso2ep(d.get("timestamp"))
        if d.get("type") == "user":
            if isinstance(c, list) and any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c): continue
            t = unwrap(txt(c))
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

def ev_openclaw(f):
    for l in open(f, errors="ignore"):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("type") != "message": continue
        m = d.get("message") or {}; ts = d.get("timestamp")
        ts = (ts / 1000 if ts > 1e11 else ts) if isinstance(ts, (int, float)) else iso2ep(ts)
        if m.get("role") == "user":
            t = txt(m.get("content")).strip()
            t = re.sub(r"^\[message_id:[^\]]*\]\s*", "", t)
            t = re.sub(r"^chill:\s*", "", t)
            if t and not any(t.startswith(p) for p in SYS_PREFIX): yield ts, "user", t
        elif m.get("role") == "assistant" and isinstance(m.get("content"), list):
            for x in m["content"]:
                if not isinstance(x, dict): continue
                if x.get("type") == "text" and x.get("text", "").strip(): yield ts, "reply", x["text"]
                elif x.get("type") == "toolCall":
                    a = x.get("arguments") if isinstance(x.get("arguments"), dict) else {}
                    yield ts, "cmd", str(a.get("command") or a.get("path") or x.get("name"))

def ev_dsh(f):
    try: raw = subprocess.run(["zstd", "-dc", f], capture_output=True, timeout=120).stdout.decode("utf-8", "ignore")
    except Exception: return
    for l in raw.splitlines():
        try: d = json.loads(l)
        except Exception: continue
        ts = (d.get("time") or 0) / 1000 or None; t = d.get("type"); data = d.get("data") or {}
        if t == "user/message":
            s = txt(data.get("content")).strip()
            if s and not any(s.startswith(p) for p in SYS_PREFIX): yield ts, "user", s
        elif t == "assistant/message":
            for x in (data.get("message") or {}).get("content") or []:
                if isinstance(x, dict) and x.get("type") == "text" and x.get("text", "").strip(): yield ts, "reply", x["text"]
                elif isinstance(x, dict) and x.get("type") in ("toolCall", "tool_use"):
                    a = x.get("arguments") or x.get("input") or {}
                    yield ts, "cmd", str((a.get("command") if isinstance(a, dict) else None) or x.get("name"))

# Antigravity（佐佐木）：SQLite 内 steps.step_payload 原生 protobuf 线格式解析
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
        if st == 14:
            u = [s for s in strs if "User request:" in s or "User message:" in s]
            if not u: continue
            t = re.split(r"User (?:request|message):", max(u, key=len))[-1]
            t = re.sub(r"^\s*\[cc-connect[^\]]*\]\s*", "", t).strip()
            if t: yield ts, "user", t
        else:
            cj = lambda s: len(re.findall(rf"[{CJK}]", s))
            nat = [s for s in strs if len(s) >= 4 and cj(s) >= 0.2 * len(s) and not s.lstrip().startswith(("{", "["))]
            if nat: yield ts, "reply", max(nat, key=len)

def ev_zcode(db, sid):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    except Exception:
        try:
            con = sqlite3.connect(db, timeout=10)
        except Exception:
            return
    try:
        rows = con.execute(
            "select m.id, m.time_created, json_extract(m.data, '$.role'), p.data "
            "from message m join part p on p.message_id = m.id "
            "where m.session_id=? and json_extract(p.data, '$.type')='text' "
            "order by m.time_created, m.sequence, p.sequence", (sid,)).fetchall()
    except Exception:
        return
    msgs = {}; order = []
    for mid, ts, role, pdata in rows:
        if mid not in msgs:
            msgs[mid] = [ts, role, []]; order.append(mid)
        try: text = json.loads(pdata).get("text") or ""
        except Exception: continue
        if str(text).strip(): msgs[mid][2].append(str(text))
    for mid in order:
        ts, role, parts = msgs[mid]
        kind = {"user": "user", "assistant": "reply"}.get(role)
        text = "\n".join(parts).strip()
        if not kind or not text: continue
        t = float(ts) / 1000.0 if ts and float(ts) > 1e12 else float(ts or 0)
        yield t, kind, text

def ev_grok(f):
    out = []; cur = None; buf = []; ts0 = None
    def flush():
        nonlocal cur, buf, ts0
        if cur and buf:
            text = "".join(buf).strip()
            if text: out.append((ts0 or 0, cur, text))
        buf = []; cur = None; ts0 = None
    for line in open(f, encoding="utf-8"):
        try: d = json.loads(line)
        except Exception: continue
        u = (d.get("params") or {}).get("update") or {}
        role = {"user_message_chunk": "user", "agent_message_chunk": "reply"}.get(u.get("sessionUpdate"))
        if not role: continue
        c = u.get("content") or {}
        text = c.get("text") if isinstance(c, dict) else (c if isinstance(c, str) else "")
        if role != cur:
            flush(); cur = role; ts0 = d.get("timestamp") or 0
        if text: buf.append(text)
    flush()
    for item in out: yield item

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

_CJK_SP = re.compile(r"(?<=[\u3000-\u9fff\uff00-\uffef])\s+(?=[\u3000-\u9fff\uff00-\uffef])")
def ev_voice_live(f):
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

def ev_page(f, topic):
    ts = os.path.getmtime(f)
    for sec in re.split(r"(?m)^(?=## )", open(f, encoding="utf-8", errors="ignore").read()):
        title = sec.splitlines()[0].lstrip("#> ").strip() if sec.strip() else ""
        body = "\n".join(sec.splitlines()[1:]).strip()
        if body and body != "暂无":
            yield ts, "user", f"【整理页】{topic}｜{title}"
            yield ts, "reply", body

def sources():
    """生成所有待收录会话: (来源, 会话, 项目, 文件或键, 修改时间, 事件生成器)"""
    g = lambda p, r=False: glob.glob(p, recursive=r)

    # 1. 扫描 Claude Code 会话（同时支持本地宿主目录与同步目录）
    claude_pats = [f"{HOME}/.claude/projects/*/*.jsonl", f"{HOME}/.claude/backups/*/*/*.jsonl",
                   f"{SYNC}/claude/projects/*/*.jsonl", f"{SYNC}/claude/backups/*/*/*.jsonl"]
    for pat in claude_pats:
        for f in g(pat):
            yield "mac-claude", os.path.basename(f)[:8], project_of(first_cwd(f, "claude")), f, os.path.getmtime(f), lambda f=f: ev_claude(f)

    # 2. 扫描 Codex 会话（同时支持本地宿主目录与同步目录）
    codex_pats = [f"{HOME}/.codex/sessions/**/rollout-*.jsonl", f"{HOME}/.codex/archived_sessions/**/*.jsonl",
                  f"{SYNC}/codex/sessions/**/rollout-*.jsonl", f"{SYNC}/codex/archived_sessions/**/*.jsonl"]
    for pat in codex_pats:
        for f in g(pat, True):
            yield "mac-codex", os.path.basename(f)[28:64], project_of(first_cwd(f, "codex")), f, os.path.getmtime(f), lambda f=f: ev_codex(f)

    # 3. 扫描 DSH 会话（同时支持本地宿主目录与同步目录）
    dsh_pats = [f"{HOME}/.dsh/sessions/**/session*.jsonl.zstd", f"{SYNC}/dsh/sessions/**/session*.jsonl.zstd"]
    for pat in dsh_pats:
        for f in g(pat, True):
            if "session.v2" not in f and os.path.exists(f.replace("session.jsonl.zstd", "session.v2.jsonl.zstd")): continue
            m = re.search(r"session-([0-9a-f\-]{8})", f); d = os.path.basename(os.path.dirname(os.path.dirname(f)))
            proj = d.split("AI~0020Native-")[-1].strip("-") if "AI~0020Native-" in d else ""
            yield "mac-dsh", m.group(1) if m else f[-20:], proj, f, os.path.getmtime(f), lambda f=f: ev_dsh(f)

    # 4. 扫描 Grok 会话（同时支持本地宿主目录与同步目录）
    grok_pats = [f"{HOME}/.grok/sessions/**/updates.jsonl", f"{SYNC}/grok/**/updates.jsonl"]
    for pat in grok_pats:
        for f in g(pat, True):
            sid = os.path.basename(os.path.dirname(f))
            parent = os.path.basename(os.path.dirname(os.path.dirname(f)))
            yield "mac-grok", sid[:36], project_of(unquote(parent)) or "Grok", f, os.path.getmtime(f), lambda f=f: ev_grok(f)

    # 5. 扫描 ZCode 会话库（同时支持本地宿主目录与同步目录）
    zcode_dbs = [f"{HOME}/.zcode/cli/db/db.sqlite", f"{SYNC}/zcode/db.sqlite"]
    seen_zcode = set()
    for zdb in zcode_dbs:
        if not os.path.exists(zdb): continue
        try:
            zc = sqlite3.connect(f"file:{zdb}?mode=ro", uri=True, timeout=10)
            for sid, directory, updated in zc.execute("select id, directory, time_updated from session"):
                if sid in seen_zcode: continue
                seen_zcode.add(sid)
                mt = float(updated) / 1000.0 if updated and float(updated) > 1e12 else float(updated or 0)
                short = sid.replace("sess_", "")[:8]
                yield "mac-zcode", short, project_of(directory or "") or "ZCode", zdb + ":" + sid, mt, lambda db=zdb, sid=sid: ev_zcode(db, sid)
        except Exception: pass

    # 2. 本地 Agent 端
    for f in g(f"{HOME}/.claude/projects/*/*.jsonl"):
        yield "tp-claude", os.path.basename(f)[:8], "佐佐木/田山", f, os.path.getmtime(f), lambda f=f: ev_claude(f)
    for f in g(f"{HOME}/.gemini/antigravity-cli/conversations/*.db"):
        yield "tp-agy", os.path.basename(f)[:8], "佐佐木(Antigravity)", f, os.path.getmtime(f), lambda f=f: ev_agy(f)
    for f in g(f"{HOME}/.openclaw/agents/main/sessions/*.jsonl*"):
        if ".trajectory" in f or f.endswith(".json"): continue
        agent = f.split("/agents/", 1)[1].split("/", 1)[0]
        who = {"main": "小火龙(openclaw)", "roxy": "洛琪希(openclaw)"}.get(agent, f"{agent}(openclaw)")
        yield "tp-openclaw", os.path.basename(f)[:8], who, f, os.path.getmtime(f), lambda f=f: ev_openclaw(f)

    # 3. Hermes 本体及多 Profile
    for src, db, names in (("tp-hermes", f"{HOME}/.hermes/state.db", {"feishu": "Hermes(飞书)", "discord": "Hermes(Discord)"}),
                           ("tp-sylphy", f"{HOME}/.hermes/profiles/sylphy/state.db", {"discord": "希露菲(Discord)"}),
                           ("tp-roxy", f"{HOME}/.hermes/profiles/roxy/state.db", {"discord": "洛琪希(Discord)"})):
        if not os.path.exists(db): continue
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            keep = {r[0]: (r[1], r[2] or 0) for r in con.execute(
                "select id, source, last_activity_at from sessions where source in (%s)" % ",".join("?" * len(names)), list(names))}
            hs = hermes_sessions(db)
            pre = {"tp-hermes": "hermes:", "tp-sylphy": "sylphy:", "tp-roxy": "roxy:"}.get(src, src + ":")
            for sid, (source, la) in keep.items():
                if sid in hs: yield src, str(sid)[-8:], names[source], pre + sid, float(la), lambda e=hs[sid]: iter(e)
        except Exception: pass

    # 4. 实时通话
    for f in g(f"{HOME}/.hermes/profiles/sylphy/voice-live-notes/*.jsonl"):
        mt = os.path.getmtime(f)
        if time.time() - mt < 120: continue
        yield "tp-sylphy", "call-" + os.path.basename(f)[11:26], "希露菲(实时通话)", f, mt, lambda f=f: ev_voice_live(f)

    # 5. 本地 ZCode
    zdb = f"{HOME}/.zcode/cli/db/db.sqlite"
    if os.path.exists(zdb):
        try:
            zc = sqlite3.connect(f"file:{zdb}?mode=ro", uri=True)
            for sid, updated in zc.execute("select id, time_updated from session"):
                mt = float(updated) / 1000.0 if updated and float(updated) > 1e12 else float(updated or 0)
                short = sid.replace("sess_", "")[:8]
                yield "tp-zcode", short, "ZCode", zdb + ":" + sid, mt, lambda db=zdb, sid=sid: ev_zcode(db, sid)
        except Exception: pass

    # 6. Wiki 结构化页面
    for f in g(os.path.expanduser("~/brain/wiki/*/*.md")):
        if "/_archive/" in f: continue
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
    todo = sorted(sources(), key=lambda s: (RANK.get(s[0], 9), s[4]))
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
    if not res: print("未检索到匹配内容。"); return
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
        print(f"{s:14} {n:>7} 条  {a} ~ {b}")
    print("合计总片段数:", c.execute("select count(*) from chunks").fetchone()[0])

if __name__ == "__main__":
    a = sys.argv[1:]
    if not a: print(__doc__); sys.exit(0)
    if a[0] == "build": build("--full" in a)
    elif a[0] == "search": cmd_search(a[1:])
    elif a[0] == "open": cmd_open(a[1:])
    elif a[0] == "stats": cmd_stats()
    else: print(__doc__)
