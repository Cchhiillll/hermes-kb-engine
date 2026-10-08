#!/usr/bin/env bash
# 每天 03:00：从 Mac 同步会话 → 增量消化进 brain → 导出给 Hermes。Hermes 04:00 自动换会话后生效。
[ -f "$HOME/.chillwang-ai-cli-env" ] && . "$HOME/.chillwang-ai-cli-env"   # 代理等环境（cron 不加载登录 shell）
L="$HOME/brain/.state/nightly.log"; mkdir -p "$(dirname "$L")"
echo "===== $(date '+%F %T') nightly =====" >> "$L"
"$HOME/brain/tools/sync_mac.sh"; echo "sync 退出码 $?" >> "$L"
"$HOME/.local/bin/kb" build --no-embed >> "$L" 2>&1; echo "kb 退出码 $?" >> "$L"   # 收新对话进 kb.sqlite（给送料脚本和导出用）
"$HOME/brain/.venv/bin/python" "$HOME/brain/kb/export_raw.py" >> "$L" 2>&1   # 导出成维基原始资料层 Markdown
"$HOME/.local/bin/qmd" update >> "$L" 2>&1 && "$HOME/brain/kb/qmd_embed_all.sh" >> "$L" 2>&1; echo "qmd 退出码 $?" >> "$L"   # qmd 索引 + 向量
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/brain/tools/digest.py" >> "$L" 2>&1; echo "digest 退出码 $?" >> "$L"
# 档案层：只处理新回合 → 重出报告和 top5 → 再导出一次给 Hermes
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/brain/tools/archive.py" >> "$L" 2>&1; echo "archive 退出码 $?" >> "$L"
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/brain/tools/archive_more.py" >> "$L" 2>&1; echo "archive_more 退出码 $?" >> "$L"
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/brain/tools/archive_more_report.py" >> "$L" 2>&1
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/brain/tools/archive_report.py" >> "$L" 2>&1
python3 "$HOME/brain/tools/export.py" >> "$L" 2>&1
# 10-04 知识库单写入锁：只在提交 wiki 这一步拿（几秒），等正在提交的整理/并入完成，最多等 20 分钟，超时照常提交并记下
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/scripts/kb_lock.py" wait nightly 1200 --pid $$ --work 每晚提交知识库 >> "$L" 2>&1 || echo "等锁超时，照常提交" >> "$L"
git -C "$HOME/brain" add archive wiki && git -C "$HOME/brain" -c user.name=chillwang -c user.email=chillwang@thinkpad commit -qm "archive nightly $(date +%F)" >> "$L" 2>&1
"$HOME/.hermes/hermes-agent/venv/bin/python" "$HOME/.hermes/scripts/kb_lock.py" release nightly >> "$L" 2>&1
