#!/usr/bin/env bash
# 通用远程 Agent 会话拉取脚本
# 通过环境变量配置远端连接和路径，实现增量同步 (Append-Only)
#
# 配置示例:
#   export REMOTE_HOST="user@remote.example.com"
#   export REMOTE_AGENT_DIR="~"
#   export BACKUP_DIR="$HOME/brain/raw/conversations"

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-$HOME/brain/raw/conversations}"
LOG="${LOG_FILE:-$HOME/.hermes/remote_sync.log}"
REMOTE_HOST="${REMOTE_HOST:-}"

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" | tee -a "$LOG"; }

if [ -z "$REMOTE_HOST" ]; then
  echo "提示: 未配置 REMOTE_HOST 环境变量，跳过远程拉取。如需同步远程机器会话，请设置 REMOTE_HOST=user@ip"
  exit 0
fi

log "开始从远程机器 ($REMOTE_HOST) 同步会话数据..."

fail=0
pull(){
  local name=$1 src=$2 dst=$3; shift 3
  mkdir -p "$dst"
  local before; before=$(find "$dst" -type f 2>/dev/null | wc -l)
  if rsync -az --update --timeout=30 "$@" "$REMOTE_HOST:$src" "$dst" >> "$LOG" 2>&1; then
    local after; after=$(find "$dst" -type f 2>/dev/null | wc -l)
    log "✓ $name: 文件数量 $before → $after (+$((after-before)))"
  else
    log "✗ $name: rsync 拉取失败"
    fail=1
  fi
}

# 1. Claude Code 会话
pull claude "~/.claude/projects/" "$BACKUP_DIR/claude/" \
     --include='*/' --include='*.jsonl' --include='*.json' --exclude='*' || true

# 2. Codex 会话
pull codex "~/.codex/sessions/" "$BACKUP_DIR/codex/" \
     --include='*/' --include='*.jsonl' --exclude='*' || true

# 3. Hermes 会话 (如果远程也是 Hermes)
pull hermes "~/.hermes/state.db" "$BACKUP_DIR/remote_hermes.db" || true

log "同步任务结束，状态码: $fail"
exit $fail
