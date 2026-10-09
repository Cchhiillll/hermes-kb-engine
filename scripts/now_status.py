#!/usr/bin/env python3
"""通用系统与知识库现状看板探针。

由定时任务（如每 15 分钟）定期触发执行，零大模型调用，直接抓取真实指标输出到 ~/brain/kb/now.md。
支持通过环境变量自定义受监控的服务和探针：
  MONITOR_SERVICES="unit:名称,unit2:名称2"
  QUOTA_PROBE_CMD="bash 命令获取额度 JSON"
"""
import glob, json, os, platform, re, sqlite3, subprocess, time

H = os.path.expanduser
OUT, STATE = H(os.environ.get("NOW_MD_PATH", "~/brain/kb/now.md")), H(os.environ.get("NOW_STATE_PATH", "~/brain/kb/now_state.json"))

# 服务监控列表（支持环境变量注入）
raw_services = os.environ.get("MONITOR_SERVICES")
if raw_services:
    SERVICES = [tuple(item.split(":", 1)) for item in raw_services.split(",") if ":" in item]
else:
    SERVICES = [("hermes-gateway", "Hermes 网关"), ("qmd-mcp", "QMD 检索服务")]


def sh(cmd, timeout=15):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def ago(ts, now):
    if not ts: return "无记录"
    m = int((now - ts) / 60)
    return f"{m} 分钟前" if m < 120 else f"{m // 60} 小时前" if m < 2880 else f"{m // 1440} 天前"


def ro(p):
    return sqlite3.connect(f"file:{H(p)}?mode=ro", uri=True)


def main():
    now = time.time()
    st = {}
    lines = [f"# 现状看板（{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))} 实测）", ""]

    # 1. 关键服务状态
    svc = {}
    lines.append("## 服务状态")
    for unit, name in SERVICES:
        state = sh(f"systemctl --user is-active {unit} 2>/dev/null") or sh(f"systemctl is-active {unit} 2>/dev/null") or "未知"
        since = sh(f"systemctl --user show {unit} -p ActiveEnterTimestamp --value 2>/dev/null")
        svc[name] = state
        since_str = " ".join(since.split()[1:3])[:16] if since else ""
        lines.append(f"- {name}：{'正常' if state == 'active' else '⚠️ ' + state}" + (f"（{since_str} 起）" if state == "active" and since_str else ""))
    st["svc"] = svc

    # 2. 定时任务健康状态
    lines += ["", "## Hermes 定时任务"]
    cron_jobs_file = H("~/.hermes/cron/jobs.json")
    if os.path.exists(cron_jobs_file):
        try:
            d = json.load(open(cron_jobs_file))
            jobs = d["jobs"] if isinstance(d, dict) else d
            on = [j for j in jobs if j.get("enabled", True) and j.get("state") != "paused"]
            lines.append(f"- 运行中 {len(on)} 个，已暂停 {len(jobs) - len(on)} 个")
        except Exception as e:
            lines.append(f"- ⚠️ 任务配置读取失败: {str(e)[:50]}")
    else:
        lines.append("- 未配置 jobs.json")

    # 3. 知识库指标
    lines += ["", "## 知识库概况"]
    W = H("~/brain/wiki")
    if os.path.exists(W):
        cnt = {d: len(glob.glob(f"{W}/{d}/*.md")) for d in ("projects", "entities", "concepts")}
        lines.append(f"- 页面数量：项目 {cnt['projects']}、实体 {cnt['entities']}、方法/经验 {cnt['concepts']}")
    kb_db = H("~/brain/kb/kb.sqlite")
    if os.path.exists(kb_db):
        try:
            k = ro(kb_db)
            total = k.execute("select count(*) from chunks where src != 'page'").fetchone()[0]
            lines.append(f"- 收录对话：总计 {total} 个切片片段")
            st["chunks_total"] = total
        except Exception as e:
            lines.append(f"- ⚠️ 对话数据库查询异常: {str(e)[:50]}")

    # 4. 机器资源负载
    hostname = platform.node()
    lines += ["", f"## 宿主机负载 ({hostname})"]
    try:
        la = os.getloadavg()
        lines.append(f"- CPU 负载: {la[0]:.2f}, {la[1]:.2f}, {la[2]:.2f} (核心数: {os.cpu_count()})")
    except Exception:
        pass
    if os.path.exists("/proc/meminfo"):
        try:
            mi = dict(l.split(":", 1) for l in open("/proc/meminfo"))
            mem = f"{int(mi['MemAvailable'].split()[0]) / 1048576:.1f}G / {int(mi['MemTotal'].split()[0]) / 1048576:.1f}G"
            lines.append(f"- 内存可用: {mem}")
        except Exception:
            pass
    disk = sh("df -h / | awk 'NR==2{print $4\"/\"$2}'")
    if disk:
        lines.append(f"- 根分区磁盘剩余: {disk}")

    # 5. 落盘并对比变化
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    # 保存状态
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
