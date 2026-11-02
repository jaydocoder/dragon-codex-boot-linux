#!/usr/bin/env bash
# verify-deb-in-container.sh —— 在干净的 Ubuntu 24.04 容器里实测安装本 deb。
#
# 这是 CI 里"容器内实测安装"步骤的本地等价物，用于在推送 tag 之前
# 提前发现依赖不可解析、维护脚本报错之类的问题。
#
# 用法：
#   scripts/verify-deb-in-container.sh [deb 路径]
#
# 不指定路径时取 dist/ 下最新的 deb。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEB="${1:-}"

if [[ -z "$DEB" ]]; then
    DEB="$(ls -t "$ROOT"/dist/*.deb 2>/dev/null | head -1 || true)"
fi

[[ -n "$DEB" && -f "$DEB" ]] || { echo "错误：找不到 deb，请先构建" >&2; exit 1; }
DEB="$(cd "$(dirname "$DEB")" && pwd)/$(basename "$DEB")"

command -v docker >/dev/null 2>&1 || { echo "错误：需要 docker" >&2; exit 1; }

echo "待验证：$DEB"
echo "宿主机校验和：$(sha256sum "$DEB" | cut -d' ' -f1)"
echo

docker run --rm \
    -v "$(dirname "$DEB"):/dist:ro" \
    -e "DEB_NAME=$(basename "$DEB")" \
    ubuntu:24.04 \
    bash -c '
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive

        echo "== 1) 准备软件源（启用 universe，python3-mpv 在那里）=="
        apt-get update -qq
        apt-get install -y -qq --no-install-recommends \
            software-properties-common ca-certificates >/dev/null
        add-apt-repository -y universe >/dev/null 2>&1 || true
        apt-get update -qq

        echo "== 2) 安装 deb =="
        apt-get install -y -qq "/dist/$DEB_NAME"

        echo "== 3) 验证文件布局 =="
        for f in \
            /usr/bin/dragon-codex-boot \
            /usr/lib/dragon-codex-boot/src/__main__.py \
            /usr/share/dragon-codex-boot/media/startup.mp4 \
            /usr/share/dragon-codex-boot/launcher.example.json \
            /usr/share/applications/dragon-codex-boot.desktop \
            /usr/share/doc/dragon-codex-boot/copyright
        do
            test -e "$f" || { echo "缺少 $f"; exit 1; }
            echo "  ok $f"
        done

        echo "== 4) 依赖是否真的装上了 =="
        python3 -c "import Xlib, mpv; print(\"  python3-xlib / python3-mpv 可用\")"
        python3 -c "import PyQt6.QtWidgets; print(\"  PyQt6 可用\")"

        echo "== 5) 命令能否启动 =="
        dragon-codex-boot --version

        echo "== 6) desktop 文件是否合法 =="
        apt-get install -y -qq --no-install-recommends desktop-file-utils >/dev/null
        desktop-file-validate /usr/share/applications/dragon-codex-boot.desktop \
            && echo "  desktop 文件合法"

        echo "== 7) 卸载必须干净 =="
        apt-get remove -y -qq dragon-codex-boot
        test ! -e /usr/bin/dragon-codex-boot || { echo "卸载后命令仍在"; exit 1; }
        echo "  ok 已卸载"

        echo
        echo "容器内验证全部通过"
    '
