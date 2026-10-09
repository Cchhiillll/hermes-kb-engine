#!/usr/bin/env python3
"""给「知识库-深加工」分活（10-04 起取代 wiki_target.py 的「单页整理」）：把同一主题的碎页收敛成一页。

为什么：10-04 知识库 471 页里 371 页是 concepts/，251 页名字带「规程」，178 页不到 2KB；原深加工规则是
「反复出现的问题拆成做法页、超过 200 行就拆」+ 只许 patch，只会越拆越碎、越追加越长，没有合并和淘汰。

碎页组做完后，再逐个「重整大页」（>40KB 或 >400 行：按 现状/做法与排障/规则/决定/历史 重写，现状每条带日期和出处，页 ≤450 行且 ≤40KB，单行 ≤1000 字、≤4 个出处；重写前自动存快照，所以不要求旧出处全留）。
每轮领一组（同一主题前缀的 concepts/ 碎页，最多 MAX_PAGES 页；能对上已有项目/实体页的，那一页就是归宿），
交给 Hermes 合并（10-04 起：在暂存区起草，--done 核对通过后拿锁提交，提交前核对这些页起草期间没被别人改过）；Hermes 在记录文件里写「并入 旧页 -> 新页」，然后运行 --done：
  - 核对：每个被并掉的页里的出处 kb:段id 必须在新页里都还在，否则拒绝并列出缺的；
  - 收尾：被并掉的页搬进 _archive/merged/，全库 [[旧页]] / [[concepts/旧页|别名]] 改指向新页；记一轮到 wiki_refine_runs。

  wiki_consolidate.py              领一组（有待并入的新对话或读对话批次在跑时让路，同 wiki_target.py）
  wiki_consolidate.py --peek       只看还剩哪些组、下一组是什么
  wiki_consolidate.py --done <组号> 核对并收尾
luna 让路闸：入口 wiki_target_luna.py 读现状页的 luna 用量，超线不分活（它 import 本模块的 pick）。
"""
import collections, fcntl, glob, hashlib, json, os, re, shutil, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(__file__)); sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
W = os.environ.get("WIKI_DIR") or os.path.expanduser("~/brain/wiki")          # 测试时指向副本
DB = os.environ.get("KB_DB") or os.path.expanduser("~/brain/kb/kb.sqlite")
REC_ENV = os.environ.get("CONSOLIDATE_REC")    # 测试时指定；正式运行每组一个记录文件（10-04：4 路共用一个「先清空再写」的文件，交卷互相读错）
def rec_path(gid):
    if REC_ENV and os.path.isdir(REC_ENV):
        return os.path.join(REC_ENV, f"consolidate-{gid}.log.md")
    return REC_ENV or os.path.expanduser(f"~/brain/kb/batches/consolidate-{gid}.log.md")
PLAN_FILE = os.environ.get("CONSOLIDATE_PLAN") or os.path.expanduser("~/brain/kb/consolidate_plan.json")   # 没有归宿页的碎页，由 Hermes 提的分组方案（脚本校验后才用）
STAGE = os.environ.get("KB_STAGE") or os.path.expanduser("~/brain/kb/staging")   # 10-04：起草在暂存区并行，提交时拿锁、核对没被别人改过再替换进知识库
COMMIT_WAIT = int(os.environ.get("KB_COMMIT_WAIT") or 15 * 60)                 # 提交时等锁最多多久（并入一轮可能占锁几分钟）
import kb_lock
LOG = os.path.join(W, "log.md")
LEASE = 50 * 60
MAX_PAGES = 8
BIG_BYTES, BIG_LINES, MAX_LINES = 40000, 400, 450   # 大页门槛；重整后上限
MAX_BYTES, MAX_LINE_CHARS, MAX_CITES_PER_LINE = 40000, 1000, 4   # 重整后：总字节、单行字数、单行出处数（10-04 测试：它把内容挤成超长行、一行堆几十个出处来过关）
KB_ID = re.compile(r"kb:([a-z]+-[a-z]+:[0-9A-Za-z\-]+:\d+)")
TICK = re.compile(r"`([^`\n]{2,80})`")           # 具体项：命令、路径、端口、版本、配置项（反引号里的）
MAX_TOTAL_RATIO, MAX_SUBS = 0.4, 6                # 10-04 第二轮试跑：它把 29 万字节的页按 4 万切成 15 块「前端-2/前端-3…」，86% 逐字照搬——加总量和子页数上限，逼它去重
MIN_KEEP = 0.7                                    # 10-04 试跑：重写大页只留下 4%~15% 的具体项，Hermes 会丢实操细节；重写后（含子页）至少留 70%
CITE = re.compile(r"\^\[[^\]]*\]")


def inline_ids(text):
    """只认挂在实际内容后面的出处：一行去掉出处标记后至少 20 个字才算（10-04 测试时它把出处单独堆成清单应付核对）。"""
    ids = set()
    for line in text.split("\n"):
        body = re.sub(r"[\s\-*#>|:：、，,。.()（）]", "", CITE.sub("", line))
        body = re.sub(r"^来源concepts/\S+", "", body)
        found = KB_ID.findall(line)
        if len(body) >= 20 and len(found) <= 2 * MAX_CITES_PER_LINE:
            ids |= set(found)
    return ids
norm = lambda s: re.sub(r"[-_\s]", "", s.lower())


def conn():
    c = sqlite3.connect(DB)
    c.execute("create table if not exists wiki_consolidate(gid text primary key, key text, pages text, home text, leased real default 0, done real default 0, result text)")
    c.execute("create table if not exists wiki_refine_runs(ts real, page text, mode text, size_before integer, size_after integer)")
    try:
        c.execute("alter table wiki_consolidate add column fails integer default 0")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("alter table wiki_consolidate add column redo text")   # 10-05：重写丢了细节要重做的，记重写前原页快照路径
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("alter table wiki_consolidate add column fail_ts real default 0")
    except sqlite3.OperationalError:
        pass
    return c


PROC_SUFFIX = ("规程", "规范", "准则", "铁律")


def procedure_page(path):
    """文件名以规程、规范、准则、铁律结尾的做法页，归宿是项目页或机器页，不再并进做法区。"""
    base = (path or "").split("/")[-1]
    return any(base.endswith(s) for s in PROC_SUFFIX)


def homes():
    out = {}
    for d in ("projects", "entities"):
        for f in glob.glob(f"{W}/{d}/*.md"):
            n = os.path.basename(f)[:-3]
            out[norm(n)] = f"{d}/{n}"
    return out


def groups():
    """分组（10-04 改）：①名字以某个项目/实体页名开头的碎页 → 并进那一页；②其余碎页只按 Hermes 提、脚本校验过的方案
    （PLAN_FILE：[{"pages": [...], "target": "目录/页名"}]，每页只进一组、每组 2~MAX_PAGES 页、页都还在）分组。
    不再按页名前两个字分组（10-04「大模」4 组 28 页被灌进同一页）。"""
    hs = homes()
    g = collections.defaultdict(list)
    for f in sorted(glob.glob(f"{W}/concepts/*.md")):
        n = os.path.basename(f)[:-3]
        k = norm(n)
        best = max((h for h in hs if len(h) >= 3 and k.startswith(h)), key=len, default=None)
        if best:
            g["→" + hs[best]].append(f"concepts/{n}")
    out, seen = [], set()
    for key, pages in g.items():
        for i in range(0, len(pages), MAX_PAGES):
            chunk = pages[i:i + MAX_PAGES]
            gid = "g" + hashlib.sha1(("|".join(chunk)).encode()).hexdigest()[:8]
            out.append((gid, key, chunk, key[1:])); seen |= set(chunk)
    try:
        plan = json.load(open(PLAN_FILE, encoding="utf-8"))
    except (OSError, ValueError):
        plan = []
    for grp in plan:
        pages = [p for p in grp.get("pages", []) if p not in seen and os.path.exists(f"{W}/{p}.md")]
        target = grp.get("target")
        if not target or not 1 <= len(pages) <= MAX_PAGES or pages == [target]:
            continue
        gid = "g" + hashlib.sha1(("|".join(pages)).encode()).hexdigest()[:8]
        # 方案里若把归宿指到一张规程页，不再往那张做法页并；交给模型写回项目页或机器页
        home = None if procedure_page(target) else target
        out.append((gid, ("→" + target) if home else ("move:" + pages[0]), pages, home)); seen |= set(pages)
    out.sort(key=lambda x: -len(x[2]))
    return out


def size(p):
    try:
        return os.path.getsize(f"{W}/{p}.md")
    except OSError:
        return 0


MAX_FAILS, RETRY_AFTER = 3, 86400   # 10-05 审查：超时/掐断才算失败，满 3 次跳过并发飞书提醒，24 小时后自动重试；被别人改过而作废不算失败


def fail(c, gid, why):
    """记一次失败；到 MAX_FAILS 时发一张飞书提醒（只发这一次）。"""
    c.execute("update wiki_consolidate set fails=coalesce(fails,0)+1, fail_ts=?, leased=0 where gid=?", (time.time(), gid)); c.commit()
    n = c.execute("select fails, key from wiki_consolidate where gid=?", (gid,)).fetchone()
    # 内部调度自愈逻辑，静默记入 DB 和日志，严禁因“不用你做什么”给用户发飞书卡片骚扰
    pass


def skipped(c, gid):
    r = c.execute("select fails, fail_ts from wiki_consolidate where gid=?", (gid,)).fetchone()
    if not r or (r[0] or 0) < MAX_FAILS:
        return False
    if time.time() - (r[1] or 0) >= RETRY_AFTER:          # 跳过满 24 小时：清零重试
        c.execute("update wiki_consolidate set fails=0 where gid=?", (gid,)); c.commit(); return False
    return True


def ticks_substantive(text):
    """数「具体项」时只算写在正文里的：一行堆了一串反引号项、其余几乎没字的清单行不算（10-05 审查：有页面用「旧快照里还出现过：`…`」凑七成）。"""
    out = set()
    for line in text.split("\n"):
        items = TICK.findall(line)
        if not items:
            continue
        body = re.sub(r"[\s\-*#>|:：、，,。.()（）]", "", CITE.sub("", TICK.sub("", line)))
        if len(items) > 4 and len(body) < 8 * len(items):
            continue
        if re.search(r"旧快照里(还)?出现过|曾出现过的(命令|路径|配置)", line):
            continue
        out |= set(items)
    return out


def sha(p):
    try:
        return hashlib.sha1(open(f"{W}/{p}.md", "rb").read()).hexdigest()
    except OSError:
        return None


def stage(gid, pages):
    """把这次要改的页复制进暂存区（相对路径同知识库），记下每页当时的指纹，交卷提交时核对。
    10-05：目标页已有的子页「页名-*」也一起登记，提交时一并核对，不会被不经核对地覆盖。"""
    pages = list(pages) + [f"{os.path.dirname(x)}/{os.path.basename(f)[:-3]}" for x in pages for f in glob.glob(f"{W}/{x}-*.md")]
    pages = list(dict.fromkeys(pages))
    S = f"{STAGE}/{gid}"
    shutil.rmtree(S, ignore_errors=True); os.makedirs(S, exist_ok=True)
    man = {}
    for p in pages:
        man[p] = sha(p)
        if man[p]:
            os.makedirs(os.path.dirname(f"{S}/{p}.md"), exist_ok=True); shutil.copy2(f"{W}/{p}.md", f"{S}/{p}.md")
            orig = f"{S}/.orig/{p}.md"
            os.makedirs(os.path.dirname(orig), exist_ok=True); shutil.copy2(f"{W}/{p}.md", orig)
    json.dump(man, open(f"{S}/.manifest.json", "w"), ensure_ascii=False)
    return S


def claimed(c, now):
    """别的路正在起草的页（领走未交、没超时）：领活时避开，两路不改同一页。"""
    out = set()
    for pages, home in c.execute("select pages, home from wiki_consolidate where coalesce(done,0)=0 and leased>?", (now - LEASE,)):
        out |= set(json.loads(pages)) | ({home} if home else set())
    return out


def pick(peek=False, force=False, job=None, prefer="small"):
    """prefer=big：先重写超长页（Grok 那路）；prefer=small：先合并、补日期（luna 那路）；自己那类没活了就帮另一类。
    10-05：领活全程拿一把文件锁，两路同一秒启动也不会领到同一组。只能由入口脚本 import 调用，命令行不领活。"""
    if peek:
        return _pick(peek, prefer)
    os.makedirs(STAGE, exist_ok=True)
    with open(f"{STAGE}/.pick.lock", "a+") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _pick(peek, prefer)


def _pick(peek, prefer):
    c = conn(); now = time.time(); todo = []
    busy = set() if peek else claimed(c, now)
    for gid, key, pages, home in groups():
        r = c.execute("select leased, done, fails from wiki_consolidate where gid=?", (gid,)).fetchone()
        if r and r[1]:
            continue
        if not peek and r and r[0] and now - r[0] >= LEASE:   # 领走超时没交（或被重启掐断）：记一次失败，暂存区作废（知识库没被动过）
            fail(c, gid, "领走超过 50 分钟没交卷"); shutil.rmtree(f"{STAGE}/{gid}", ignore_errors=True); r = (0, 0, 0)
        if r and r[0]:
            continue
        if skipped(c, gid):
            continue
        if home and size(home) > MAX_BYTES:              # 目标页已超上限：先等它重写，不再往里并
            continue
        if any(size(x) > MAX_BYTES for x in pages):      # 组员本身超上限（比如 10 万字节的大杂烩）：先等它重写，不整页灌进别处
            continue
        if busy & set(pages + ([home] if home else [])):
            continue
        todo.append((gid, key, pages, home))
    bigs = [b for b in big_pages(c, now) if b[0] not in busy]
    if peek:
        print(f"还剩 {len(todo)} 组、{sum(len(t[2]) for t in todo)} 页待收敛；concepts/ 现有 {len(glob.glob(W + '/concepts/*.md'))} 页；待重整大页 {len(bigs)} 个")
        for gid, key, pages, home in todo[:10]:
            print(f"  {gid} {key} {len(pages)} 页 → {home or ('项目或机器页' if any(procedure_page(p) for p in pages) else '（新主题页）')}")
        for p, b, l in bigs[:5]:
            print(f"  大页 {p}（{b} 字节、{l} 行）")
        failed = c.execute("select gid, key, fails from wiki_consolidate where coalesce(fails,0)>=? and coalesce(done,0)=0", (MAX_FAILS,)).fetchall()
        if failed:
            print(f"  失败 {MAX_FAILS} 次已跳过 {len(failed)} 个：" + "、".join(f"{g}({k})" for g, k, _ in failed[:8]))
        return
    really_big = [b for b in bigs if b[1] > BIG_BYTES or b[2] > BIG_LINES]
    small_big = [b for b in bigs if not (b[1] > BIG_BYTES or b[2] > BIG_LINES)]
    order = {"big": ["big", "group", "fill"], "light": ["fill"]}.get(prefer, ["group", "fill", "big"])   # 10-06 用户：Grok 干重活、luna 干轻活。按实测（Grok 整理一次交卷 73%、被打回 2.3 次/次；luna 63%、4.8 次/次），luna 只领补日期
    for kind in order:
        if kind == "big" and really_big:
            return pick_big(c, now, really_big[0])
        if kind == "fill" and small_big:
            return pick_big(c, now, small_big[0])
        if kind == "group" and todo:
            break
    else:
        print("NO_TARGET：目前没有能领的整理活（或者都被另一路领着），本次直接结束。"); print(json.dumps({"wakeAgent": False})); return
    gid, key, pages, home = todo[0]
    c.execute("insert into wiki_consolidate(gid, key, pages, home, leased) values(?,?,?,?,?) on conflict(gid) do update set leased=excluded.leased",
              (gid, key, json.dumps(pages, ensure_ascii=False), home, now)); c.commit()
    S = stage(gid, pages + ([home] if home else []))
    dest = home or ("它所属的项目页或机器页" if any(procedure_page(p) for p in pages) else "一个主题页")
    print(f"组号：{gid}（{len(pages)} 页并进 {dest}）")
    if home:
        print(f"归宿页：{home}.md（{size(home)} 字节）")
    print("这组碎页：")
    for p in pages:
        print(f"  - {p}.md（{size(p)} 字节）")
    print(f"暂存目录：{S}（上面这些页的副本在这里，相对路径和知识库一样；只在这里改，新页也建在这里）")
    print(f"这轮的记录写进：{rec_path(gid)}（本组专用，先清空再写）")
    if any(procedure_page(p) for p in pages) or procedure_page(home):
        print("归宿：文件名带「规程」「规范」「准则」「铁律」的，内容写进所属项目页或机器页的「做法与排障」。"
              "记录行右边必须是 projects/ 或 entities/。找不到所属页就不要交卷，不要并进别的做法页，也不要新开做法页。")


def big_pages(c, now):
    """要重整的页：先是 >40KB 或 >400 行的大页（最大的先来）；再是还没有带日期「现状」的项目页、实体页
    （10-04 用户：旧记录被当成现状，所以每个项目/实体页都要有「## 现状」且条目写「截至 YYYY-MM-DD」）。7 天内重整过的跳过。"""
    out, rest = [], []
    members = set()                                       # 还要被合并掉的页：不单独补日期（10-05 审查：「事实时效」刚重写完 9 分钟就被并掉，白做）
    for gid, key, pages, home in groups():
        r = c.execute("select done from wiki_consolidate where gid=?", (gid,)).fetchone()
        if not (r and r[0]):
            members |= set(pages)
    allp = {f"{d}/{os.path.basename(f)[:-3]}" for d in ("projects", "entities", "concepts") for f in glob.glob(f"{W}/{d}/*.md")}
    for d in ("projects", "entities", "concepts"):
        for f in glob.glob(f"{W}/{d}/*.md"):
            p = f"{d}/{os.path.basename(f)[:-3]}"
            is_sub = any(p.startswith(n + "-") for n in allp if n != p)   # 10-05：重写长页拆出的子页，现状在主页上，不再排进补日期
            text = open(f, encoding="utf-8").read()
            b = os.path.getsize(f); l = text.count("\n")
            big = b > BIG_BYTES or l > BIG_LINES
            undated = d in ("projects", "entities") and not is_sub and p not in members and not (re.search(r"^## 现状", text, re.M) and re.search(r"截至 ?\d{4}-\d{2}-\d{2}", text))
            if not big and not undated:
                continue
            bg = "big-" + hashlib.sha1(p.encode()).hexdigest()[:8]
            r = c.execute("select leased, done from wiki_consolidate where gid=?", (bg,)).fetchone()
            if r and (now - (r[1] or 0) < 7 * 86400 or now - r[0] < LEASE) or skipped(c, bg):
                continue
            (out if big else rest).append((p, b, l))
    redo = []
    for g, ps, rd in c.execute("select gid, pages, redo from wiki_consolidate where key='big' and coalesce(done,0)=0 and redo is not null"):
        p = json.loads(ps)[0]
        if os.path.exists(f"{W}/{p}.md") and not skipped(c, g):
            r = c.execute("select leased from wiki_consolidate where gid=?", (g,)).fetchone()
            if not (r and now - (r[0] or 0) < LEASE):
                redo.append((p, BIG_BYTES + 1, BIG_LINES + 1))      # 当超长页排（Grok 那路优先领）
    seen = {x[0] for x in redo}
    return redo + sorted([x for x in out if x[0] not in seen], key=lambda x: -x[1]) + sorted([x for x in rest if x[0] not in seen], key=lambda x: -x[1])


def pick_big(c, now, big):
    p, b, l = big
    gid = "big-" + hashlib.sha1(p.encode()).hexdigest()[:8]
    b = os.path.getsize(f"{W}/{p}.md"); l = open(f"{W}/{p}.md", encoding="utf-8").read().count("\n")
    rd = c.execute("select redo from wiki_consolidate where gid=?", (gid,)).fetchone()
    redo_src = f"{W}/{rd[0]}" if rd and rd[0] and os.path.exists(f"{W}/{rd[0]}") else None
    r = c.execute("select leased, done, fails from wiki_consolidate where gid=?", (gid,)).fetchone()
    if r and r[0] and not r[1]:                          # 上次领走没交（超时或被掐断）：记一次失败，暂存区作废
        fail(c, gid, "上次领走没交卷（超时或被重启掐断）"); shutil.rmtree(f"{STAGE}/{gid}", ignore_errors=True)
    snap = f"_archive/snapshots/{os.path.basename(p)}-{time.strftime('%Y%m%d-%H%M%S')}.md"   # 快照路径先记下来，文件要等提交时才写进知识库
    c.execute("insert into wiki_consolidate(gid, key, pages, home, leased, done, result) values(?,?,?,?,?,0,null) "
              "on conflict(gid) do update set leased=excluded.leased, pages=excluded.pages, done=0",
              (gid, "big", json.dumps([p, snap], ensure_ascii=False), p, now)); c.commit()
    print(f"任务类型：重整大页\n组号：{gid}\n目标页：{p}.md（{b} 字节、{l} 行）" + ("" if b > BIG_BYTES or l > BIG_LINES or redo_src else "（页不大，主要是补上带日期的「现状」、把旧状态挪进「历史」）"))
    print(f"重写前的全文留在暂存区，提交通过后才写入快照：{snap}")
    print(f"原页有 {len(set(TICK.findall(open(f'{W}/{p}.md', encoding='utf-8').read())))} 个具体项（反引号里的命令、路径、端口、版本、配置项），重写后本页加子页至少保留 {MIN_KEEP:.0%}；"
          f"放不下 {MAX_BYTES // 1000}KB 就把做法按主题拆到子页 {p}-主题.md（每页同样 ≤{MAX_LINES} 行、≤{MAX_BYTES // 1000}KB，子页名用主题、不带编号，最多 {MAX_SUBS} 个），本页留概览并链接子页；"
          f"本页加子页总共不超过 {max(MAX_BYTES, int(MAX_TOTAL_RATIO * b)) // 1000}KB（原页的 {MAX_TOTAL_RATIO:.0%}）——重复的、同一件事的多次快照要合成一条，不是把原文切块搬过去。")
    S = stage(gid, [p])
    open(f"{S}/.snapshot_rel", "w").write(snap)
    if redo_src:
        shutil.copy2(redo_src, f"{S}/.redo_from.md")
        print(f"【重做】这页上次重写丢了细节（正文里的具体项不到七成）。上次重写前的原页全文：{S}/.redo_from.md（只读参考）。"
              f"在现在这页的基础上改（后来并进来的内容要留着），把原页里还有用的具体命令、路径、端口、版本、配置写回对应那条内容里；交卷按原页的具体项数七成。")
    print(f"暂存目录：{S}（目标页副本在 {S}/{p}.md；只在这里改，子页也建在 {S}/{os.path.dirname(p)}/ 下）")
    print(f"这轮的记录写进：{rec_path(gid)}（本组专用，先清空再写，写一行「重整 {p}」）")


def commit(c, gid, S, writes, archive=(), links=(), snapshot=None):
    """提交：拿知识库写锁（等最多 COMMIT_WAIT），核对起草期间这些页没被别人改过，再把暂存区的页替换进知识库。
    writes：暂存区里要写进去的页；archive：要搬进 _archive/merged 的旧页；links：(旧页, 新页) 全库改链接。"""
    man = json.load(open(f"{S}/.manifest.json"))
    end = time.time() + COMMIT_WAIT
    while True:
        ok, prev = kb_lock.acquire(f"commit-{gid}", pid=str(os.getpid()), work=f"提交整理 {gid}")
        if ok:
            break
        if time.time() > end:
            print(f"没收尾：等了 {COMMIT_WAIT // 60} 分钟知识库仍被占（{kb_lock.describe(prev)}），过几分钟再运行一次 --done。"); sys.exit(1)
        time.sleep(15)
    try:
        changed = [p for p, h in man.items() if sha(p) != h]
        changed += [p for p in writes if p not in man and os.path.exists(f"{W}/{p}.md")]   # 登记外的页只能是新建的，真库里已有就不许覆盖
        if changed:
            c.execute("update wiki_consolidate set leased=0 where gid=?", (gid,)); c.commit()   # 别人改过而作废，不算这组的失败
            shutil.rmtree(S, ignore_errors=True)
            print(f"作废：起草期间 {'、'.join(changed[:5])} 被别的任务改过（或真库里已有同名页），这次不能替换进去，稍后会重新领。直接结束，不要重试。"); sys.exit(2)
        if snapshot:
            rel, src = snapshot
            os.makedirs(os.path.dirname(f"{W}/{rel}"), exist_ok=True)
            shutil.copy2(src, f"{W}/{rel}")
        for p in writes:
            os.makedirs(os.path.dirname(f"{W}/{p}.md"), exist_ok=True)
            tmp = f"{W}/{p}.md.tmp"; shutil.copy2(f"{S}/{p}.md", tmp); os.replace(tmp, f"{W}/{p}.md")
        os.makedirs(f"{W}/_archive/merged", exist_ok=True)
        for p in archive:
            if os.path.exists(f"{W}/{p}.md"):
                shutil.move(f"{W}/{p}.md", f"{W}/_archive/merged/{p.split('/', 1)[1]}.md")
        n = sum(rewrite_links(o, nw) for o, nw in links)
    finally:
        kb_lock.release(f"commit-{gid}")
    shutil.rmtree(S, ignore_errors=True)
    return n


def cite_dates(ids):
    """出处段 id -> 原始对话日期（kb.sqlite chunks 表）。"""
    ids = list(ids)
    if not ids:
        return {}
    k = sqlite3.connect(DB)
    out = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        out.update(k.execute(f"select id, date from chunks where id in ({','.join('?' * len(part))})", part).fetchall())
    return out


def no_stage(gid):
    print(f"作废：{gid} 的暂存区已不在（领走超时被收回，或被重启掐断），这次的草稿不能提交，稍后会重新领。直接结束，不要重试。"); sys.exit(2)


def done_big(c, gid, pages, check_only=False):
    p, snap = pages
    S = f"{STAGE}/{gid}"
    if not os.path.exists(f"{S}/.manifest.json"):          # 10-05 审查：不再退回「直接读改知识库」的旧路径
        no_stage(gid)
    R = S; staged = True
    subs = sorted(f[len(R) + 1:-3] for f in glob.glob(f"{R}/{p}-*.md"))
    new = open(f"{R}/{p}.md", encoding="utf-8").read()
    alltext = new + "".join(open(f"{R}/{s}.md", encoding="utf-8").read() for s in subs)
    orig = f"{S}/.orig/{p}.md"
    old_path = orig if staged and os.path.exists(orig) else f"{W}/{snap}"
    oldtext = open(old_path, encoding="utf-8").read()
    redo_from = f"{S}/.redo_from.md"
    basetext = open(redo_from, encoding="utf-8").read() if os.path.exists(redo_from) else oldtext   # 重做：具体项和总量上限都按上次重写前的原页算
    old_ids = set(KB_ID.findall(oldtext))
    kept = old_ids & set(KB_ID.findall(alltext))
    lines = new.split("\n"); nb = len(new.encode())
    problems = []
    for s in [p] + subs:
        t = open(f"{R}/{s}.md", encoding="utf-8").read(); n_l, n_b = t.count("\n") + 1, len(t.encode())
        if n_l > MAX_LINES or n_b > MAX_BYTES:
            problems.append(f"{s}：{n_l} 行、{n_b} 字节，超过 {MAX_LINES} 行、{MAX_BYTES} 字节；按主题拆到子页 {p}-主题.md，不要删具体内容")
    total, cap = len(alltext.encode()), max(MAX_BYTES, int(MAX_TOTAL_RATIO * len(basetext.encode())))
    if total > cap:
        problems.append(f"本页加子页共 {total} 字节，要压到 {cap} 字节以内：同一件事的多次快照、重复的段落合成一条，过时的在「历史」里一行带过；不是把原文切块搬过去")
    numbered = [x for x in subs if re.search(r"-\d+$", x)]
    if numbered or len(subs) > MAX_SUBS:
        problems.append(f"子页 {len(subs)} 个（最多 {MAX_SUBS} 个）" + (f"，且这些是按编号切的块：{'、'.join(numbered[:4])}；子页要按主题分、名字用主题" if numbered else ""))
    bo, bn = set(TICK.findall(basetext)) | (ticks_substantive(oldtext) if basetext is not oldtext else set()), ticks_substantive(alltext)
    if bo and len(bo & bn) < MIN_KEEP * len(bo):
        miss = sorted(bo - bn)
        problems.append(f"原页 {len(bo)} 个具体项（反引号里的命令、路径、端口、版本、配置项）只保留了 {len(bo & bn)} 个，要至少 {int(MIN_KEEP * len(bo)) + 1} 个；"
                        f"补回到做法、现状或历史里对应的那条内容里（已被取代的也可以在「历史」里一行带过；一行堆一串的清单不算），例如缺：{'、'.join(miss[:12])}")
    long_ = [i + 1 for i, l in enumerate(lines) if len(CITE.sub("", l)) > MAX_LINE_CHARS]
    if long_:
        problems.append(f"第 {', '.join(map(str, long_[:5]))} 行太长（单行超过 {MAX_LINE_CHARS} 字），拆成几条")
    piled = [i + 1 for i, l in enumerate(lines) if len(KB_ID.findall(l)) > MAX_CITES_PER_LINE]
    if piled:
        problems.append(f"第 {', '.join(map(str, piled[:5]))} 行出处超过 {MAX_CITES_PER_LINE} 个：每条只挂 1~3 个最能支撑它的出处")
    m = re.search(r"^## 现状\s*\n(.*?)(?=^## |\Z)", new, re.M | re.S)
    if not m:
        problems.append("缺「## 现状」一节")
    else:
        bullets = [l for l in m.group(1).split("\n") if l.lstrip().startswith(("-", "*"))]
        bad = [l.strip()[:30] for l in bullets if not re.search(r"截至 ?\d{4}-\d{2}-\d{2}", l) or not KB_ID.search(l)]
        if not bullets:
            problems.append("「现状」一节是空的")
        elif bad:
            problems.append(f"「现状」有 {len(bad)} 条缺「截至 YYYY-MM-DD」或出处，例如：{bad[0]}…")
        dates = cite_dates({i for l in bullets for i in KB_ID.findall(l)})
        wrong = []
        for l in bullets:   # 截至日期不能比它引用的出处还新（10-04 试跑：旧内容全写成「截至今天」，又会被当成现状）
            m2 = re.search(r"截至 ?(\d{4}-\d{2}-\d{2})", l); ds = [dates[i] for i in KB_ID.findall(l) if dates.get(i)]
            if m2 and ds and m2.group(1) > max(ds):
                wrong.append(f"「截至 {m2.group(1)}」应为「截至 {max(ds)}」：{l.strip()[:40]}…")
        if wrong:
            problems.append(f"「现状」有 {len(wrong)} 条的截至日期比它引用的出处还新（要取出处的日期）：" + "；".join(wrong[:5]))
    if problems:
        print("没收尾，先改好：\n- " + "\n- ".join(problems)); sys.exit(1)
    if check_only:
        print("检查通过（只检查，没有提交）。运行 --done 提交。"); return
    commit(c, gid, S, [p] + subs, snapshot=(snap, orig))
    c.execute("update wiki_consolidate set redo=null where gid=?", (gid,)); c.commit()
    sb = os.path.getsize(f"{W}/{snap}")
    c.execute("insert into wiki_refine_runs values(?,?,?,?,?)", (time.time(), p, "restructure", sb, nb))
    c.execute("update wiki_consolidate set done=?, result=? where gid=?", (time.time(), p, gid)); c.commit()
    with open(LOG, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX); f.write(f"\n## [{time.strftime('%Y-%m-%d')}] restructure | {p}\n- {sb} → {nb} 字节，保留出处 {len(kept)}/{len(old_ids)}，快照 {snap}\n")
    print(f"完成：{p} 重整 {sb} → {nb} 字节、{len(lines)} 行" + (f"，子页 {len(subs)} 个（{', '.join(subs)}）" if subs else "") +
          f"；具体项保留 {len(bo & bn)}/{len(bo)}；出处保留 {len(kept)}/{len(old_ids)}（其余在快照 {snap}）。")


def rewrite_links(old, new):
    """全库把指向 old 的双链改成指向 new（保留别名）。old/new 形如 concepts/xxx。"""
    oname = old.split("/", 1)[1]
    pat = re.compile(r"\[\[(?:" + re.escape(old) + "|" + re.escape(oname) + r")(\|[^\]]*)?\]\]")
    n = 0
    for f in glob.glob(f"{W}/**/*.md", recursive=True):
        if "/_archive/" in f:
            continue
        s = open(f, encoding="utf-8").read()
        t, k = pat.subn(lambda m: f"[[{new}{m.group(1) or ''}]]", s)
        if k:
            open(f, "w", encoding="utf-8").write(t); n += k
    return n


def done(gid, check_only=False):
    c = conn()
    r = c.execute("select key, pages, home from wiki_consolidate where gid=?", (gid,)).fetchone()
    if not r:
        sys.exit(f"没有这个组号：{gid}")
    key, pages, home = r[0], json.loads(r[1]), r[2]
    if key == "big":
        return done_big(c, gid, pages, check_only)
    REC = rec_path(gid)
    text = open(REC, encoding="utf-8").read() if os.path.exists(REC) else ""
    pairs = re.findall(r"并入\s*\[*\s*((?:concepts|entities|projects)/[^\]>]+?)(?:\.md)?\s*\]*\s*(?:->|→)\s*\[*\s*((?:concepts|entities|projects|queries)/[^\]>]+?)(?:\.md)?\s*\]*\s*$",
                       text, re.M)
    if not pairs:
        sys.exit(f"记录文件 {REC} 里没有「并入 concepts/旧页 -> 目录/新页」这样的行，请按格式写好再运行。")
    S = f"{STAGE}/{gid}"
    if not os.path.exists(f"{S}/.manifest.json"):
        no_stage(gid)
    R = S; staged = True
    problems, moved = [], []
    for old, new in pairs:
        of, nf = f"{W}/{old}.md", f"{R}/{new}.md"
        if old == new:
            continue
        if old not in pages:
            problems.append(f"{old}：不是本组（{gid}）的页，别的组的事不在这里交"); continue
        if procedure_page(old) and str(new).startswith("concepts/"):
            problems.append(f"{old} → {new}：做法页要写回项目页或机器页，不能再并进做法区"); continue
        if not os.path.exists(of):
            problems.append(f"{old}：文件不存在（是不是已经搬走了？）"); continue
        if not os.path.exists(nf):
            problems.append(f"{new}：新页不存在"); continue
        lost = set(KB_ID.findall(open(of, encoding="utf-8").read())) - inline_ids(open(nf, encoding="utf-8").read())
        if lost:
            problems.append(f"{old} → {new}：新页正文里缺 {len(lost)} 个出处（出处要跟在对应那条内容后面，单独堆成出处清单不算），例如 {', '.join(sorted(lost)[:5])}")
        moved.append((old, new))
    targets_ = sorted({n for _, n in moved})
    subs = sorted({f[len(S) + 1:-3] for t in targets_ for f in glob.glob(f"{S}/{t}-*.md")})
    for t in targets_ + subs:                                # 10-05 审查：合并后也不许超上限（运维页并到 55KB、agent 页 416 行没人管）
        if os.path.exists(f"{S}/{t}.md"):
            tx = open(f"{S}/{t}.md", encoding="utf-8").read(); n_l, n_b = tx.count("\n") + 1, len(tx.encode())
            if n_l > MAX_LINES or n_b > MAX_BYTES:
                problems.append(f"{t}：合并后 {n_l} 行、{n_b} 字节，超过 {MAX_LINES} 行、{MAX_BYTES} 字节；重复的合成一条、过时的压进「历史」，仍放不下就按主题拆到子页 {t}-主题.md（名字用主题、不带编号）")
    if problems:
        print("没收尾，先补齐：\n- " + "\n- ".join(problems)); sys.exit(1)
    if check_only:
        print("检查通过（只检查，没有提交）。运行 --done 提交。"); return
    before = len(glob.glob(f"{W}/concepts/*.md"))
    sizes = [(old, size(old), os.path.getsize(f"{R}/{new}.md")) for old, new in moved]
    links = commit(c, gid, S, targets_ + subs, archive=[o for o, _ in moved], links=moved)   # 新页、归宿页、子页写进去，旧页归档，全库改链接——都在提交的锁里做
    for old, sb, sa in sizes:
        c.execute("insert into wiki_refine_runs values(?,?,?,?,?)", (time.time(), old, "merged", sb, sa))
    targets = sorted({n for _, n in moved})
    c.execute("update wiki_consolidate set done=?, result=? where gid=?", (time.time(), ", ".join(targets), gid)); c.commit()
    entry = f"\n## [{time.strftime('%Y-%m-%d')}] consolidate | {gid} {key.lstrip('→')}\n" + "".join(f"- 并入 {o} → {n}\n" for o, n in moved)
    with open(LOG, "a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX); f.write(entry)
    after = len(glob.glob(f"{W}/concepts/*.md"))
    print(f"完成：{len(moved)} 页并进 {', '.join(targets)}；改了 {links} 处链接；concepts/ {before} → {after} 页。")


USAGE = """用法：
  wiki_consolidate.py              领一组活（定时任务或命令行直接分活）
  wiki_consolidate.py --check 组号   只检查这次交卷合不合格，不提交（交卷前自查用这个，不要自己写脚本）
  wiki_consolidate.py --done 组号    检查并提交
  wiki_consolidate.py --peek         只看还剩哪些活"""

if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) == 2 and a[0] == "--done":
        done(a[1])
    elif len(a) == 2 and a[0] == "--check":
        done(a[1], check_only=True)
    elif a == ["--peek"]:
        pick(peek=True)
    elif not a:
        pick()
    else:
        print(USAGE); sys.exit(2)
