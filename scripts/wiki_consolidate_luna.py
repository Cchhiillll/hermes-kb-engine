#!/usr/bin/env python3
"""「知识库-深加工」（luna）收敛分活入口（10-04 起换成 wiki_consolidate）；原，10-03 用户批：加一路 luna 深加工，试一天后和 Gemini 那路比产出。
luna 也负责每天把新对话并入知识库，所以这里设闸：现状页（now.md，每 15 分钟实测）里 luna 的
5 小时窗口用到 LIMIT_5H% 或本周用到 LIMIT_WEEK%，就让路（不分活、不调模型）；读不到用量也让路。
其余和 wiki_target.py 完全一样（同一张租约表，两路不会领到同一页）。--done 仍用 wiki_target.py。"""
import json, os, re, sys

sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
NOW = os.path.expanduser("~/brain/kb/now.md")
LIMIT_5H, LIMIT_WEEK = 60, 65   # 10-05：周线 50→65，Grok 分担后 luna 这路多跑一段，留 35% 给 10-10 前的日常任务


def luna_usage():
    try:
        t = open(NOW, encoding="utf-8").read()
        a = re.search(r"5 小时窗口已用 (\d+)%（(\d\d-\d\d \d\d:\d\d) 重置）", t)
        b = re.search(r"本周已用 (\d+)%（(\d\d-\d\d \d\d:\d\d) 重置）", t)
        if not (a and b):
            return None
        # 10-04：用量数只在调用 luna 时更新；窗口已过重置时间就按 0 算。否则让路后没人调用 luna，
        # 数永远停在旧值，深加工卡死（10-04 16:37 窗口已重置，却按 62% 让路停了 3 小时）
        import datetime as dt
        now = dt.datetime.now()
        def cur(m):
            reset = dt.datetime.strptime(f"{now.year}-{m.group(2)}", "%Y-%m-%d %H:%M")
            return 0 if reset <= now else int(m.group(1))
        return cur(a), cur(b)
    except OSError:
        return None


if __name__ == "__main__":
    u = luna_usage()
    if u is not None and (u[0] >= LIMIT_5H or u[1] >= LIMIT_WEEK):
        print(f"NO_TARGET：luna 用量 5 小时 {u[0]}%、本周 {u[1]}%，给并入新对话让路，这轮不深加工。")
        print(json.dumps({"wakeAgent": False}))
    else:
        import wiki_consolidate as wiki_target
        wiki_target.pick(prefer="light")
