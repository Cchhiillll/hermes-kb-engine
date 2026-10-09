#!/usr/bin/env bash
# 每天 03:00：从 Mac 同步会话 → 增量消化进 brain → 构建切片与索引。
set -eo pipefail

[ -f "$HOME/.chillwang-ai-cli-env" ] && . "$HOME/.chillwang-ai-cli-env"

L="${LOG_PATH:-$HOME/brain/.state/nightly.log}"
mkdir -p "$(dirname "$L")"
echo "===== $(date '+%F %T') nightly =====" >> "$L"

# 1. 从 Mac 增量同步原始会话
if [ -f "$HOME/brain/tools/sync_mac.sh" ]; then
    bash "$HOME/brain/tools/sync_mac.sh" >> "$L" 2>&1 || echo "sync 退出码 $?" >> "$L"
elif [ -f "$(dirname "$0")/sync_mac.sh" ]; then
    bash "$(dirname "$0")/sync_mac.sh" >> "$L" 2>&1 || echo "sync 退出码 $?" >> "$L"
fi

# 2. 会话切片入库 (kb build)
if command -v kb >/dev/null 2>&1; then
    kb build --no-embed >> "$L" 2>&1 || echo "kb 退出码 $?" >> "$L"
elif [ -f "$HOME/brain/kb/kb.py" ]; then
    python3 "$HOME/brain/kb/kb.py" build --no-embed >> "$L" 2>&1 || echo "kb 退出码 $?" >> "$L"
fi

# 3. 导出原始 Markdown 层
if [ -f "$HOME/brain/kb/export_raw.py" ]; then
    python3 "$HOME/brain/kb/export_raw.py" >> "$L" 2>&1 || true
fi

# 4. QMD 混合检索索引刷新
if command -v qmd >/dev/null 2>&1; then
    qmd update >> "$L" 2>&1 || true
    if [ -f "$HOME/brain/kb/qmd_embed_all.sh" ]; then
        bash "$HOME/brain/kb/qmd_embed_all.sh" >> "$L" 2>&1 || true
    fi
fi

# 5. Git 知识库自动提交
if [ -d "$HOME/brain/.git" ]; then
    git -C "$HOME/brain" add wiki >> "$L" 2>&1 || true
    if ! git -C "$HOME/brain" diff --cached --quiet -- wiki; then
        git -C "$HOME/brain" commit -qm "wiki nightly $(date +%F)" >> "$L" 2>&1 || true
    fi
fi

echo "===== $(date '+%F %T') nightly finished =====" >> "$L"
