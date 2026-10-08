#!/usr/bin/env bash
# qmd 每次 embed 最多跑 30 分钟；没显卡时首次全量算不完，就连着跑，直到没有待算的（最多 12 轮）
for i in $(seq 1 12); do
  left=$("$HOME/.local/bin/qmd" status 2>/dev/null | grep -oE "Pending: +[0-9]+" | grep -oE "[0-9]+")
  [ -z "$left" ] || [ "$left" = "0" ] && break
  echo "== 第 $i 轮，待算文件 $left"; QMD_EMBED_PARALLELISM=3 "$HOME/.local/bin/qmd" embed 2>&1 | tr "\r" "\n" | grep -E "Done|failed" | head -2
done
"$HOME/.local/bin/qmd" status 2>/dev/null | grep -E "Vectors|Pending"
# 没算完就以失败退出（nightly 的退出码才能如实反映索引是否补齐）
left=$("$HOME/.local/bin/qmd" status 2>/dev/null | grep -oE "Pending: +[0-9]+" | grep -oE "[0-9]+")
[ -z "$left" ] || [ "$left" = "0" ] || { echo "qmd 仍有 $left 个文件未算向量"; exit 1; }
