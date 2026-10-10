"""kb-recall 的逻辑（__init__.py 每轮检查本文件修改时间，改了自动重新加载，改这里不用重启网关）。
10-04：检索结果注明页面最后更新日期，并说明笔记是当时的情况、会变的状态以现场查为准（用户：旧记录被当成现状）。
每轮对话前：①问的是"现在/还剩/进度"这类现状问题时，附上现状页 ~/brain/kb/now.md（脚本每 15 分钟实测）；
②从 Hermes 自己的知识库（~/brain/wiki）和语雀文档（~/brain/sources/yuque，配置了才查）里找出最相关的几页，附上标题、一句话摘要、路径（语雀附原文链接）。

- 只在 KB_RECALL_PLATFORMS 列出的平台生效（默认飞书和命令行）；定时任务不加。
- QMD 地址 KB_MCP_URL、检索集合 KB_RECALL_COLLECTIONS（默认 wiki）均可配置。
- 查法：关键词 + 语义两路、关掉本地重排（rerank:false），检索仅需 1~2 秒。
- 查不到、超时、出错都静默跳过，不影响对话。
"""
import json, os, re, sys, time, urllib.request, logging

log = logging.getLogger("kb-recall")


def _cfg():
    """10-10：路径和地址可配置。能 import 到 kb_config（~/.hermes/scripts 或仓库 scripts/）就用它，否则只看环境变量。"""
    here = os.path.dirname(os.path.realpath(__file__))
    for p in (os.path.join(here, "..", "..", "scripts"), os.path.expanduser("~/.hermes/scripts")):
        if os.path.isdir(p) and p not in sys.path:
            sys.path.append(p)
    try:
        import kb_config
        c = kb_config.load()
        return c.mcp_url, c.wiki_dir, c.now_file, c.yuque_dir
    except Exception:
        return (os.environ.get("KB_MCP_URL", "http://127.0.0.1:8181/mcp"),
                os.path.expanduser(os.environ.get("KB_WIKI_DIR", "~/brain/wiki")),
                os.path.expanduser(os.environ.get("KB_NOW_FILE", "~/brain/kb/now.md")),
                os.path.expanduser(os.environ.get("KB_YUQUE_DIR", "~/brain/sources/yuque")))


MCP, WIKI, NOW, YUQUE = _cfg()
ROOTS = {"wiki": WIKI, "yuque": YUQUE}          # QMD 集合名 -> 本地目录（命中结果的 file 形如「集合/相对路径」）
PLATFORMS = {p.strip() for p in os.environ.get("KB_RECALL_PLATFORMS", "feishu,cli").split(",") if p.strip()}
# 默认查 wiki；语雀目录存在时加上 yuque 集合（10-10）。查询出错时退回只查第一个集合（比如 yuque 集合还没在 QMD 里建）。
COLLECTIONS = [c.strip() for c in (os.environ.get("KB_RECALL_COLLECTIONS")
                                   or ("wiki,yuque" if os.path.isdir(YUQUE) else "wiki")).split(",") if c.strip()]
SKIP_FILES = re.compile(r"(^|/)(index\.md|log[^/]*\.md|SCHEMA\.md)$|/_meta/|/_archive/")
TOP, MIN_SCORE, TIMEOUT = 4, 0.3, 4
STATUS_Q = re.compile(r"现在|目前|当前|还剩|剩多少|额度|进度|状态|在跑|跑着|通不通|正常吗|正常不|挂了|好了没|多少了|怎么样了|最近在|在做什么|在忙|还活着|出错|报错|卡住|停了")


def _status():
    try:
        age = int((time.time() - os.path.getmtime(NOW)) / 60)
        txt = open(NOW, encoding="utf-8").read()[:3500]
    except OSError:
        return ""
    return (f"【现状页（{age} 分钟前由脚本实测，每 15 分钟更新一次）】\n{txt}\n"
            "回答\"现在怎么样\"这类问题以这页为准；页里没有的，再自己去查。\n\n")


def _post(payload, sid=None):
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if sid:
        h["mcp-session-id"] = sid
    r = urllib.request.urlopen(urllib.request.Request(MCP, json.dumps(payload).encode(), h), timeout=TIMEOUT)
    return r.headers.get("mcp-session-id"), r.read().decode()


def _search(text):
    try:
        return _query(text, COLLECTIONS)
    except Exception:
        if len(COLLECTIONS) < 2:
            raise
        return _query(text, COLLECTIONS[:1])


def _query(text, collections):
    sid, _ = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "kb-recall", "version": "1"}}})
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, body = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "query", "arguments": {
        "searches": [{"type": "lex", "query": text}, {"type": "vec", "query": text}],
        "limit": TOP + 3, "rerank": False, "collections": collections}}}, sid)
    data = json.loads(body.split("data: ", 1)[1] if body.startswith("event:") else body)
    if data.get("error") or (data.get("result") or {}).get("isError"):
        raise RuntimeError(str(data.get("error") or "query 返回错误")[:80])
    return (data.get("result", {}).get("structuredContent") or {}).get("results", [])


def _page(rel, root=None):
    """返回（页面登记的标题，开头的「> 一句话：…」或正文第一段，页面最后更新日期, 原文链接）。"""
    try:
        raw = open(os.path.join(root or WIKI, rel), encoding="utf-8").read(6000)
    except OSError:
        return "", "", "", ""
    fm = re.match(r"^---\n(.*?)\n---\n", raw, re.S)
    title = (re.search(r"^title:\s*(.+)$", fm.group(1), re.M).group(1).strip().strip('"') if fm and re.search(r"^title:", fm.group(1), re.M) else "")
    body = raw[fm.end():] if fm else raw
    upd = re.search(r"^updated(?:_at)?:\s*\"?([0-9]{4}-[0-9]{2}-[0-9]{2})", fm.group(1), re.M) if fm else None
    url = re.search(r'^url:\s*"?([^"\s]+)', fm.group(1), re.M) if fm else None
    m = re.search(r"^>(?!\s*上下文：)\s*(?:一句话[：:])?\s*(.+)$", body, re.M)
    line = m.group(1) if m else next((l for l in body.splitlines()
                                      if l.strip() and not l.startswith(("#", ">")) and not re.fullmatch(r"\s*[-*_]{3,}\s*", l)
                                      and not re.match(r"^[A-Za-z_]+:\s", l)), "")
    line = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", line)   # [[a|b]] → b
    line = re.sub(r"\^\[kb:[^\]]+\]", "", line).strip(" -")
    return title, line[:160], (upd.group(1) if upd else ""), (url.group(1) if url else "")


def recall(user_message="", platform=None, **kw):
    if platform not in PLATFORMS:
        return None
    msg = (user_message or "").strip()
    if len(msg) < 4 or msg.startswith("[") or "CONTEXT COMPACTION" in msg:
        return None
    status = _status() if STATUS_Q.search(msg) else ""
    if len(msg) < 6:
        return status or None
    t = time.time()
    lines = []
    try:
        hits = _search(msg[:300])
    except Exception as e:
        log.info("kb-recall 检索跳过（%s）", str(e)[:80])
        hits = []
    for h in hits:
        f = (h.get("file") or "").removeprefix("qmd://")
        coll, _, rel = f.partition("/") if f.split("/", 1)[0] in ROOTS else ("wiki", "", f)
        if not rel or SKIP_FILES.search("/" + rel) or (h.get("score") or 0) < MIN_SCORE:
            continue
        title, summ, upd, url = _page(rel, ROOTS[coll])
        if coll == "yuque":
            lines.append(f"- 〔语雀〕《{title or h.get('title') or rel}》（yuque/{rel}，语雀更新于 {upd or '不详'}"
                         + (f"，原文 {url}" if url else "") + f"）：{summ}")
        else:
            lines.append(f"- 《{title or h.get('title') or rel}》（{rel}，页面最后更新 {upd or '不详'}）：{summ}")
        if len(lines) >= TOP:
            break
    log.info("kb-recall 现状页%s，知识库 %d 条，%.1f 秒", "附上" if status else "未附", len(lines), time.time() - t)
    kb = ("【你的知识库里和这句话可能相关的页面（自动检索，供参考）】\n" + "\n".join(lines) +
          "\n这些是从过去对话整理的笔记，记的是当时的情况：经验、做法和用户的要求可以照着用；"
          "机器装没装什么、服务和任务在不在跑、配置和授权是什么这类会变的状态，以现场查到的为准——笔记只告诉你去哪查，"
          "说现状前先跑命令看（ls/which/systemctl/cron list/读配置），没查就说「笔记里记的是某日的情况，现在没核实」。"
          "相关的就先读全文（qmd 的 get，知识页路径前加 wiki/、语雀文档用上面给的 yuque/ 路径）；不相关就忽略。") if lines else ""
    return (status + kb) or None
