#!/usr/bin/env bash
# 每小时刷新 QMD 混合检索索引与 Wiki / 语雀向量，并给 wiki 做 Git 快照。
# 静默运行并记录日志至 $KB_BRAIN_DIR/.state/qmd_refresh.log
BRAIN="${KB_BRAIN_DIR:-$HOME/brain}"
QMD="${QMD_BIN:-qmd}"
L="${LOG_FILE:-$BRAIN/.state/qmd_refresh.log}"
mkdir -p "$(dirname "$L")"
LOW=(nice -n 19)

{
  echo "== $(date '+%F %T') QMD Refresh =="
  if command -v "$QMD" >/dev/null 2>&1; then
    "${LOW[@]}" "$QMD" update || true
    flock -n -E 75 /tmp/qmd_embed.lock "${LOW[@]}" "$QMD" embed -c wiki || true
    # 语雀集合（qmd collection add ~/brain/sources/yuque --name yuque）存在时一起补向量
    if "$QMD" collection list 2>/dev/null | grep -qw yuque; then
      flock -n -E 75 /tmp/qmd_embed.lock "${LOW[@]}" "$QMD" embed -c yuque || true
    fi
  else
    echo "没有找到 qmd，跳过索引刷新"
  fi
  if [ -d "$BRAIN/.git" ]; then
    git -C "$BRAIN" add wiki
    if ! git -C "$BRAIN" diff --cached --quiet -- wiki; then
      git -C "$BRAIN" commit -qm "wiki hourly snapshot $(date '+%F %H:%M')" -- wiki || true
    fi
  fi
} >> "$L" 2>&1

exit 0
