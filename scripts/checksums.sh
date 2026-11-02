#!/usr/bin/env bash
# checksums.sh —— 为已构建的 deb 生成 SHA256SUMS。
#
# 用法：
#   scripts/checksums.sh <版本号>       # 生成 dist/SHA256SUMS
#   scripts/checksums.sh                # 版本号留空时自动扫描 dist/*.deb
#
# 发布时附上校验和，用户下载后可以自己核对，也便于排查下载损坏。

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTDIR="$ROOT/dist"
TARGET="$OUTDIR/SHA256SUMS"

[[ -d "$OUTDIR" ]] || { echo "错误：$OUTDIR 不存在，请先构建" >&2; exit 1; }

cd "$OUTDIR"

# 只对 deb 计算校验和：SHA256SUMS 自身不能被卷进去。
shopt -s nullglob
debs=( ./*.deb )
shopt -u nullglob

if [[ ${#debs[@]} -eq 0 ]]; then
    echo "错误：dist/ 下没有 .deb 文件" >&2
    exit 1
fi

# 排序保证多次运行输出一致，便于对比。
printf '%s\n' "${debs[@]}" | LC_ALL=C sort | xargs -r sha256sum > "$TARGET"

echo "已生成 $TARGET："
cat "$TARGET"
