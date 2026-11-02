#!/bin/bash
# build-deb.sh —— 构建 dragon-codex-boot 的 .deb 安装包
#
# 产物：dist/dragon-codex-boot_<版本>_all.deb
#
# 安装后的文件布局：
#   /usr/bin/dragon-codex-boot                      启动命令
#   /usr/lib/dragon-codex-boot/src/                 程序代码
#   /usr/share/dragon-codex-boot/media/startup.mp4  启动动画
#   /usr/share/dragon-codex-boot/launcher.example.json  配置模板
#   /usr/share/applications/dragon-codex-boot.desktop   菜单图标
#   /usr/share/doc/dragon-codex-boot/               文档与许可
#
# 不使用 dh / debhelper：本项目是纯 Python + 数据文件，没有编译步骤，
# 手工组装目录树更透明，也免去引入完整构建依赖。
#
# 用法：
#   ./packaging/build-deb.sh             构建
#   ./packaging/build-deb.sh --install   构建并用 apt 安装

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

say() { printf '%s\n' "$*"; }
die() { printf '错误：%s\n' "$*" >&2; exit 1; }

PKG_NAME="dragon-codex-boot"
# 版本号来源：默认读 VERSION 文件；CI 发布时用 DCB_VERSION 覆盖，
# 让包里版本与 git tag 严格一致（否则 tag v0.2.0 会打出 0.1.0 的包）。
VERSION="${DCB_VERSION:-$(tr -d '[:space:]' < "$ROOT/VERSION" | sed 's/-linux$//')}"
# git tag 通常带 v 前缀，deb 版本号不允许，这里剥掉。
VERSION="${VERSION#v}"
[[ -n "$VERSION" ]] || die "版本号为空"
# Deb 版本号只允许 [A-Za-z0-9.+~-]，带其它字符会让 dpkg 拒绝，提前挡住。
if [[ ! "$VERSION" =~ ^[0-9] ]]; then
    die "版本号必须以数字开头：$VERSION"
fi
ARCH="all"
INSTALL_ROOT="$ROOT/packaging/usr-bin-dragon-codex-boot"
APPDIR="$ROOT/packaging/build/${PKG_NAME}_${VERSION}_${ARCH}"
OUTDIR="$ROOT/dist"


# ----------------------------------------------------------------------
# 前置检查
# ----------------------------------------------------------------------
for tool in dpkg-deb fakeroot; do
    command -v "$tool" >/dev/null 2>&1 || die "缺少构建工具：$tool（apt install $tool）"
done

[[ -f "$ROOT/VERSION" ]]      || die "缺少 VERSION 文件"
[[ -f "$ROOT/packaging/usr-bin-dragon-codex-boot" ]] || die "缺少启动入口脚本"
[[ -f "$ROOT/media/startup.mp4" ]] || die "缺少 media/startup.mp4"

say "== 构建 $PKG_NAME $VERSION ($ARCH) =="

# ----------------------------------------------------------------------
# 组装目录树
# ----------------------------------------------------------------------
rm -rf "$APPDIR"
mkdir -p "$APPDIR/DEBIAN"

install -Dm644 "$ROOT/VERSION" "$APPDIR/usr/share/doc/$PKG_NAME/VERSION"

# 程序代码：只复制运行时需要的模块，排除测试与缓存。
install -d "$APPDIR/usr/lib/$PKG_NAME/src"
for f in "$ROOT"/src/*.py; do
    install -m644 "$f" "$APPDIR/usr/lib/$PKG_NAME/src/"
done

# 启动命令
install -Dm755 "$INSTALL_ROOT" "$APPDIR/usr/bin/$PKG_NAME"

# 媒体与配置模板
install -d "$APPDIR/usr/share/$PKG_NAME/media"
install -m644 "$ROOT/media/startup.mp4"       "$APPDIR/usr/share/$PKG_NAME/media/"
install -m644 "$ROOT/media/MEDIA_NOTICE.md"   "$APPDIR/usr/share/$PKG_NAME/media/"
install -m644 "$ROOT/config/launcher.linux.example.json" \
              "$APPDIR/usr/share/$PKG_NAME/launcher.example.json"

# 桌面入口
install -Dm644 "$ROOT/packaging/dragon-codex-boot.desktop" \
    "$APPDIR/usr/share/applications/dragon-codex-boot.desktop"

# 文档与许可：Debian 政策要求每个包都有 copyright，
# 且必须完整保留上游 MIT 版权声明与署名。
install -d "$APPDIR/usr/share/doc/$PKG_NAME"
install -m644 "$ROOT/LICENSE"                "$APPDIR/usr/share/doc/$PKG_NAME/copyright"
install -m644 "$ROOT/NOTICE.md"              "$APPDIR/usr/share/doc/$PKG_NAME/NOTICE.md"
install -m644 "$ROOT/THIRD_PARTY_NOTICES.md" "$APPDIR/usr/share/doc/$PKG_NAME/THIRD_PARTY_NOTICES.md"
install -m644 "$ROOT/README.md"              "$APPDIR/usr/share/doc/$PKG_NAME/README.md"

# 计算安装体积（KB），写入 control 的 Installed-Size
INSTALLED_SIZE="$(du -sk --exclude=DEBIAN "$APPDIR" | cut -f1)"

# ----------------------------------------------------------------------
# control 元数据
# ----------------------------------------------------------------------
# Depends 说明：
#   python3-*     用系统 Python 与 apt 提供的绑定，避免把 270MB 的
#                 PyQt6 私有副本塞进包里，也避免与系统 libmpv 的 ABI 冲突
#   libmpv2       视频播放底层
#   x11-utils     提供 xprop / xwininfo，窗口定位与排障要用
sed -e "s/@VERSION@/$VERSION/g" \
    -e "s/@INSTALLED_SIZE@/$INSTALLED_SIZE/g" \
    -e "s/@ARCH@/$ARCH/g" \
    "$ROOT/packaging/debian/control.in" > "$APPDIR/DEBIAN/control"

install -m755 "$ROOT/packaging/debian/postinst" "$APPDIR/DEBIAN/postinst"
install -m755 "$ROOT/packaging/debian/prerm"    "$APPDIR/DEBIAN/prerm"
install -m755 "$ROOT/packaging/debian/postrm"   "$APPDIR/DEBIAN/postrm"

# ----------------------------------------------------------------------
# 打包
# ----------------------------------------------------------------------
mkdir -p "$OUTDIR"
DEB="$OUTDIR/${PKG_NAME}_${VERSION}_${ARCH}.deb"

# --root-owner-group 让包内文件统一归属 root:root。
# 构建机是普通用户，不加这个选项会打出一个属主是当前用户的包。
fakeroot dpkg-deb --root-owner-group -Zxz --build "$APPDIR" "$DEB" >/dev/null

say "已生成：$DEB"
say "大小：$(du -h "$DEB" | cut -f1)"

# ----------------------------------------------------------------------
# 结构自检
# ----------------------------------------------------------------------
say ""
say "== 包内容 =="
dpkg-deb --contents "$DEB" | awk '{print $1, $6, $7, $8}' | sed 's/^/  /'

say ""
say "== 控制信息 =="
dpkg-deb --field "$DEB" Package Version Architecture Depends Installed-Size \
    | sed 's/^/  /'

if [[ "${1:-}" == "--install" ]]; then
    say ""
    say "== 安装 =="
    sudo apt-get install -y "$DEB"
fi

say ""
say "完成。安装后可执行："
say "  dragon-codex-boot run"
say "  或在应用菜单里双击「ChatGPT（启动动画）」"
