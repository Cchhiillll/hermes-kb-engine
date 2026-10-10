#!/usr/bin/env python3
"""学习闭环 · 教训手册（10-10，参考 ACE 的「条目 + 确定性增量合并」和 Reflexion / ExpeL 的经验沉淀）。

唯一真相：wiki/lessons/_playbook.json；wiki/lessons/<领域>.md 由它渲染（稳定 / 草稿（未核实）/ 历史 三节，带出处与 👍/👎），不要手改。
模型只「提议」操作，合并由本脚本按固定规则做，结果可复现：

  ADD     {"op":"ADD","domain":"领域","text":"一句话教训","basis":"user|output|agent","sources":["段id"],"confirmed":false}
  UPDATE  {"op":"UPDATE","id":"L-xxxxxxxx","text":"新说法(可选)","basis":"…","sources":["段id"]}
  REMOVE  {"op":"REMOVE","id":"L-xxxxxxxx","reason":"原因"}

规则：
- 必须带出处（sources，段 id）；同领域里和已有条目相似度（Jaccard）≥0.8 的算同一条，只合并出处和依据
- 相似但一条是否定说法（不要/别/禁止…）另一条不是：两条都标「冲突」，不升级，等人处理
- 稳定条目只接受 user / output 依据的 UPDATE；REMOVE 是软删除（进「历史」）
- 每个领域最多 KB_LESSONS_MAX（默认 50）条在用条目，超了先把最没用的草稿挪进历史
升级（草稿 → 稳定），满足其一且没有冲突、最近一次检索评测没退步：
  用户确认（confirmed）；或 ≥2 个不同会话的出处且依据含 user/output；或 👍≥2 且 👎=0 且依据含 user/output。只有 agent 依据的永远不升级。
衰减：👎≥2 且 👎>👍 —— 稳定条目降回草稿，草稿退役；未确认的草稿 90 天没被用到 —— 退役。
反馈：读 kb-recall 命中日志（注入过哪些教训）+ Hermes 会话库里用户的下一句话：纠正（不对/错了/不行…）记 👎，肯定（好了/可以了/谢谢…）记 👍。

  kb_lessons.py apply ops.json|-     应用操作（拿不到写入锁时排队，下次 maintain 再做）
  kb_lessons.py feedback             读命中日志更新 👍/👎
  kb_lessons.py decay                衰减
  kb_lessons.py maintain             排队的操作 + 反馈 + 衰减 + 升级 + 渲染（每小时 cron）
  kb_lessons.py render | stats
"""
import datetime as dt
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import kb_config

GENERATOR = "hermes-kb/kb_lessons"
SEG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*:[0-9A-Za-z_\-]+:\d+$")
SIM_SAME, SIM_CONFLICT = 0.8, 0.5
NEG = re.compile(r"不要|别|禁止|不能|不可|切勿|避免|不应|don'?t|never|avoid|\bnot\b", re.I)
CORRECTION = re.compile(r"不对|错了|不是这样|搞错|说错|别这样|不要这样|还是不行|不行|没用|不管用|你又|wrong|incorrect|that'?s not|doesn'?t work", re.I)
POSITIVE = re.compile(r"对的|没错|可以了|好了|搞定|成功了|解决了|谢谢|有用|管用|works|thanks|perfect|great", re.I)
DRAFT_TTL_DAYS = 90
REPLY_WAIT = 600          # 命中后至少等 10 分钟再看用户的下一句
REPLY_GIVEUP = 86400      # 一天都没有下一句：只算「用过」


def _cfg():
    return kb_config.load()


def paths():
    c = _cfg()
    d = os.path.join(c.wiki_dir, "lessons")
    return d, os.path.join(d, "_playbook.json"), os.path.join(c.brain_dir, "kb", "lessons_pending.jsonl")


def today(now=None):
    return dt.date.fromtimestamp(now or time.time()).isoformat()


# ---------- 相似度 ----------
def toks(s):
    s = (s or "").lower()
    out = set(re.findall(r"[a-z0-9_./\-]{2,}", s))
    for run in re.findall(r"[\u4e00-\u9fff]+", s):
        out.update(run[i:i + 2] for i in range(max(len(run) - 1, 1)))
    return out


def jaccard(a, b):
    ta, tb = toks(a), toks(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def slug(domain):
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", (domain or "").strip()).strip("-.")
    return s[:40] or "通用"


# ---------- 读写 ----------
def load(path=None):
    path = path or paths()[1]
    try:
        pb = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        pb = {}
    pb.setdefault("version", 1)
    pb.setdefault("lessons", {})
    pb.setdefault("cursor", 0)
    return pb


def save(pb, path=None):
    path = path or paths()[1]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    json.dump(pb, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path)


def active(pb, domain=None):
    return [l for l in pb["lessons"].values() if l["status"] != "retired" and (domain is None or l["domain"] == domain)]


def sessions_of(sources):
    return {s.rsplit(":", 1)[0] for s in sources}


def _hist(l, now, op, note=""):
    l.setdefault("history", []).append({"date": today(now), "op": op, "note": note[:200]})
    l["history"] = l["history"][-10:]


def _basis(b):
    if isinstance(b, str):
        b = [b]
    return sorted({x for x in (b or []) if x in ("user", "output", "agent")}) or ["agent"]


# ---------- 操作 ----------
def apply_ops(pb, ops, now=None):
    """按顺序确定性地应用操作，返回 [(op, 结果, id 或原因)]。"""
    now = now or time.time()
    out = []
    for op in ops:
        kind = str(op.get("op", "")).upper()
        try:
            out.append((kind,) + {"ADD": _add, "UPDATE": _update, "REMOVE": _remove}[kind](pb, op, now))
        except KeyError:
            out.append((kind, "rejected", "不认识的操作"))
    return out


def _check_sources(op):
    src = [s for s in (op.get("sources") or op.get("seg") or []) if isinstance(s, str) and SEG.match(s)]
    return sorted(set(src))


def _add(pb, op, now):
    text = re.sub(r"\s+", " ", str(op.get("text") or "")).strip()
    src = _check_sources(op)
    if len(text) < 6:
        return "rejected", "教训太短或为空"
    if not src:
        return "rejected", "没有有效出处（sources 要写段 id）"
    domain = str(op.get("domain") or "通用").strip()[:40] or "通用"
    basis = _basis(op.get("basis"))
    best, sim = None, 0.0
    for l in active(pb, domain):
        s = jaccard(text, l["text"])
        if s > sim:
            best, sim = l, s
    if best and sim >= SIM_SAME:
        best["sources"] = sorted(set(best["sources"]) | set(src))
        best["basis"] = sorted(set(best["basis"]) | set(basis))
        best["confirmed"] = bool(best.get("confirmed") or op.get("confirmed"))
        best["updated"] = today(now)
        _hist(best, now, "merge", f"合并相似条目（{sim:.2f}）")
        return "merged", best["id"]
    lid = "L-" + hashlib.sha1(f"{domain}\x00{text}".encode()).hexdigest()[:8]
    if lid in pb["lessons"]:
        old = pb["lessons"][lid]
        if old["status"] == "retired":
            return "rejected", f"和已退役的 {lid} 完全相同（{old.get('retired_reason', '')}）"
        return "merged", lid
    l = {"id": lid, "domain": domain, "text": text, "status": "draft", "basis": basis, "sources": src,
         "confirmed": bool(op.get("confirmed")), "helpful": 0, "harmful": 0, "created": today(now),
         "updated": today(now), "last_used": "", "conflict_with": []}
    if best and sim >= SIM_CONFLICT and bool(NEG.search(text)) != bool(NEG.search(best["text"])):
        l["conflict_with"] = [best["id"]]
        best.setdefault("conflict_with", []).append(lid)
        _hist(best, now, "conflict", f"和 {lid} 说法相反")
    _hist(l, now, "add")
    pb["lessons"][lid] = l
    _cap(pb, domain, now)
    return ("conflict" if l["conflict_with"] else "added"), lid


def _cap(pb, domain, now):
    mx = int(os.environ.get("KB_LESSONS_MAX") or 50)
    act = active(pb, domain)
    drafts = sorted((l for l in act if l["status"] == "draft"),
                    key=lambda l: (l["helpful"] - l["harmful"], l.get("last_used") or l["created"], l["created"]))
    while len(act) > mx and drafts:
        l = drafts.pop(0)
        _retire(l, now, f"领域条目超过 {mx} 条，挪出最没用的草稿")
        act = active(pb, domain)


def _retire(l, now, why):
    l["status"] = "retired"
    l["retired_reason"] = why
    l["updated"] = today(now)
    _hist(l, now, "retire", why)


def _update(pb, op, now):
    l = pb["lessons"].get(op.get("id"))
    if not l or l["status"] == "retired":
        return "rejected", f"没有在用的 {op.get('id')}"
    src = _check_sources(op)
    if not src:
        return "rejected", "UPDATE 也要带出处"
    basis = _basis(op.get("basis"))
    if l["status"] == "stable" and not set(basis) & {"user", "output"}:
        return "rejected", "稳定条目只接受用户或实测依据的修改"
    text = re.sub(r"\s+", " ", str(op.get("text") or "")).strip()
    if text and text != l["text"]:
        _hist(l, now, "update", "原说法：" + l["text"])
        l["text"] = text
    l["sources"] = sorted(set(l["sources"]) | set(src))
    l["basis"] = sorted(set(l["basis"]) | set(basis))
    if op.get("confirmed"):
        l["confirmed"] = True
    if op.get("resolve_conflict"):
        for o in l.get("conflict_with", []):
            other = pb["lessons"].get(o)
            if other:
                other["conflict_with"] = [x for x in other.get("conflict_with", []) if x != l["id"]]
        l["conflict_with"] = []
    l["updated"] = today(now)
    return "updated", l["id"]


def _remove(pb, op, now):
    l = pb["lessons"].get(op.get("id"))
    if not l or l["status"] == "retired":
        return "rejected", f"没有在用的 {op.get('id')}"
    why = str(op.get("reason") or "").strip()
    if not why:
        return "rejected", "REMOVE 要写原因"
    _retire(l, now, why)
    for o in l.get("conflict_with", []):
        other = pb["lessons"].get(o)
        if other:
            other["conflict_with"] = [x for x in other.get("conflict_with", []) if x != l["id"]]
    return "retired", l["id"]


# ---------- 升级 / 衰减 ----------
def promotable(l):
    if l["status"] != "draft" or l.get("conflict_with"):
        return False
    grounded = bool(set(l["basis"]) & {"user", "output"})
    if l.get("confirmed"):
        return True
    if not grounded:
        return False                       # 只有 agent 自己的说法，永远不升级
    return len(sessions_of(l["sources"])) >= 2 or (l["helpful"] >= 2 and l["harmful"] == 0)


def promote(pb, now=None, regressed=None):
    now = now or time.time()
    if regressed is None:
        try:
            import kb_eval
            regressed = kb_eval.latest_regressed()
        except Exception:
            regressed = False
    if regressed:
        return []
    up = []
    for l in pb["lessons"].values():
        if promotable(l):
            l["status"] = "stable"
            l["updated"] = today(now)
            _hist(l, now, "promote")
            up.append(l["id"])
    return up


def decay(pb, now=None):
    now = now or time.time()
    changed = []
    cutoff = today(now - DRAFT_TTL_DAYS * 86400)
    for l in pb["lessons"].values():
        if l["status"] == "retired":
            continue
        if l["harmful"] >= 2 and l["harmful"] > l["helpful"]:
            if l["status"] == "stable":
                l["status"] = "draft"
                l["confirmed"] = False
                _hist(l, now, "demote", f"👎{l['harmful']} > 👍{l['helpful']}")
                changed.append((l["id"], "demoted"))
            else:
                _retire(l, now, f"被判有害次数多于有用（👎{l['harmful']} / 👍{l['helpful']}）")
                changed.append((l["id"], "retired"))
        elif l["status"] == "draft" and not l.get("confirmed") and (l.get("last_used") or l["created"]) < cutoff:
            _retire(l, now, f"{DRAFT_TTL_DAYS} 天没被用到、也没被确认")
            changed.append((l["id"], "retired"))
    return changed


# ---------- 反馈 ----------
def _state_dbs():
    h = _cfg().hermes_home
    return [p for p in [os.path.join(h, "state.db")] + sorted(glob.glob(os.path.join(h, "profiles", "*", "state.db"))) if os.path.exists(p)]


def next_user_message(session, after):
    for p in _state_dbs():
        try:
            st = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            r = st.execute("select content from messages where session_id=? and role='user' and timestamp>? order by timestamp limit 1",
                           (session, after)).fetchone()
        except sqlite3.Error:
            continue
        if r:
            return r[0] or ""
    return None


def feedback(pb, now=None, log_path=None):
    """读命中日志里 cursor 之后、且已过 10 分钟的记录；返回 (处理条数, 👍, 👎)。"""
    now = now or time.time()
    log_path = log_path or _cfg().recall_log
    n = up = down = 0
    recs = []
    for p in (log_path + ".1", log_path):
        try:
            for line in open(p, encoding="utf-8"):
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
    cursor = pb.get("cursor", 0)
    for r in sorted(recs, key=lambda r: r.get("ts", 0)):
        ts = r.get("ts", 0)
        if ts <= cursor or not r.get("lessons"):
            continue
        if now - ts < REPLY_WAIT:
            break
        reply = next_user_message(r["session"], ts) if r.get("session") else None
        if reply is None and now - ts < REPLY_GIVEUP and r.get("session"):
            break                          # 还在等用户的下一句
        verdict = "harmful" if reply and CORRECTION.search(reply) else "helpful" if reply and POSITIVE.search(reply) else ""
        for lid in r["lessons"]:
            l = pb["lessons"].get(lid)
            if not l:
                continue
            l["last_used"] = today(ts)
            if verdict:
                l[verdict] += 1
                _hist(l, now, verdict, (reply or "")[:60])
        up += verdict == "helpful"
        down += verdict == "harmful"
        n += 1
        pb["cursor"] = ts
    return n, up, down


# ---------- 渲染 ----------
def _cites(l, k=3):
    return " ".join(f"^[kb:{s}]" for s in l["sources"][-k:])


def render(pb, now=None, lessons_dir=None):
    d = lessons_dir or paths()[0]
    os.makedirs(d, exist_ok=True)
    by = {}
    for l in pb["lessons"].values():
        by.setdefault(l["domain"], []).append(l)
    written = set()
    for domain, ls in sorted(by.items()):
        st = sorted((l for l in ls if l["status"] == "stable"), key=lambda l: (-l["helpful"], l["id"]))
        dr = sorted((l for l in ls if l["status"] == "draft"), key=lambda l: (-l["helpful"], l["id"]))
        old = sorted((l for l in ls if l["status"] == "retired"), key=lambda l: l["id"])
        if not st and not dr and not old:
            continue
        mark = lambda l: f" 👍{l['helpful']} 👎{l['harmful']}" + ("（有冲突：" + "、".join(l["conflict_with"]) + "）" if l.get("conflict_with") else "")
        lines = ["---", f'title: "教训 · {domain}"', f"updated: {today(now)}", f'generator: "{GENERATOR}"', "---",
                 f"# 教训 · {domain}", "",
                 f"> 一句话：{domain} 的经验教训（稳定 {len(st)} 条、草稿 {len(dr)} 条）。由 kb_lessons.py 从 _playbook.json 生成，不要手改。", "",
                 "## 稳定"] + [f"- [{l['id']}] {l['text']}{mark(l)} {_cites(l)}" for l in st] + (["- 暂无"] if not st else []) + \
                ["", "## 草稿（未核实）"] + [f"- [{l['id']}] {l['text']}{mark(l)} {_cites(l)}" for l in dr] + (["- 暂无"] if not dr else []) + \
                ["", "## 历史"] + [f"- [{l['id']}] ~~{l['text']}~~（{l.get('retired_reason', '')}，{l['updated']}）" for l in old[-30:]] + (["- 暂无"] if not old else [])
        text = "\n".join(lines) + "\n"
        path = os.path.join(d, slug(domain) + ".md")
        written.add(os.path.abspath(path))
        cur = open(path, encoding="utf-8").read() if os.path.exists(path) else None
        strip = lambda s: re.sub(r"^updated: .*$", "", s or "", flags=re.M)
        if strip(cur) != strip(text):
            open(path, "w", encoding="utf-8").write(text)
    for f in glob.glob(os.path.join(d, "*.md")):
        if os.path.abspath(f) not in written and GENERATOR in open(f, encoding="utf-8", errors="ignore").read(400):
            os.remove(f)
    return len(written)


# ---------- 锁与排队 ----------
def _locked(fn):
    import kb_lock
    ok, prev = kb_lock.acquire("lessons", pid=str(os.getpid()), work="更新教训手册")
    if not ok:
        return False, prev
    try:
        return True, fn()
    finally:
        kb_lock.release("lessons")


def apply_or_queue(ops, now=None):
    """拿得到写入锁就立即应用并渲染，拿不到就排队（maintain 时再做）。返回结果列表或 None（已排队）。"""
    ops = [o for o in ops if isinstance(o, dict)]
    if not ops:
        return []

    def run():
        pb = load()
        res = apply_ops(pb, ops, now)
        promote(pb, now)
        save(pb); render(pb, now)
        return res
    ok, res = _locked(run)
    if ok:
        return res
    pend = paths()[2]
    os.makedirs(os.path.dirname(pend), exist_ok=True)
    with open(pend, "a", encoding="utf-8") as f:
        for o in ops:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return None


def maintain(now=None):
    def run():
        pb = load()
        pend = paths()[2]
        ops = []
        if os.path.exists(pend):
            ops = [json.loads(l) for l in open(pend, encoding="utf-8") if l.strip()]
            os.remove(pend)
        res = apply_ops(pb, ops, now)
        fb = feedback(pb, now)
        dc = decay(pb, now)
        up = promote(pb, now)
        save(pb); render(pb, now)
        return res, fb, dc, up
    return _locked(run)


def stats(pb):
    c = {"stable": 0, "draft": 0, "retired": 0}
    for l in pb["lessons"].values():
        c[l["status"]] += 1
    return c


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    cmd = a[0] if a else "stats"
    if cmd == "apply":
        src = a[1] if len(a) > 1 else "-"
        data = json.load(sys.stdin if src == "-" else open(src, encoding="utf-8"))
        res = apply_or_queue(data if isinstance(data, list) else data.get("ops", []))
        if res is None:
            print("知识库正被别的任务改，操作已排队，下次 maintain 时应用。"); return 0
        for r in res:
            print(" ".join(map(str, r)))
        return 0 if all(r[1] != "rejected" for r in res) else 1
    if cmd == "maintain":
        ok, r = maintain()
        if not ok:
            print(f"跳过：知识库正被占用"); return 0
        res, (n, up, down), dc, prom = r
        print(f"教训维护：应用排队操作 {len(res)} 条；反馈 {n} 次（👍{up} 👎{down}）；衰减 {len(dc)} 条；升级 {len(prom)} 条")
        return 0
    pb = load()
    if cmd == "feedback":
        n, up, down = feedback(pb); save(pb); render(pb)
        print(f"反馈 {n} 次：👍{up} 👎{down}")
    elif cmd == "decay":
        dc = decay(pb); save(pb); render(pb)
        print(f"衰减 {len(dc)} 条：" + "、".join(f"{i}({s})" for i, s in dc))
    elif cmd == "render":
        print(f"渲染 {render(pb)} 个领域页")
    elif cmd == "stats":
        s = stats(pb)
        print(f"教训：稳定 {s['stable']}、草稿 {s['draft']}、历史 {s['retired']}")
        worst = sorted(active(pb), key=lambda l: -l["harmful"])[:5]
        for l in worst:
            if l["harmful"]:
                print(f"  👎{l['harmful']} 👍{l['helpful']} {l['id']} {l['text'][:50]}")
    else:
        print(__doc__); return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
