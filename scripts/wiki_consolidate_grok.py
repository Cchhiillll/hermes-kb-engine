#!/usr/bin/env python3
"""Grok 深加工入口。周额度用到 80%（大约还剩 20%）就停，不领活。停了也不影响 luna 那一路。"""
import json, os, sys, urllib.request, urllib.error

STOP_USED = 80


def should_stop(used):
    """used 是本周已用百分比。读不到也停，避免不知道还剩多少还继续花。"""
    return used is None or used >= STOP_USED


def _token(refresh=False):
    """从 Hermes 的凭证池取 Grok 令牌；refresh=True 时自动续期。"""
    os.environ.setdefault("HERMES_HOME", os.path.expanduser("~/.hermes"))
    os.environ.setdefault("HTTPS_PROXY", "http://127.0.0.1:10819"); os.environ.setdefault("HTTP_PROXY", "http://127.0.0.1:10819")
    sys.path.insert(0, os.path.expanduser("~/.hermes/hermes-agent"))
    from agent.credential_pool import load_pool
    p = load_pool("xai-oauth"); es = p.entries()
    if not es:
        return None
    e = es[0]
    if refresh:
        e = p.try_refresh_matching(credential_id=e.id) or e
    return e.runtime_api_key


def week_used():
    """只返回本周已用百分比；读不到返回 None。不打印令牌，也不打印账单原文。
    10-06：新一周还没用时接口不返回 creditUsagePercent，按 0 算（以前当成读不到，两路都停了）。"""
    proxy = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:10819"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    for refresh in (False, True):
        try:
            tok = _token(refresh)
            if not tok:
                return None
            req = urllib.request.Request("https://cli-chat-proxy.grok.com/v1/billing?format=credits",
                                         headers={"Authorization": "Bearer " + tok, "User-Agent": "grok-cli/1.0.44"})
            with opener.open(req, timeout=20) as r:
                cfg = json.loads(r.read().decode())["config"]
            if "currentPeriod" not in cfg:
                return None
            return float(cfg.get("creditUsagePercent") or 0)
        except urllib.error.HTTPError as e:
            if e.code != 401 or refresh:
                return None
        except Exception:
            return None
    return None


if __name__ == "__main__":
    used = week_used()
    if should_stop(used):
        why = "读不到" if used is None else f"本周已用 {used:.0f}%"
        print(f"NO_TARGET：Grok 周额度{why}，剩下约 20% 就停，这轮不整理。")
        print(json.dumps({"wakeAgent": False}))
    else:
        sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
        import wiki_consolidate
        wiki_consolidate.pick(prefer="big")
