#!/usr/bin/env python3
"""知识库提炼与并入的模型选择（10-10 起：单一 OpenAI 兼容端点）。

  export KB_MODEL="gemini-..."               # 模型名
  export OPENAI_BASE_URL="https://中转/v1"    # 端点
  export OPENAI_API_KEY="..."                # 密钥（只放环境变量或 ~/.hermes/.env）

pick() 返回 (名字, 模型, provider, 说明)；没配好时名字为 None，调用方据此让路（不领活、不叫醒模型）。
旧的 Grok / luna / Gemini 额度探测保留为可选：KB_LEGACY_ROUTING=1 才启用（依赖 Hermes 内部模块和非官方接口，不推荐）。
"""
import json, os, subprocess, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import kb_config

GROK_STOP, LUNA_5H, LUNA_WEEK = 80, 60, 80
LEGACY_MODELS = {
    "grok": ("grok-4.6", "xai-oauth"),
    "luna": ("gpt-6-luna", "modelversecpa"),
    "gemini": ("gemini-3.8-flash-high", "modelversecpa"),
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
    """10-10：没有探针时算「不知道」而不是「能用」（原来直接返回能用，让路逻辑形同虚设）。"""
    probe_cmd = os.environ.get("GEMINI_STATUS_CMD")
    if not probe_cmd:
        return False, "没有配置 GEMINI_STATUS_CMD 探针，状态未知"
    try:
        r = subprocess.run(probe_cmd, shell=True, capture_output=True, text=True, timeout=15)
        n = json.loads(r.stdout).get("gemini_usable", 0)
        return n > 0, f"可用号 {n} 个"
    except Exception as e:
        return False, f"探针读取失败（{str(e)[:40]}）"


def pick():
    cfg = kb_config.load()
    base = cfg.base_url or kb_config.hermes_env().get("OPENAI_BASE_URL", "")
    if cfg.model and base:
        return ("custom", cfg.model, cfg.provider, f"使用 KB_MODEL={cfg.model}（OpenAI 兼容端点）")
    notes = [f"KB_MODEL{'已' if cfg.model else '未'}设置、OPENAI_BASE_URL{'已' if base else '未'}设置"]
    if os.environ.get("KB_LEGACY_ROUTING") == "1":
        for name, fn in (("gemini", gemini), ("grok", grok), ("luna", luna)):
            try:
                ok, why = fn()
            except Exception as e:
                ok, why = False, f"出错（{str(e)[:40]}）"
            notes.append(f"{name}：{why}")
            if ok:
                return (name, *LEGACY_MODELS[name], "；".join(notes))
    return (None, None, None, "；".join(notes))


if __name__ == "__main__":
    name, model, provider, notes = pick()
    print(f"选中：{name or '未就绪'}（模型: {model}, provider: {provider}, 状态: {notes}）")
    sys.exit(0 if name else 1)
