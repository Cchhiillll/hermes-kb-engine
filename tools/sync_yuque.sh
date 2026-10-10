#!/usr/bin/env bash
# 语雀 → Markdown 增量同步（10-10）：调用导出工具写到 yuque_export_dir，再用 kb/clean_yuque.py 清洗到 yuque_dir。
# 导出工具二选一（KB_YUQUE_TOOL，或配置文件 yuque_tool）：
#   yuque-exporter  https://github.com/Bkm016/yuque-exporter （.export_records.json 增量）
#                   需要 YUQUE_TOKEN、YUQUE_USER；YUQUE_REPOS="知识库slug 知识库slug"（空 = 该用户全部知识库）
#                   注意：该工具只支持命令行传 token，运行期间同机其他用户可在进程列表里看到，请在单用户机器上运行。
#   elog            https://github.com/LetTTGACO/elog （elog.cache.json 增量）
#                   KB_YUQUE_ELOG_DIR=放 elog 配置的目录；配置里的输出目录设成 yuque_export_dir；token 写在 elog 的 env 文件里
# 没配置导出工具时跳过（退出码 0）；导出失败时照样清洗已有内容，最后以非 0 退出。
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${KB_ENV_FILE:-$HOME/.config/hermes-kb/env}"
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  . "$ENV_FILE"
fi
PY="${PYTHON:-python3}"
cfg(){ "$PY" "$REPO/scripts/kb_config.py" get "$1"; }
TOOL="$(cfg yuque_tool)"
EXPORT="$(cfg yuque_export_dir)"
LOG="${LOG_PATH:-$(cfg state_dir)/yuque_sync.log}"
mkdir -p "$(dirname "$LOG")"
log(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }

if [ -z "$TOOL" ]; then
  log "未配置 KB_YUQUE_TOOL（yuque-exporter / elog），跳过语雀同步"; exit 0
fi
mkdir -p "$EXPORT"
rc=0
case "$TOOL" in
  yuque-exporter)
    BIN="${YUQUE_EXPORTER_BIN:-yuque-exporter}"
    if ! command -v "$BIN" >/dev/null 2>&1; then
      log "✗ 找不到 $BIN（设置 YUQUE_EXPORTER_BIN）"; rc=1
    elif [ -z "${YUQUE_TOKEN:-}" ] || [ -z "${YUQUE_USER:-}" ]; then
      log "✗ 缺少 YUQUE_TOKEN 或 YUQUE_USER（写进 $ENV_FILE）"; rc=1
    else
      read -r -a REPOS <<< "${YUQUE_REPOS:-}"
      [ "${#REPOS[@]}" -eq 0 ] && REPOS=("")
      for repo in "${REPOS[@]}"; do
        args=(-token "$YUQUE_TOKEN" -user "$YUQUE_USER" -output "$EXPORT" -format markdown)
        [ -n "$repo" ] && args+=(-repo "$repo")
        log "导出 ${repo:-（全部知识库）}"
        if ! "$BIN" "${args[@]}" >> "$LOG" 2>&1; then log "✗ 导出 ${repo:-全部} 失败"; rc=1; fi
      done
    fi
    ;;
  elog)
    BIN="${ELOG_BIN:-elog}"
    DIR="${KB_YUQUE_ELOG_DIR:-}"
    if ! command -v "$BIN" >/dev/null 2>&1; then
      log "✗ 找不到 $BIN（npm i -g @elog/cli，或设置 ELOG_BIN）"; rc=1
    elif [ -z "$DIR" ] || [ ! -d "$DIR" ]; then
      log "✗ KB_YUQUE_ELOG_DIR 没设置或不存在（先在该目录 elog init 生成配置）"; rc=1
    else
      log "elog sync（$DIR）"
      if ! (cd "$DIR" && "$BIN" sync -c "${KB_YUQUE_ELOG_CONFIG:-elog.config.js}" -e "${KB_YUQUE_ELOG_ENV:-.elog.env}") >> "$LOG" 2>&1; then
        log "✗ elog sync 失败"; rc=1
      fi
    fi
    ;;
  *)
    log "✗ 不认识的 KB_YUQUE_TOOL：$TOOL（只支持 yuque-exporter / elog）"; rc=1 ;;
esac

if ! "$PY" "$REPO/kb/clean_yuque.py" >> "$LOG" 2>&1; then log "✗ 清洗失败"; rc=1; fi
if [ "$rc" -eq 0 ]; then log "✓ 语雀同步完成"; else log "✗ 语雀同步有失败项"; fi
exit "$rc"
