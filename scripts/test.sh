#!/usr/bin/env bash
# 测试脚本：在 offscreen 平台下跑全部单元测试。
#
# 用 offscreen 而不是真实 X：测试不依赖窗口系统，这样在 SSH、CI、
# 无图形会话里都能跑。真正需要 X 的部分在端到端联调里验证。
#
# 用法：
#   scripts/test.sh              # 跑全部
#   scripts/test.sh -k widget    # 透传给 pytest 的过滤参数
#   scripts/test.sh -v           # 详细输出

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$ROOT/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "找不到 $PY，请先运行 scripts/install.sh" >&2
  exit 2
fi

export QT_QPA_PLATFORM=offscreen
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
export LC_NUMERIC=C

cd "$ROOT"
exec "$PY" -m pytest tests/ "$@"
