#!/usr/bin/env python3
"""知识库每天的提炼、并入用哪家模型（10-06 起）。
用户 10-06：「10.8 号前的主力还是 grok 和 luna，国庆假期结束可以放开 gemini」；10-06 下午 Grok 一家出问题（令牌过期 + 新周不返回用量），
提炼、并入、两路整理全压在它身上，停了 11 小时——所以按顺序挑第一家能用的，不再一家出问题全停：
  1. Grok：本周已用 < 80%（用户：剩两成就停）
  2. luna：5 小时窗口 < 60%、本周 < 80%（luna 还要给每晚自查等日常任务留量）
  3. Gemini：GEMINI_FROM 起才用；可用号 > 3（给飞书聊天留 3 个）
都不能用就返回 None（调用方停这一轮；停工报警 kb_stall_alert.py 会报）。
  kb_models.py   打印三家现在能不能用、选中哪家
"""
import datetime as dt, json, os, subprocess, sys, time
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))

GEMINI_FROM = "2026-10-08"
GROK_STOP, LUNA_5H, LUNA_WEEK = 80, 60, 80
MODELS = {"grok": ("grok-4.6", "xai-oauth"), "luna": ("gpt-6-luna", "modelversecpa"), "gemini": ("gemini-3.8-flash-high", "modelversecpa")}


def grok():
    from wiki_consolidate_grok import week_used
    u = week_used()
    return (u is not None and u < GROK_STOP), ("读不到" if u is None else f"本周已用 {u:.0f}%")


def luna():
    from wiki_consolidate_luna import luna_usage
    u = luna_usage()
    if u is None:
        return False, "读不到"
    return (u[0] < LUNA_5H and u[1] < LUNA_WEEK), f"5 小时 {u[0]}%、本周 {u[1]}%"


def gemini():
    if dt.date.today().isoformat() < GEMINI_FROM:
        return False, f"{GEMINI_FROM} 起才用"
    try:
        r = subprocess.run(["ssh", "-i", os.path.expanduser("~/.ssh/cpa_status"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                            "ubuntu@47.130.146.113"], capture_output=True, text=True, timeout=30)
        s = json.loads(r.stdout)
        if time.time() - s["generated"] > 600:
            return False, "可用号数据过期"
        n = s.get("gemini_usable", 0)
        return n > 3, f"可用号 {n} 个"
    except Exception as e:
        return False, f"读不到（{str(e)[:40]}）"


def pick():
    """返回 (名字, 模型, provider, 三家状态说明)；都不能用时名字为 None。"""
    notes = []
    for name, fn in (("grok", grok), ("luna", luna), ("gemini", gemini)):
        try:
            ok, why = fn()
        except Exception as e:
            ok, why = False, f"出错（{str(e)[:40]}）"
        notes.append(f"{name}：{why}")
        if ok:
            return (name, *MODELS[name], "；".join(notes))
    return (None, None, None, "；".join(notes))


if __name__ == "__main__":
    name, model, provider, notes = pick()
    print(f"选中：{name or '都不能用'}（{notes}）")
