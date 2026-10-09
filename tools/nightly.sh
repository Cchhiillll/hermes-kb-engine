#!/usr/bin/env bash
# Hermes 知识库夜间全自动处理流水线
# 流程: 远端会话拉取 -> 切片构建 (kb build) -> QMD 增量索引 -> 提炼与自愈
set -eo pipefail

L="${LOG_PATH:-$HOME/brain/.state/nightly.log}"
mkdir -p "$(dirname "$L")"
echo "===== $(date '+%F %T') Hermes KB Nightly Pipeline =====" >> "$L"

# 1. 远端会话拉取 (如果配置了 REMOTE_HOST)
if [ -f "$HOME/brain/tools/sync_remote.sh" ]; then
    bash "$HOME/brain/tools/sync_remote.sh" >> "$L" 2>&1 || true
fi

# 2. 会话切片入库与索引生成
if command -v kb >/dev/null 2>&1; then
    kb build >> "$L" 2>&1 || true
elif [ -f "$HOME/brain/kb/kb.py" ]; then
    python3 "$HOME/brain/kb/kb.py" build >> "$L" 2>&1 || true
fi

# 3. QMD 混合检索索引刷新
if command -v qmd >/dev/null 2>&1; then
    qmd update >> "$L" 2>&1 || true
fi

# 4. Git 版本快照与提交 (若知识库目录为 Git 仓库)
if [ -d "$HOME/brain/.git" ]; then
    git -C "$HOME/brain" add wiki >> "$L" 2>&1 || true
    if ! git -C "$HOME/brain" diff --cached --quiet; then
        git -C "$HOME/brain" commit -m "chore(wiki): nightly auto snapshot $(date +%F)" >> "$L" 2>&1 || true
    fi
fi

echo "===== $(date '+%F %T') Nightly Pipeline Finished =====" >> "$L"
