#!/usr/bin/env bash
# 每天从 Mac 增量拉取 agent 会话到本地 ~/mac_agent_sync/（只增不删）。
# 来源：Claude Code ~/.claude、Codex ~/.codex/{sessions,archived_sessions}、DSH ~/.dsh/sessions、Grok ~/.grok/sessions（只收 updates.jsonl）、ZCode 命令行会话库
# 每个来源单独判成败；有任何失败，退出码非 0。

BACKUP_DIR="${MAC_SYNC_DIR:-$HOME/mac_agent_sync}"
LOG="${LOG_PATH:-$HOME/.hermes/mac_sync.log}"

# 探测 Mac 可达 IP（支持环境变量指定，或在已知局域网漂移 IP 中自动发现）
MAC_USER="${MAC_USER:-wangyipeng}"
CANDIDATE_IPS=("${MAC_IP:-}" "192.168.3.205" "192.168.3.115")
TARGET_IP=""

for ip in "${CANDIDATE_IPS[@]}"; do
  [ -z "$ip" ] && continue
  if ping -c 1 -W 2 "$ip" >/dev/null 2>&1; then
    TARGET_IP="$ip"
    break
  fi
done

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }

log "开始同步"
if [ -z "$TARGET_IP" ]; then
  log "⚠️ Mac 离线/休眠（尝试候选 IP 均不可达），跳过（下次增量会补上）"; exit 0
fi

MAC="${MAC_USER}@${TARGET_IP}"
log "✓ 已定位目标 Mac: $MAC"

fail=0
pull(){ # $1=名称 $2=远端路径 $3=本地路径 其余=rsync 过滤参数
  local name=$1 src=$2 dst=$3; shift 3
  mkdir -p "$dst"
  local before; before=$(find "$dst" -type f | wc -l)
  if rsync -az --update --timeout=30 "$@" "$MAC:$src" "$dst" >> "$LOG" 2>&1; then
    local after; after=$(find "$dst" -type f | wc -l)
    log "✓ $name: 本地文件 $before → $after（+$((after-before))）"
  else
    log "✗ $name: rsync 失败，退出码 $?"; fail=1
  fi
}

pull claude       "~/.claude/"                  "$BACKUP_DIR/claude/" \
     --include='*/' --include='*.jsonl' --include='*.md' --include='*.json' \
     --exclude='cache/***' --exclude='*'
pull codex        "~/.codex/sessions/"          "$BACKUP_DIR/codex/sessions/" \
     --include='*/' --include='*.jsonl' --exclude='*'
pull codex-archv  "~/.codex/archived_sessions/" "$BACKUP_DIR/codex/archived_sessions/" \
     --include='*/' --include='*.jsonl' --exclude='*'
pull dsh          "~/.dsh/sessions/"            "$BACKUP_DIR/dsh/sessions/" \
     --include='*/' --include='*.zstd' --include='*.jsonl' --include='*.json' --exclude='*'
pull grok         "~/.grok/sessions/"           "$BACKUP_DIR/grok/" \
     --include='*/' --include='updates.jsonl' --exclude='*'

# ZCode 会话库正在写，先在 Mac 上做一致性备份，再拉这一个文件。不拉缓存和图形界面配置。
mkdir -p "$BACKUP_DIR/zcode"
if ssh -o BatchMode=yes -o ConnectTimeout=10 "$MAC" 'sqlite3 ~/.zcode/cli/db/db.sqlite ".backup /tmp/zcode-kb.sqlite"' >> "$LOG" 2>&1 \
   && rsync -az --timeout=60 "$MAC:/tmp/zcode-kb.sqlite" "$BACKUP_DIR/zcode/db.sqlite" >> "$LOG" 2>&1; then
  log "✓ zcode: 会话库已备份"
else
  log "✗ zcode: 备份失败"; fail=1
fi

[ $fail -eq 0 ] && log "同步完成（全部成功）" || log "同步结束，有失败项，见上"
exit $fail
