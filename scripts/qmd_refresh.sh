#!/usr/bin/env bash
# 每小时刷新 QMD 混合检索索引与 Wiki 向量
# 静默运行并记录日志至 ~/brain/.state/qmd_refresh.log

L="${LOG_FILE:-$HOME/brain/.state/qmd_refresh.log}"
mkdir -p "$(dirname "$L")"
LOW="nice -n 19"

{
  echo "== $(date '+%F %T') QMD Refresh =="
  if command -v qmd >/dev/null 2>&1; then
    $LOW qmd update || true
    flock -n -E 75 /tmp/qmd_embed.lock $LOW qmd embed -c wiki || true
  fi
  # 自动 Wiki Git 快照
  if [ -d "$HOME/brain/.git" ]; then
    git -C "$HOME/brain" add wiki
    if ! git -C "$HOME/brain" diff --cached --quiet -- wiki; then
      git -C "$HOME/brain" commit -qm "wiki hourly snapshot $(date '+%F %H:%M')" -- wiki || true
    fi
  fi
} >> "$L" 2>&1

exit 0
