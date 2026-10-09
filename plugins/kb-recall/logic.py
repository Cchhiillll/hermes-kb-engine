"""kb-recall 的逻辑（__init__.py 每轮检查本文件修改时间，改了自动重新加载，改这里不用重启网关）。
10-04：检索结果注明页面最后更新日期，并说明笔记是当时的情况、会变的状态以现场查为准（用户：旧记录被当成现状）。
每轮对话前：①问的是"现在/还剩/进度"这类现状问题时，附上现状页 ~/brain/kb/now.md（脚本每 15 分钟实测）；
②从 Hermes 自己的知识库（~/brain/wiki）里找出最相关的几页，附上标题、一句话摘要和路径。

- 只在飞书（和命令行测试）对话里生效；定时任务不加。
- 查法：关键词 + 语义两路、关掉本地重排（rerank:false），检索仅需 1~2 秒。
- 查不到、超时、出错都静默跳过，不影响对话。
"""
import json, os, re, time, urllib.request, logging

log = logging.getLogger("kb-recall")
MCP = "http://127.0.0.1:8181/mcp"
WIKI = os.path.expanduser("~/brain/wiki")
PLATFORMS = {"feishu", "cli"}
SKIP_FILES = re.compile(r"(^|/)(index\.md|log[^/]*\.md|SCHEMA\.md)$|/_meta/|/_archive/")
TOP, MIN_SCORE, TIMEOUT = 4, 0.3, 4
NOW = os.path.expanduser("~/brain/kb/now.md")
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
    sid, _ = _post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "kb-recall", "version": "1"}}})
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    _, body = _post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "query", "arguments": {
        "searches": [{"type": "lex", "query": text}, {"type": "vec", "query": text}],
        "limit": TOP + 3, "rerank": False, "collections": ["wiki"]}}}, sid)
    data = json.loads(body.split("data: ", 1)[1] if body.startswith("event:") else body)
    return (data.get("result", {}).get("structuredContent") or {}).get("results", [])


def _page(rel):
    """返回（页面登记的标题，开头的「> 一句话：…」或正文第一段，页面最后更新日期）。"""
    try:
        raw = open(os.path.join(WIKI, rel), encoding="utf-8").read(6000)
    except OSError:
        return "", "", ""
    fm = re.match(r"^---\n(.*?)\n---\n", raw, re.S)
    title = (re.search(r"^title:\s*(.+)$", fm.group(1), re.M).group(1).strip() if fm and re.search(r"^title:", fm.group(1), re.M) else "")
    body = raw[fm.end():] if fm else raw
    upd = re.search(r"^updated:\s*(\S+)", fm.group(1), re.M) if fm else None
    m = re.search(r"^>\s*(?:一句话[：:])?\s*(.+)$", body, re.M)
    line = m.group(1) if m else next((l for l in body.splitlines()
                                      if l.strip() and not l.startswith("#") and not re.fullmatch(r"\s*[-*_]{3,}\s*", l)
                                      and not re.match(r"^[A-Za-z_]+:\s", l)), "")
    line = re.sub(r"\[\[([^\]|]+\|)?([^\]]+)\]\]", r"\2", line)   # [[a|b]] → b
    line = re.sub(r"\^\[kb:[^\]]+\]", "", line).strip(" -")
    return title, line[:160], (upd.group(1).strip('"') if upd else "")


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
        rel = (h.get("file") or "").removeprefix("wiki/")
        if not rel or SKIP_FILES.search("/" + rel) or (h.get("score") or 0) < MIN_SCORE:
            continue
        title, summ, upd = _page(rel)
        lines.append(f"- 《{title or h.get('title') or rel}》（{rel}，页面最后更新 {upd or '不详'}）：{summ}")
        if len(lines) >= TOP:
            break
    log.info("kb-recall 现状页%s，知识库 %d 条，%.1f 秒", "附上" if status else "未附", len(lines), time.time() - t)
    kb = ("【你的知识库里和这句话可能相关的页面（自动检索，供参考）】\n" + "\n".join(lines) +
          "\n这些是从过去对话整理的笔记，记的是当时的情况：经验、做法和用户的要求可以照着用；"
          "机器装没装什么、服务和任务在不在跑、配置和授权是什么这类会变的状态，以现场查到的为准——笔记只告诉你去哪查，"
          "说现状前先跑命令看（ls/which/systemctl/cron list/读配置），没查就说「笔记里记的是某日的情况，现在没核实」。"
          "相关的就先读全文（qmd 的 get，路径前加 wiki/）；不相关就忽略。") if lines else ""
    return (status + kb) or None
