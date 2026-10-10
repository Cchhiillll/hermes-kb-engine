#!/usr/bin/env python3
"""知识库单写入锁（10-04 起）：同一时间只有一个任务改 ~/brain/wiki。

为什么：10-04 几路同时改知识库互相覆盖（4 路整理共用记录文件、同一页被 4 组同时改、并入/整理的互斥查的是废弃的表）。
规则：所有改知识库的任务领活前 acquire，交卷后 release；拿不到就这轮跳过。
锁文件 ~/brain/kb/write.lock（JSON）：holder 名字、job（Hermes 定时任务 id，可空）、script（领活脚本名，按它反查定时任务）、pid（可空）、since、work。
锁是否还有效：
  - 有 job（或 script 反查到的任务）：executions.db 里这个任务有 status=running、且在拿锁前后开始的执行 → 有效；执行结束、被重启掐断（unknown）→ 失效；
  - 有 pid：进程活着 → 有效；
  - 都判断不了：超过 MAX_HOLD 失效。
交卷被拒后在同一次执行里重跑 --done 不需要再拿锁；交卷成功时放锁。

  kb_lock.py status
  kb_lock.py acquire 名字 [--job 任务id] [--pid 进程号] [--work 说明]   成功退出码 0，被占退出码 1
  kb_lock.py release 名字
  kb_lock.py wait 名字 秒数 [--pid 进程号] [--work 说明]               等到拿到锁（给每晚 nightly 用），超时退出码 1
"""
import datetime as dt, fcntl, json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_config

_CFG = kb_config.load()
LOCK = _CFG.lock_file
EXEC_DB = os.path.join(_CFG.hermes_home, "cron", "executions.db")
MAX_HOLD = 90 * 60


def _read():
    try:
        return json.load(open(LOCK, encoding="utf-8"))
    except (OSError, ValueError):
        return None


JOBS = os.path.join(_CFG.hermes_home, "cron", "jobs.json")


def _jobs_for_script(script):
    """Hermes 不把任务编号传给脚本，这里按脚本名反查是哪几个定时任务。"""
    try:
        j = json.load(open(JOBS, encoding="utf-8"))
        return [x["id"] for x in (j["jobs"] if isinstance(j, dict) else j) if x.get("script") == script]
    except (OSError, ValueError, KeyError):
        return []


def _job_running(job, since):
    try:
        c = sqlite3.connect(f"file:{EXEC_DB}?mode=ro", uri=True, timeout=5)
        for st, started in c.execute("select status, started_at from executions where job_id=? order by started_at desc limit 5", (job,)):
            t = dt.datetime.fromisoformat(started).timestamp() if isinstance(started, str) else float(started or 0)
            if st == "running" and t >= since - 600:
                return True
        return False
    except Exception:
        return time.time() - since < MAX_HOLD      # 查不了执行记录就按时间兜底


def alive(h):
    """这把锁现在还算不算被占着。"""
    if not h:
        return False
    if h.get("job"):
        return _job_running(h["job"], h["since"])
    if h.get("script"):
        jobs = _jobs_for_script(h["script"])
        return any(_job_running(j, h["since"]) for j in jobs) if jobs else time.time() - h["since"] < MAX_HOLD
    if h.get("pid"):
        try:
            os.kill(int(h["pid"]), 0); return True
        except OSError:
            return False
    return time.time() - h["since"] < MAX_HOLD


def acquire(holder, job=None, pid=None, work="", script=None):
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK + ".mutex", "a+") as m:
        fcntl.flock(m, fcntl.LOCK_EX)
        h = _read()
        if h and alive(h):                 # 持有者那次执行还在跑，谁来都不给（同一路下一次执行开始时，上一次早已结束）
            return False, h
        new = {"holder": holder, "job": job, "pid": pid, "script": script, "since": time.time(), "work": work,
               "time": time.strftime("%F %T")}
        tmp = LOCK + ".tmp"
        json.dump(new, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, LOCK)
        return True, h if (h and h.get("holder") != holder) else None


def release(holder):
    with open(LOCK + ".mutex", "a+") as m:
        fcntl.flock(m, fcntl.LOCK_EX)
        h = _read()
        if h and h.get("holder") == holder:
            os.remove(LOCK); return True
        return False


def describe(h):
    if not h:
        return "空闲"
    return f"{h.get('holder')}（{h.get('work') or ''}，{h.get('time')} 起{'，仍在跑' if alive(h) else '，持有者已结束，可收回'}）"


if __name__ == "__main__":
    a = sys.argv[1:]
    opt = lambda k: a[a.index(k) + 1] if k in a else None
    if not a or a[0] == "status":
        print(describe(_read()))
    elif a[0] == "acquire":
        ok, prev = acquire(a[1], opt("--job"), opt("--pid"), opt("--work") or "")
        print(("拿到锁" + (f"（收回了已结束的 {prev.get('holder')}）" if prev else "")) if ok else f"被占：{describe(prev)}")
        sys.exit(0 if ok else 1)
    elif a[0] == "release":
        print("已放" if release(a[1]) else "不是持有者，没放")
    elif a[0] == "wait":
        end = time.time() + float(a[2])
        while time.time() < end:
            ok, prev = acquire(a[1], None, opt("--pid"), opt("--work") or "")
            if ok:
                print("拿到锁"); sys.exit(0)
            time.sleep(20)
        print(f"等了 {a[2]} 秒仍被占：{describe(_read())}"); sys.exit(1)
