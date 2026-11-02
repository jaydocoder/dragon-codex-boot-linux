#!/usr/bin/env bash
# 安装脚本：创建项目内虚拟环境、安装依赖、初始化配置。
#
# 用法：
#   scripts/install.sh            # 常规安装
#   scripts/install.sh --check    # 只做环境检查，不安装任何东西
#
# 为什么用项目内 .venv：Ubuntu 24.04 的系统 Python 受 PEP 668 保护，
# 直接 pip install 会被拒绝；PyQt6 又必须走 pip 才能拿到与 libmpv 匹配的版本。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
CHECK_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    -h|--help)
      sed -n '2,9p' "${BASH_SOURCE[0]}"
      exit 0
      ;;
    *)
      echo "未知参数：$arg" >&2
      exit 2
      ;;
  esac
done

say() { printf '%s\n' "$*"; }
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------
# 1. 系统依赖
# ----------------------------------------------------------------------
say "== 检查系统依赖 =="

command -v python3 >/dev/null 2>&1 || fail "找不到 python3"

PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3, 10) else 0)')
[[ "$PY_OK" == "1" ]] || fail "需要 Python 3.10 或更新版本"

# libmpv 是播放核心，缺失时程序无法启动。
#
# 注意不要写 ``grep -q``：它会提前退出，让 ldconfig 收到 SIGPIPE，
# 在 ``set -o pipefail`` 下整条管道被判失败，从而误报「未安装」。
if ! ldconfig -p 2>/dev/null | grep 'libmpv\.so' >/dev/null; then
  fail "找不到 libmpv。请安装：sudo apt install libmpv2 （或 libmpv1）"
fi
say "  libmpv：已安装"

# X11 是硬依赖：整个方案建立在 X 的窗口嵌入能力上。
if [[ -z "${DISPLAY:-}" ]]; then
  say "  警告：当前会话没有 DISPLAY。程序只能在图形会话里运行。"
fi

# 合成器决定 fade 档的透明度属性是否生效，仅提示不阻断。
if command -v xprop >/dev/null 2>&1 && [[ -n "${DISPLAY:-}" ]]; then
  if xprop -root _NET_SUPPORTING_WM_CHECK >/dev/null 2>&1; then
    say "  X11：可用（$DISPLAY）"
  fi
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  say "检查完成（未安装任何内容）。"
  exit 0
fi

# ----------------------------------------------------------------------
# 2. 虚拟环境与依赖
# ----------------------------------------------------------------------
say "== 准备虚拟环境 =="

if [[ ! -x "$PY" ]]; then
  say "  创建 $VENV"
  python3 -m venv "$VENV"
else
  say "  复用已有 $VENV"
fi

say "== 安装 Python 依赖 =="
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r "$ROOT/requirements.txt"
say "  安装完成"

# ----------------------------------------------------------------------
# 3. 配置文件
# ----------------------------------------------------------------------
say "== 准备配置文件 =="

CONFIG="$ROOT/launcher.json"
if [[ -f "$CONFIG" ]]; then
  say "  已存在 launcher.json，保持不变"
else
  cp "$ROOT/config/launcher.linux.example.json" "$CONFIG"
  say "  已从模板生成 launcher.json"
fi

# ----------------------------------------------------------------------
# 4. 自检
# ----------------------------------------------------------------------
say "== 自检 =="

if (cd "$ROOT" && QT_QPA_PLATFORM=offscreen PYTHONPATH=. "$PY" -c 'import src.session' 2>/dev/null); then
  say "  模块导入正常"
else
  fail "模块导入失败，请检查依赖安装"
fi

say ""
say "安装完成。接下来可以："
say "  ./run.sh run             播放启动动画并拉起应用"
say "  ./run.sh entries --plan  查看桌面入口接管计划"
say "  scripts/test.sh          运行全部测试"
