#!/usr/bin/env python3
"""知识库提炼与并入模型路由。

支持通过环境变量灵活指定模型与提供商：
  export KB_MODEL="grok-4.6"          # 模型名称
  export KB_PROVIDER="custom"         # 提供商 / Provider
  export OPENAI_BASE_URL="..."        # 自定义网关地址
  export OPENAI_API_KEY="..."         # 认证密钥

若未显式指定环境变量，支持基于本地配置或探测命令自适应挑可用模型。
"""
import datetime as dt, json, os, subprocess, sys, time
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))

GROK_STOP, LUNA_5H, LUNA_WEEK = 80, 60, 80
MODELS = {
    "grok": ("grok-4.6", "xai-oauth"),
    "luna": ("gpt-6-luna", "modelversecpa"),
    "gemini": ("gemini-3.8-flash-high", "modelversecpa"),
    "default": ("gpt-4o-mini", "custom"),
}


def grok():
    try:
        from wiki_consolidate_grok import week_used
        u = week_used()
        return (u is not None and u < GROK_STOP), ("读不到" if u is None else f"本周已用 {u:.0f}%")
    except Exception as e:
        return False, f"未配置或读不到（{str(e)[:40]}）"


def luna():
    try:
        from wiki_consolidate_luna import luna_usage
        u = luna_usage()
        if u is None:
            return False, "读不到"
        return (u[0] < LUNA_5H and u[1] < LUNA_WEEK), f"5 小时 {u[0]}%、本周 {u[1]}%"
    except Exception as e:
        return False, f"未配置或读不到（{str(e)[:40]}）"


def gemini():
    # 优先支持环境变量指定的健康探测脚本或接口
    probe_cmd = os.environ.get("GEMINI_STATUS_CMD")
    if not probe_cmd:
        # 无自定义探针时，若配置了 Hermes 默认环境则直接视为可用
        return True, "默认启用"
    try:
        r = subprocess.run(probe_cmd, shell=True, capture_output=True, text=True, timeout=15)
        s = json.loads(r.stdout)
        n = s.get("gemini_usable", 0)
        return n > 0, f"可用号 {n} 个"
    except Exception as e:
        return False, f"探针读取失败（{str(e)[:40]}）"


def pick():
    # 1. 优先使用环境变量显式指定的模型
    env_m = os.environ.get("KB_MODEL")
    env_p = os.environ.get("KB_PROVIDER", "custom")
    if env_m:
        return ("custom", env_m, env_p, f"使用环境变量指定模型: {env_m} (provider: {env_p})")

    # 2. 依次探测备用模型
    notes = []
    for name, fn in (("gemini", gemini), ("grok", grok), ("luna", luna)):
        try:
            ok, why = fn()
        except Exception as e:
            ok, why = False, f"出错（{str(e)[:40]}）"
        notes.append(f"{name}：{why}")
        if ok and name in MODELS:
            return (name, *MODELS[name], "；".join(notes))

    # 3. 兜底默认模型
    return ("default", *MODELS["default"], f"回退至默认配置（{'；'.join(notes)}）")


if __name__ == "__main__":
    name, model, provider, notes = pick()
    print(f"选中：{name or '未就绪'}（模型: {model}, provider: {provider}, 状态: {notes}）")
