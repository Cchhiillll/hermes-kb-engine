#!/usr/bin/env python3
"""知识库停工报警（10-06 起，no-agent 定时任务，每 30 分钟，不调大模型）。
为什么：10-06 约 11:50~22:45 两路整理和每天的提炼、并入全停了 11 小时（Grok 令牌过期、新周不返回用量，都被当成「读不到就停」），
没有任何提醒；进度卡一天只发两次，20:00 那张还因脚本报错没发出去。
规则：还有活没做（有没提炼的新对话、没并入的知识点、或整理队列不空），但最近 STALL_HOURS 小时没有任何进展（新提炼、新并入、整理交卷都算），
就发一张飞书卡片，写清停了多久、三家模型各自能不能用、写锁在谁手里。同一次停工只报一次；恢复后清零；一直没恢复每 12 小时再报一次。
  --dry  只打印判断结果，不发卡片
"""
import json, os, sqlite3, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, os.path.expanduser("~/.hermes/scripts"))
import kb_config
import wiki_consolidate as w
import wiki_feed as wf
import kb_lock

STALL_HOURS, REPEAT_HOURS = 3, 12
STATE = os.path.join(os.path.dirname(kb_config.load().db), ".stall_alert.json")
now = time.time()
c = w.conn()
k = wf.conn()
marks = [x for x in (k.execute("select max(created) from extract_batches").fetchone()[0],
                     k.execute("select max(merged) from extract_batches").fetchone()[0],
                     c.execute("select max(done) from wiki_consolidate").fetchone()[0]) if x]
last = max(marks) if marks else 0
d, total = wf.progress(k)
unread = max(0, total - d)
unmerged = k.execute("select count(*) from extract_batches where merged is null").fetchone()[0]
queue = 0
for gid, key, pages, home in w.groups():
    r = c.execute("select done from wiki_consolidate where gid=?", (gid,)).fetchone()
    if not (r and r[0]) and not w.skipped(c, gid):
        queue += 1
queue += len(w.big_pages(c, now))
pending = unread + unmerged + queue
try:
    st = json.load(open(STATE))
except (OSError, ValueError):
    st = {}

if pending == 0:
    if os.path.exists(STATE):
        os.remove(STATE)
    if "--dry" in sys.argv:
        print(f"正常 还有活：没提炼的新对话 0 段、没并入的知识点 0 批、整理队列 0 项（当前无积压）。")
    sys.exit(0)

# pending > 0: 活刚进来时开始计时；如有新进展则把停工计时重置为最新进展时间
pending_since = st.get("pending_since")
if not pending_since:
    pending_since = now
    if "--dry" not in sys.argv:
        st["pending_since"] = now
elif last and last > pending_since:
    pending_since = last
    if "--dry" not in sys.argv:
        st["pending_since"] = last
        st.pop("alerted", None)

stall_h = (now - pending_since) / 3600
stalled = stall_h >= STALL_HOURS
msg = f"还有活：没提炼的新对话 {unread} 段、没并入的知识点 {unmerged} 批、整理队列 {queue} 项；已停滞 {stall_h:.1f} 小时。"
if "--dry" in sys.argv:
    print(("停工 " if stalled else "正常 ") + msg); sys.exit(0)
if not stalled:
    json.dump(st, open(STATE, "w"))
    sys.exit(0)
if st.get("alerted") and now - st["alerted"] < REPEAT_HOURS * 3600:
    sys.exit(0)
import kb_models
name, _, _, why = kb_models.pick()
try:
    from feishu_card import send_feishu_card
    send_feishu_card(
        title=f"⛔ 知识库停了 {stall_h:.0f} 小时",
        what_happened=msg + f"\n三家模型：{why}" + (f"（现在能用的是 {name}）" if name else "（三家都不能用）") + f"\n写锁：{kb_lock.describe(kb_lock._read())}",
        business_impact="新对话进不了知识库，Hermes 查不到最近的事；整理也停在原地。",
        user_action="把这张卡转给 Claude Code，或在会话里说一句「知识库停了」。",
        template="red", note="赫尔墨斯 · 知识库停工报警")
except Exception as e:
    print(f"⛔ 知识库停了 {stall_h:.0f} 小时: {msg} (飞书卡片未发出：没有 feishu_card 模块或发送失败：{e})")
st["alerted"] = now
json.dump(st, open(STATE, "w"))
