#!/usr/bin/env bash
# 每小时刷新 qmd：重扫文件更新关键词索引 + 只给知识库页面（wiki 集合）补向量（量小）。不调大模型；静默，过程写日志。
# 顺带给 wiki 做一次 git 快照（只提交 wiki/，有改动才提交）：页面或 log.md 被误覆盖时最多丢一小时。
L="$HOME/brain/.state/qmd_refresh.log"
# 10-04：更新和向量化用最低 CPU/IO 优先级，不和 Hermes 的检索抢（之前向量化一跑半小时，检索被拖到 5~20 分钟）；上一轮向量化没跑完就跳过，不叠跑
LOW="nice -n 19 ionice -c3"
{ echo "== $(date "+%F %T")"; $LOW "$HOME/.local/bin/qmd" update; flock -n -E 75 /tmp/qmd_embed.lock $LOW "$HOME/.local/bin/qmd" embed -c wiki; [ $? -eq 75 ] && echo "上一轮向量化还在跑，本轮跳过"; "$HOME/.local/bin/qmd" status | grep -E "Vectors|Pending"; } >> "$L" 2>&1
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/scripts/log_guard.py" >> "$L" 2>&1   # 日志保险：快照前比对，丢了就补回并飞书通知
"$HOME/.hermes/scripts/patch_guard.sh" >> "$L" 2>&1   # 源码修补保险：Hermes 升级覆盖后自动补回
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/scripts/lint_feishu_delivery.py" >> "$L" 2>&1 || true # 飞书消息合规门禁扫描
{ git -C "$HOME/brain" add wiki && { git -C "$HOME/brain" diff --cached --quiet -- wiki || git -C "$HOME/brain" -c user.name=chillwang -c user.email=chillwang@thinkpad commit -qm "wiki hourly snapshot $(date "+%F %H:%M")" -- wiki; }; } >> "$L" 2>&1
exit 0
