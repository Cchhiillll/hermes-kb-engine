#!/usr/bin/env python3
"""Hermes 对话知识库引擎：多 Agent 历史对话 → 一问一答切片 → SQLite FTS5 全文检索（向量检索由 QMD 负责）。

来源支持（本机读到的记 <local_prefix>-xxx，默认 tp；从另一台机器同步到 MAC_SYNC_DIR 的记 <sync_prefix>-xxx，默认 mac）：
  - Claude Code: ~/.claude/projects/*/*.jsonl（同步目录：claude/projects）
  - Codex: ~/.codex/sessions/**/rollout-*.jsonl 及 archived_sessions/**/*.jsonl
  - DSH: ~/.dsh/sessions/**/session*.jsonl.zstd（需要 zstd 命令）
  - Grok: ~/.grok/sessions/**/updates.jsonl
  - ZCode: ~/.zcode/cli/db/db.sqlite（同步目录：zcode/db.sqlite）
  - OpenClaw: ~/.openclaw/agents/*/sessions/*.jsonl*
  - Antigravity: ~/.gemini/antigravity-cli/conversations/*.db（protobuf 线格式解析）
  - Hermes 本体及各 profile: ~/.hermes/state.db、~/.hermes/profiles/*/state.db（渠道见 KB_HERMES_SOURCES / KB_PROFILE_SOURCES）
  - 知识库现有页面: <wiki>/*/*.md
路径与开关见 scripts/kb_config.py。

使用方法：
  kb build [--full]                增量收录变动会话；--full 全量重建（--no-embed 为兼容旧调用保留，无作用）
  kb search 查询词 [-k 8]          检索一问一答切片
  kb open <id> [--around 2]        查看切片全文及上下文
  kb stats                         查看各来源片段统计
"""
import os, re, sys, glob, json, math, time, sqlite3, hashlib, subprocess, datetime as dt
from urllib.parse import unquote

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, "..", "scripts"), os.path.expanduser("~/.hermes/scripts")):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)
import kb_config

CFG = kb_config.load()
HOME = os.path.expanduser("~")
DB = CFG.db
SYNC = CFG.sync_dir
HERMES = CFG.hermes_home
WIKI = CFG.wiki_dir
LOCAL, REMOTE = CFG.local_prefix, CFG.sync_prefix
# 会话 cwd 在这个目录下时，下一级目录名就是「项目」（10-10：原来写死成本机某个个人目录，改成配置项）
ROOT = (CFG.project_root.rstrip("/") + "/") if CFG.project_root else ""
# DSH 把 cwd 编码进目录名（空格写成 ~0020），据此识别项目
DSH_MARK = (os.path.basename(ROOT.rstrip("/")).replace(" ", "~0020") + "-") if ROOT else ""
_ORDER = ("claude", "dsh", "codex", "agy", "openclaw", "hermes", "zcode", "grok")
def rank(src):
    """收录顺序：页面最先，其余按来源类型（同类内本机在前）。重复片段只保留先收的那份。"""
    if src in kb_config.NON_CONVERSATION_SRCS: return -1     # 知识页、语雀文档先收
    pre, _, fam = src.partition("-")
    return (_ORDER.index(fam) if fam in _ORDER else len(_ORDER)) * 2 + (0 if pre == REMOTE else 1)
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
    """cc-connect 外壳剥除：真话在最后一个 'User message:' 之后。"""
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
    if ROOT and cwd and cwd.startswith(ROOT): return cwd[len(ROOT):].split("/")[0] or ""
    return ""

def sid(raw, keep=8):
    """新会话的会话 ID（10-10）：只留字母数字和连字符；超过 16 位时取前 keep 位 + 6 位哈希。
    原来只截 8 位，不同会话撞车时 drop_session 会删掉别人的片段。已收录过的会话沿用旧 ID（见 build 里的 known），旧出处不受影响。"""
    clean = re.sub(r"[^0-9A-Za-z-]", "", str(raw))
    if clean and len(clean) <= 16:
        return clean
    return (clean[:keep] or "s") + "-" + hashlib.sha1(str(raw).encode()).hexdigest()[:6]

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
            if CFG.openclaw_user_prefix: t = re.sub(rf"^{re.escape(CFG.openclaw_user_prefix)}:\s*", "", t)
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

# Antigravity：SQLite 内 steps.step_payload 原生 protobuf 线格式解析
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

def ev_yuque(f):
    """语雀文档（clean_yuque.py 的输出）：按 ## 小节切，每节一段；时间取文档的 updated_at。"""
    from kb_md import split_frontmatter
    fm, body = split_frontmatter(open(f, encoding="utf-8", errors="ignore").read())
    ts = iso2ep(fm.get("updated_at")) or os.path.getmtime(f)
    title, book = fm.get("title") or _stem(f), fm.get("book") or ""
    for n, sec in enumerate(re.split(r"(?m)^(?=## )", body)):
        if not sec.strip(): continue
        head = sec.splitlines()[0].lstrip("#> ").strip() if sec.startswith("## ") else "概述"
        text = "\n".join(sec.splitlines()[1:] if sec.startswith("## ") else sec.splitlines()).strip()
        if text:
            yield ts, "user", f"【语雀】{book}｜{title}｜{head}"
            yield ts, "reply", text

def yuque_docs():
    """(会话 ID, 知识库名, 文件) —— 会话 ID 用 clean_yuque 写的 doc_key（语雀文档 id，没有就是路径哈希），出处形如 ^[kb:yuque-doc:doc_key:小节序号]。"""
    from kb_md import split_frontmatter
    for f in sorted(glob.glob(f"{CFG.yuque_dir}/**/*.md", recursive=True)):
        fm, _ = split_frontmatter(open(f, encoding="utf-8", errors="ignore").read(2000))
        key = fm.get("doc_key") or "h" + hashlib.sha1(os.path.relpath(f, CFG.yuque_dir).encode()).hexdigest()[:10]
        yield sid(key), fm.get("book") or "", f

def _stem(f):
    return os.path.basename(f).split(".")[0]

def sources():
    """生成所有待收录会话: (来源, 会话, 项目, 文件或键, 修改时间, 事件生成器)
    10-10：同一个文件只产出一次（原来本机 ~/.claude、~/.zcode 被当成两个来源各扫一遍）；
    本机读到的记 <local_prefix>-xxx，同步目录里的记 <sync_prefix>-xxx。"""
    g = lambda p, r=False: glob.glob(p, recursive=r)
    roots = ((LOCAL, HOME), (REMOTE, SYNC))

    # 1. Claude Code
    for pre, base in roots:
        pats = ([f"{base}/.claude/projects/*/*.jsonl", f"{base}/.claude/backups/*/*/*.jsonl"] if base == HOME
                else [f"{base}/claude/projects/*/*.jsonl", f"{base}/claude/backups/*/*/*.jsonl"])
        for pat in pats:
            for f in g(pat):
                yield f"{pre}-claude", sid(_stem(f)), project_of(first_cwd(f, "claude")) or "Claude Code", f, os.path.getmtime(f), lambda f=f: ev_claude(f)

    # 2. Codex
    for pre, base in roots:
        d = f"{base}/.codex" if base == HOME else f"{base}/codex"
        for pat in (f"{d}/sessions/**/rollout-*.jsonl", f"{d}/archived_sessions/**/*.jsonl"):
            for f in g(pat, True):
                yield f"{pre}-codex", sid(os.path.basename(f)[28:64] or _stem(f)), project_of(first_cwd(f, "codex")), f, os.path.getmtime(f), lambda f=f: ev_codex(f)

    # 3. DSH（zstd 压缩）
    for pre, base in roots:
        d = f"{base}/.dsh/sessions" if base == HOME else f"{base}/dsh/sessions"
        for f in g(f"{d}/**/session*.jsonl.zstd", True):
            if "session.v2" not in f and os.path.exists(f.replace("session.jsonl.zstd", "session.v2.jsonl.zstd")): continue
            m = re.search(r"session-([0-9a-f\-]{8,})", f); dname = os.path.basename(os.path.dirname(os.path.dirname(f)))
            proj = dname.split(DSH_MARK)[-1].strip("-") if DSH_MARK and DSH_MARK in dname else ""
            yield f"{pre}-dsh", sid(m.group(1) if m else f), proj, f, os.path.getmtime(f), lambda f=f: ev_dsh(f)

    # 4. Grok
    for pre, base in roots:
        pat = f"{base}/.grok/sessions/**/updates.jsonl" if base == HOME else f"{base}/grok/**/updates.jsonl"
        for f in g(pat, True):
            s_ = os.path.basename(os.path.dirname(f))
            parent = os.path.basename(os.path.dirname(os.path.dirname(f)))
            yield f"{pre}-grok", sid(s_), project_of(unquote(parent)) or "Grok", f, os.path.getmtime(f), lambda f=f: ev_grok(f)

    # 5. ZCode 会话库
    for pre, zdb in ((LOCAL, f"{HOME}/.zcode/cli/db/db.sqlite"), (REMOTE, f"{SYNC}/zcode/db.sqlite")):
        if not os.path.exists(zdb): continue
        try:
            zc = sqlite3.connect(f"file:{zdb}?mode=ro", uri=True, timeout=10)
            for s_, directory, updated in zc.execute("select id, directory, time_updated from session"):
                mt = float(updated) / 1000.0 if updated and float(updated) > 1e12 else float(updated or 0)
                yield f"{pre}-zcode", sid(s_.replace("sess_", "")), project_of(directory or "") or "ZCode", zdb + ":" + s_, mt, lambda db=zdb, s_=s_: ev_zcode(db, s_)
        except Exception: pass

    # 6. Antigravity
    for f in g(f"{HOME}/.gemini/antigravity-cli/conversations/*.db"):
        yield f"{LOCAL}-agy", sid(_stem(f)), "Antigravity", f, os.path.getmtime(f), lambda f=f: ev_agy(f)

    # 7. OpenClaw
    for f in g(f"{HOME}/.openclaw/agents/*/sessions/*.jsonl*"):
        if ".trajectory" in f or f.endswith(".json"): continue
        agent = f.split("/agents/", 1)[1].split("/", 1)[0]
        yield f"{LOCAL}-openclaw", sid(_stem(f)), f"{agent}(openclaw)", f, os.path.getmtime(f), lambda f=f: ev_openclaw(f)

    # 8. Hermes 本体及各 profile（10-10：profile 自动发现，不再写死名字）
    dbs = [("hermes", f"{HERMES}/state.db", CFG.hermes_sources, "Hermes")]
    for pdb in sorted(g(f"{HERMES}/profiles/*/state.db")):
        prof = os.path.basename(os.path.dirname(pdb))
        dbs.append((prof, pdb, CFG.profile_sources, prof))
    for name, db, wanted, label in dbs:
        if not os.path.exists(db): continue
        names = {s_.strip(): f"{label}({s_.strip()})" for s_ in wanted.split(",") if s_.strip()}
        src = f"{LOCAL}-" + (re.sub(r"[^a-z]", "", name.lower()) or "profile")
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            keep = {r[0]: (r[1], r[2] or 0) for r in con.execute(
                "select id, source, last_activity_at from sessions where source in (%s)" % ",".join("?" * len(names)), list(names))}
            hs = hermes_sessions(db)
            for s_, (source, la) in keep.items():
                if s_ in hs: yield src, sid(s_), names[source], f"{name}:{s_}", float(la), lambda e=hs[s_]: iter(e)
        except Exception: pass

    # 9. 实时通话记录（各 profile 的 voice-live-notes）
    for f in g(f"{HERMES}/profiles/*/voice-live-notes/*.jsonl"):
        mt = os.path.getmtime(f)
        if time.time() - mt < 120: continue
        prof = f.split("/profiles/", 1)[1].split("/", 1)[0]
        src = f"{LOCAL}-" + (re.sub(r"[^a-z]", "", prof.lower()) or "profile")
        yield src, "call-" + os.path.basename(f)[11:26], f"{prof}(实时通话)", f, mt, lambda f=f: ev_voice_live(f)

    # 10. Wiki 结构化页面
    for f in g(f"{WIKI}/*/*.md"):
        if "/_archive/" in f: continue
        topic = os.path.basename(f)[:-3]
        yield "page", topic, topic, f, os.path.getmtime(f), lambda f=f, topic=topic: ev_page(f, topic)

    # 11. 语雀文档（tools/sync_yuque.sh 同步 + kb/clean_yuque.py 清洗后的目录）
    for session, book, f in yuque_docs():
        yield "yuque-doc", session, book, f, os.path.getmtime(f), lambda f=f: ev_yuque(f)

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

def known_identity(c, key, src, session):
    """已收录过的文件沿用当时的 (来源, 会话 ID)，保证旧的 ^[kb:来源:会话:序号] 出处不失效。
    旧版本把本机 ~/.claude 同时当 mac-claude 和 tp-claude 扫，files 表里记的来源可能和片段实际来源不一致，
    所以以 chunks 表里同一会话、同一类来源（后缀相同）的实际来源为准。"""
    r = c.execute("select src, session from files where key=?", (key,)).fetchone()
    if not r:
        return src, session
    old_src, old_sess = r
    fam = src.split("-", 1)[-1]
    hit = c.execute("select src from chunks where session=? and (src=? or src like ?) limit 1",
                    (old_sess, old_src, f"%-{fam}")).fetchone()
    return (hit[0] if hit else old_src), old_sess

def build(full=False):
    c = conn()
    if full:
        c.executescript("delete from chunks; insert into fts(fts) values('delete-all'); delete from fts_map; delete from files;")
    seen = {r[0]: r[1] for r in c.execute("select key, mtime from files")}
    todo, keys = [], set()
    for t in sources():
        if t[3] in keys: continue                     # 同一文件只收一次
        keys.add(t[3]); todo.append(t)
    todo.sort(key=lambda s: (rank(s[0]), s[4]))
    added = skipped = sess = 0
    for src, session, project, key, mtime, evf in todo:
        if seen.get(key) == mtime: continue
        src, session = known_identity(c, key, src, session)
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
    gone = 0
    for key, src, session in c.execute("select key, src, session from files where src='yuque-doc'").fetchall():
        if not os.path.exists(key):                   # 语雀那边删掉/改名的文档：片段一并删掉（对话来源只增不删）
            drop_session(c, src, session); c.execute("delete from files where key=?", (key,)); gone += 1
    c.commit()
    print(f"收录完成：处理 {sess} 个会话，新增片段 {added}，重复跳过 {skipped}" + (f"，移除已删除的语雀文档 {gone} 篇" if gone else ""))

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
