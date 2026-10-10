#!/usr/bin/env bash
# 每天从另一台机器（通常是 Mac）增量拉取 agent 会话到本地 MAC_SYNC_DIR（只增不删）。
# 来源：Claude Code ~/.claude、Codex ~/.codex/{sessions,archived_sessions}、DSH ~/.dsh/sessions、Grok ~/.grok/sessions（只收 updates.jsonl）、ZCode 命令行会话库
# 每个来源单独判成败；有任何失败，退出码非 0。
# 10-10：主机和用户名只从配置读（不再写死用户名和局域网 IP）：
#   MAC_USER=用户名  MAC_HOSTS="主机名或IP 备选IP ..."（也兼容单个 MAC_IP）；都没配就跳过（退出码 0）。

BACKUP_DIR="${MAC_SYNC_DIR:-$HOME/mac_agent_sync}"
LOG="${LOG_PATH:-${HERMES_HOME:-$HOME/.hermes}/mac_sync.log}"
mkdir -p "$(dirname "$LOG")"

ts(){ date '+%Y-%m-%d %H:%M:%S'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }

read -r -a CANDIDATES <<< "${MAC_HOSTS:-} ${MAC_IP:-}"
if [ -z "${MAC_USER:-}" ] || [ "${#CANDIDATES[@]}" -eq 0 ]; then
  log "未配置 MAC_USER / MAC_HOSTS，跳过同步"; exit 0
fi

TARGET=""
for host in "${CANDIDATES[@]}"; do
  if ping -c 1 -W 2 "$host" >/dev/null 2>&1; then TARGET="$host"; break; fi
done

log "开始同步"
if [ -z "$TARGET" ]; then
  log "⚠️ 目标机器离线/休眠（候选主机均不可达），跳过（下次增量会补上）"; exit 0
fi

MAC="${MAC_USER}@${TARGET}"
log "✓ 已定位目标机器: $MAC"

fail=0
pull(){ # $1=名称 $2=远端路径（~ 在远端展开） $3=本地路径 其余=rsync 过滤参数
  local name=$1 src=$2 dst=$3; shift 3
  mkdir -p "$dst"
  local before after; before=$(find "$dst" -type f | wc -l)
  if rsync -az --update --timeout=30 "$@" "$MAC:$src" "$dst" >> "$LOG" 2>&1; then
    after=$(find "$dst" -type f | wc -l)
    log "✓ $name: 本地文件 $before → $after（+$((after-before))）"
  else
    log "✗ $name: rsync 失败，退出码 $?"; fail=1
  fi
}

# shellcheck disable=SC2088  # ~ 是给远端 shell 展开的
{
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
}

# ZCode 会话库正在写，先在远端做一致性备份，再拉这一个文件。
mkdir -p "$BACKUP_DIR/zcode"
if ssh -o BatchMode=yes -o ConnectTimeout=10 "$MAC" 'sqlite3 ~/.zcode/cli/db/db.sqlite ".backup /tmp/zcode-kb.sqlite"' >> "$LOG" 2>&1 \
   && rsync -az --timeout=60 "$MAC:/tmp/zcode-kb.sqlite" "$BACKUP_DIR/zcode/db.sqlite" >> "$LOG" 2>&1; then
  log "✓ zcode: 会话库已备份"
else
  log "✗ zcode: 备份失败（远端没有 ZCode 可忽略）"; fail=1
fi

if [ "$fail" -eq 0 ]; then log "同步完成（全部成功）"; else log "同步结束，有失败项，见上"; fi
exit "$fail"
