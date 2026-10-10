#!/usr/bin/env bash
# 每天 03:00：同步会话 / 语雀 → 切片入库 → 导出原始层 → QMD 索引 → Git 快照。
# 10-10：组件按仓库位置定位（不再假设脚本被拷到 ~/brain/kb、~/brain/tools）；任何一步失败或缺组件，最后以非 0 退出，
#        不再「找不到就静默跳过、照样返回成功」。
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KB_HOME="${KB_HOME:-$REPO}"
ENV_FILE="${KB_ENV_FILE:-$HOME/.config/hermes-kb/env}"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi
BRAIN="${KB_BRAIN_DIR:-$HOME/brain}"
PY="${PYTHON:-python3}"
QMD="${QMD_BIN:-qmd}"
L="${LOG_PATH:-$BRAIN/.state/nightly.log}"
mkdir -p "$(dirname "$L")"
fail=0

log(){ echo "$*" >> "$L"; }
step(){ # $1=名称 其余=命令
  local name=$1; shift
  log "--- $name"
  if "$@" >> "$L" 2>&1; then log "✓ $name"; else local rc=$?; log "✗ $name 退出码 $rc"; fail=1; fi
}
need(){ [ -e "$1" ] && return 0; log "✗ 缺少组件：$1"; fail=1; return 1; }

log "===== $(date '+%F %T') nightly（KB_HOME=$KB_HOME）====="

# 1. 从另一台机器增量同步原始会话（没配置 MAC_USER/MAC_HOSTS 时脚本自己跳过）
need "$KB_HOME/tools/sync_mac.sh" && step "sync_mac" bash "$KB_HOME/tools/sync_mac.sh"

# 1b. 语雀增量同步 + 清洗（没配置 KB_YUQUE_TOOL 时脚本自己跳过）
need "$KB_HOME/tools/sync_yuque.sh" && step "sync_yuque" bash "$KB_HOME/tools/sync_yuque.sh"

# 2. 会话切片入库（含语雀文档，来源 yuque-doc）
need "$KB_HOME/kb/kb.py" && step "kb build" "$PY" "$KB_HOME/kb/kb.py" build

# 3. 导出原始 Markdown 层
need "$KB_HOME/kb/export_raw.py" && step "export_raw" "$PY" "$KB_HOME/kb/export_raw.py"

# 4. QMD 混合检索索引刷新（KB_SKIP_QMD=1 可显式跳过）
if [ "${KB_SKIP_QMD:-0}" = "1" ]; then
  log "· 已按 KB_SKIP_QMD=1 跳过 QMD"
elif command -v "$QMD" >/dev/null 2>&1; then
  step "qmd update" "$QMD" update
  need "$KB_HOME/kb/qmd_embed_all.sh" && step "qmd embed" bash "$KB_HOME/kb/qmd_embed_all.sh"
else
  log "✗ 没有找到 qmd（设置 QMD_BIN，或 KB_SKIP_QMD=1 显式跳过）"; fail=1
fi

# 5. Git 知识库自动提交
if [ -d "$BRAIN/.git" ]; then
  git -C "$BRAIN" add wiki >> "$L" 2>&1 || fail=1
  if ! git -C "$BRAIN" diff --cached --quiet -- wiki; then
    step "git commit" git -C "$BRAIN" commit -qm "wiki nightly $(date +%F)"
  fi
fi

if [ "$fail" -eq 0 ]; then
  log "===== $(date '+%F %T') nightly finished（全部成功）====="
else
  log "===== $(date '+%F %T') nightly finished（有失败项，见上）====="
  echo "nightly 有失败项，详见 $L" >&2
fi
exit "$fail"
