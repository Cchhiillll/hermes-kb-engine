#!/usr/bin/env python3
"""现状页（Hermes cron「Hermes-现状页」，--no-agent，每 15 分钟，不调模型）。
把"现在怎么样"实际查一遍，写成 ~/brain/kb/now.md：服务、代理、额度、定时任务、知识库、最近在忙什么、机器负载，
以及和上一次相比的变化（~/brain/kb/now_state.json）。kb-recall 插件在他问"现在/还剩/进度"这类问题时把这页带给 Hermes。
标准输出保持为空（--no-agent 模式下有输出就会发飞书）。"""
import json, os, re, sqlite3, subprocess, time, glob

H = os.path.expanduser
OUT, STATE = H("~/brain/kb/now.md"), H("~/brain/kb/now_state.json")
SERVICES = [("hermes-gateway", "Hermes（飞书）"), ("openclaw-gateway", "小火龙"), ("cc-connect-agy", "佐佐木"),
            ("qmd-mcp", "知识库检索"), ("v2ray-user", "本机代理"), ("netflix-watcher", "奈飞验证码监听")]

def sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""

def ago(ts, now):
    if not ts: return "无记录"
    m = int((now - ts) / 60)
    return f"{m} 分钟前" if m < 120 else f"{m // 60} 小时前" if m < 2880 else f"{m // 1440} 天前"

def hm(ts): return time.strftime("%m-%d %H:%M", time.localtime(ts)) if ts else "?"

def ro(p): return sqlite3.connect(f"file:{H(p)}?mode=ro", uri=True)

def main():
    now = time.time()
    st = {}
    lines = [f"# 现状（{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} 实测，每 15 分钟更新）", ""]

    # 1. 服务
    svc = {}
    for unit, name in SERVICES:
        state = sh(f"systemctl --user is-active {unit}") or "未知"
        since = sh(f"systemctl --user show {unit} -p ActiveEnterTimestamp --value")
        svc[name] = state
        since = " ".join(since.split()[1:3])[:16]   # "Sat 2026-10-03 11:19:23 CST" → "2026-10-03 11:19"
        lines.append(f"- {name}：{'正常' if state == 'active' else '⚠️ ' + state}" + (f"（{since} 起）" if state == "active" and since else ""))
    st["svc"] = svc
    t = sh("curl -s -o /dev/null -m 10 -x http://127.0.0.1:10819 -w '%{http_code} %{time_total}' https://www.google.com/generate_204")
    ok = t.startswith("204")
    st["proxy"] = ok
    lines.append(f"- 代理出网：{'通，' + t.split()[1][:4] + ' 秒' if ok else '⚠️ 不通（' + (t or '超时') + '）'}")
    lines.insert(2, "## 服务")

    # 2. 额度（线上 CPA 每 5 分钟导出）
    lines += ["", "## 额度"]
    try:
        c = json.loads(sh(f"ssh -i {H('~/.ssh/cpa_status')} -o BatchMode=yes -o ConnectTimeout=10 ubuntu@47.130.146.113", 30))
        st["gemini_usable"] = c.get("gemini_usable")
        lines.append(f"- Gemini：{c.get('gemini_total', '?')} 个号里 {c.get('gemini_usable', '?')} 个可用（周额度没到顶）；飞书聊天和知识库整理都用它")
        lines.append(f"- Codex（luna）：5 小时窗口已用 {c.get('used', '?')}%（{hm(c.get('reset_at'))} 重置），本周已用 {c.get('week_used', '?')}%（{hm(c.get('week_reset_at'))} 重置）；"
                     f"这组数取自最近一次 Codex 调用（{ago(c.get('ts'), now)}），之后没再调用就不会更新")
    except Exception:
        lines.append("- ⚠️ 这次没取到线上额度数据（连 CPA 失败）")

    # 3. 定时任务
    lines += ["", "## Hermes 定时任务"]
    try:
        d = json.load(open(H("~/.hermes/cron/jobs.json")))
        jobs = d["jobs"] if isinstance(d, dict) else d
        fails, last = {}, {}
        for jid, err in ro("~/.hermes/cron/executions.db").execute(
                "select job_id, started_at from executions where status='failed' and started_at >= ? order by started_at",
                (time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now - 86400)),)):
            fails[jid] = fails.get(jid, 0) + 1; last[jid] = err
        on = [j for j in jobs if j.get("enabled", True) and j.get("state") != "paused"]
        lines.append(f"- 在跑 {len(on)} 个，停着 {len(jobs) - len(on)} 个")
        bad = [(j["name"], fails[j["id"]], str(last[j["id"]])[5:16].replace("T", " ")) for j in jobs if fails.get(j["id"])]
        lines.append("- 最近 24 小时出错：" + ("、".join(f"{n} {k} 次（最近一次 {t}）" for n, k, t in bad) if bad else "没有"))
        st["fail_jobs"] = sorted(n for n, _, _ in bad)
    except Exception as e:
        lines.append(f"- ⚠️ 读任务列表出错：{str(e)[:60]}")

    # 4. 知识库
    lines += ["", "## 知识库"]
    W = H("~/brain/wiki")
    cnt = {d: len(glob.glob(f"{W}/{d}/*.md")) for d in ("projects", "entities", "concepts")}
    lines.append(f"- 页面：项目 {cnt['projects']}、实体 {cnt['entities']}、做法/经验 {cnt['concepts']}")
    try:
        k = ro("~/brain/kb/kb.sqlite")
        total = k.execute("select count(*) from chunks where src != 'page'").fetchone()[0]
        import sys; sys.path.insert(0, H("~/.hermes/scripts")); from wiki_feed import SKIP   # 和读历史同一口径：自动审批子会话等不算
        unread = k.execute(f"select count(*) from chunks k where k.src != 'page' and {SKIP} and k.id not in (select id from hermes_read where done is not null)").fetchone()[0]
        lines.append(f"- 对话：共 {total} 段，还没整理进知识库 {unread} 段")
        try:   # 深加工进度（wiki_target.py 每轮记进 wiki_refine_runs，10-03 加）
            rr = k.execute("select count(*), count(distinct page) from wiki_refine_runs where ts >= ?", (now - 86400,)).fetchone()
            dn = k.execute("select count(*) from wiki_refine where size_at_done > 0").fetchone()[0]
            allp = sum(cnt.values())
            lines.append(f"- 深加工：近 24 小时整理 {rr[0]} 轮、{rr[1]} 个不同页面；整页整理过 {dn}/{allp} 页")
        except Exception:
            pass
        st["unread"] = unread
        nl = H("~/brain/.state/nightly.log")
        lines.append(f"- 每晚同步新对话：上次跑完 {ago(os.path.getmtime(nl), now) if os.path.exists(nl) else '无记录'}")
        # 5. 最近在忙什么（按最近 3 天的对话量）
        rows = k.execute("select coalesce(project,'-'), count(*), max(date) from chunks where src != 'page' and ts >= ? "
                         "group by 1 order by 2 desc limit 8", (now - 3 * 86400,)).fetchall()
        lines += ["", "## 最近 3 天在忙什么（按和 agent 的对话量）"]
        lines += [f"- {p}：{n} 段，最近 {d}" for p, n, d in rows] or ["- 最近 3 天没有新对话"]
    except Exception as e:
        lines.append(f"- ⚠️ 读对话库出错：{str(e)[:60]}")
    sc = H("~/brain/kb/selfcheck.md")
    if os.path.exists(sc):
        rows = [l for l in open(sc).read().splitlines() if l.startswith("| ") and not l.startswith("| 日期") and "---" not in l]
        if rows: lines += ["", "## 最近一次自查（每晚 03:30）", "| 日期 | 他问了几次 | 回复耗时中位数 | 超过3分钟 | 回复字数中位数 | 交后台几次 | 被纠正/不满 | 返工 | 当天改了什么 |", "|---|---|---|---|---|---|---|---|---|", rows[-1]]

    # 6. 机器
    la = os.getloadavg()
    mi = dict(l.split(":", 1) for l in open("/proc/meminfo"))
    mem = f"{int(mi['MemAvailable'].split()[0]) / 1048576:.0f}/{int(mi['MemTotal'].split()[0]) / 1048576:.0f}"
    disk = sh("df -h / | awk 'NR==2{print $4\"/\"$2}'")
    lines += ["", "## ThinkPad 本机", f"- 负载 {la[0]:.1f}（{os.cpu_count()} 核）；内存可用 {mem} GB；硬盘剩 {disk}"]

    # 7. 和上次比的变化
    try: prev = json.load(open(STATE))
    except Exception: prev = {}
    ch = []
    for name, s in svc.items():
        p = (prev.get("svc") or {}).get(name)
        if p and p != s: ch.append(f"{name}：{'恢复正常' if s == 'active' else '从正常变成 ' + s}")
    if "proxy" in prev and prev["proxy"] != st["proxy"]: ch.append("代理：" + ("恢复" if st["proxy"] else "变成不通"))
    if prev.get("gemini_usable") is not None and st.get("gemini_usable") is not None and prev["gemini_usable"] != st["gemini_usable"]:
        ch.append(f"Gemini 可用号：{prev['gemini_usable']} → {st['gemini_usable']}")
    new_fail = sorted(set(st.get("fail_jobs", [])) - set(prev.get("fail_jobs", [])))
    if new_fail: ch.append("新出错的任务：" + "、".join(new_fail))
    lines[1:1] = ["", "## 和上次（" + (hm(prev.get("at")) if prev.get("at") else "无") + "）比的变化", *(f"- {x}" for x in ch or ["没有变化"])]
    st["at"] = now

    tmp = OUT + ".tmp"
    open(tmp, "w").write("\n".join(lines) + "\n"); os.replace(tmp, OUT)
    json.dump(st, open(STATE, "w"), ensure_ascii=False)

if __name__ == "__main__":
    main()
