#!/usr/bin/env bash
# Dragon Codex Boot Linux —— 启动入口
#
# 桌面入口接管后，Exec 指向本脚本。它负责把调用转发给项目内的
# Python 环境，所有参数原样透传（包括 --launch-command）。
#
# 手动运行：
#   ./run.sh                     使用 launcher.json 的默认配置
#   ./run.sh --launch-command "…"  模拟桌面入口透传
#   ./run.sh entries --plan      查看入口接管计划

set -euo pipefail

# 脚本所在目录即项目根，避免受调用时工作目录影响。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 优先使用项目内虚拟环境：系统 Python 受 PEP 668 保护，无法安装 PyQt6。
VENV_PY="$ROOT/.venv/bin/python"
if [[ -x "$VENV_PY" ]]; then
  PY="$VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
  PY="python3"
else
  echo "找不到 Python 解释器，请先运行 scripts/install.sh" >&2
  exit 2
fi

# 用 libmpv 时数值区域必须是 C：某些 locale 的小数逗号会让 mpv 解析失败。
export LC_NUMERIC=C

# 本程序依赖 X11 的窗口嵌入能力，禁止退化到 Wayland 平台插件。
if [[ -z "${QT_QPA_PLATFORM:-}" ]]; then
  export QT_QPA_PLATFORM=xcb
fi

# 强制 1:1 设备像素比。
#
# 本机的 Xft.dpi 常被桌面调成 115 之类的值，Qt6 会据此把 devicePixelRatio
# 抬到 1.2，屏幕逻辑尺寸随之变成 1603×902，最终窗口被放大到 2300×1294。
# 而 screenFrames 是按视频原始 1920×1080 像素标定的，一旦缩放整条几何链路
# 全部错位。这里钉死缩放，保证 Qt 坐标与 X11 像素一一对应。
export QT_SCALE_FACTOR=1
export QT_ENABLE_HIGHDPI_SCALING=0
export QT_AUTO_SCREEN_SCALE_FACTOR=0

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

exec "$PY" -m src "$@"
