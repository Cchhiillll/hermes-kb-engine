#!/usr/bin/env bash
# 本地 / CI 统一检查：Python 语法、shell 语法、shellcheck（装了才跑）、pytest。
# 用法：bash tools/check.sh      （PYTHON 指定解释器，默认 python3）
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
PY="${PYTHON:-python3}"
fail=0
mapfile -t PYS < <(git ls-files '*.py' 2>/dev/null || find . -name '*.py' -not -path './.git/*')
mapfile -t SHS < <(git ls-files '*.sh' 2>/dev/null || find . -name '*.sh' -not -path './.git/*')

echo "== py_compile（${#PYS[@]} 个文件）"
for f in "${PYS[@]}"; do
  "$PY" -c 'import ast,sys; ast.parse(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1])' "$f" || fail=1
done
echo "== bash -n（${#SHS[@]} 个文件）"
for f in "${SHS[@]}"; do bash -n "$f" || fail=1; done
if command -v shellcheck >/dev/null 2>&1; then
  echo "== shellcheck"
  shellcheck "${SHS[@]}" || fail=1
else
  echo "== shellcheck 未安装，跳过（pip install shellcheck-py）"
fi
echo "== pytest"
"$PY" -m pytest || fail=1
exit "$fail"
