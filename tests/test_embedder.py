"""``src.embedder`` 的单元测试。

分为两部分：

1. 纯逻辑：能力降级选择、过渡透明度、矩形插值、像素分块；
2. Qt 管线：BGRA → QImage 的通道顺序与尺寸正确性（用 offscreen 平台运行，
   不需要真实 X 服务器）。
"""

from __future__ import annotations

import pytest

from src.embedder import (
    CapabilityReport,
    EmbedMode,
    EmbedderError,
    align_rect,
    choose_mode,
    frame_alpha,
    split_rows,
)
from src.geometry import Rect


def _report(
    composite: bool,
    shm: bool,
    compositor: bool,
    redirect: bool = True,
) -> CapabilityReport:
    return CapabilityReport(
        composite=composite,
        shm=shm,
        compositor=compositor,
        redirect=redirect,
    )


def test_合成器已接管时不可抢占重定向():
    """GNOME/KDE 合成器已对顶层窗口做 RedirectAutomatic，X 会拒绝第二次重定向。"""
    report = _report(composite=True, shm=True, compositor=True, redirect=False)
    assert report.supports(EmbedMode.COMPOSITE) is False
    # 这种环境下应当退到 shm，而不是 fade。
    assert choose_mode("auto", report) is EmbedMode.SHM


def test_无合成器时_composite_与_shm_都不可用():
    report = _report(composite=True, shm=True, compositor=False, redirect=True)
    assert report.supports(EmbedMode.COMPOSITE) is False
    assert report.supports(EmbedMode.SHM) is False
    assert choose_mode("auto", report) is EmbedMode.FADE


def test_auto_在缺少_composite_时选择_shm():
    report = _report(composite=False, shm=True, compositor=True)
    assert choose_mode("auto", report) is EmbedMode.SHM


def test_显式请求能力不足时降级而不是报错():
    # 启动动画属于附加功能，任何情况下都不应阻止用户进入应用。
    report = _report(composite=False, shm=False, compositor=False)
    assert choose_mode("composite", report) is EmbedMode.FADE
    assert choose_mode("shm", report) is EmbedMode.FADE
    assert choose_mode("fade", report) is EmbedMode.FADE


def test_显式请求能力足够时按请求执行():
    report = _report(composite=True, shm=True, compositor=True)
    assert choose_mode("shm", report) is EmbedMode.SHM
    assert choose_mode("fade", report) is EmbedMode.FADE
    # 大小写与空白都要宽容处理，配置文件是人工编辑的。
    assert choose_mode("  COMPOSITE ", report) is EmbedMode.COMPOSITE


def test_空值等价于_auto():
    report = _report(composite=True, shm=True, compositor=True)
    assert choose_mode("", report) is EmbedMode.COMPOSITE


def test_未知方案抛出中文错误():
    report = _report(composite=True, shm=True, compositor=True)
    with pytest.raises(EmbedderError) as excinfo:
        choose_mode("dwm", report)
    assert "未知的 EmbedMode" in str(excinfo.value)
    assert "auto/composite/shm/fade" in str(excinfo.value)


# ----------------------------------------------------------------------
# 过渡透明度
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "progress,expected",
    [
        (0.0, 1.0),
        (0.3, 1.0),
        (0.6, 1.0),
        (0.8, 0.5),
        (1.0, 0.0),
    ],
)
def test_进度映射到播放窗口透明度(progress, expected):
    assert frame_alpha(progress) == pytest.approx(expected)


def test_进度越界被钳制():
    assert frame_alpha(-1.0) == 1.0
    assert frame_alpha(2.0) == 0.0


# ----------------------------------------------------------------------
# 矩形插值
# ----------------------------------------------------------------------
def test_插值起点与终点精确命中():
    origin = Rect(236, 102, 1413, 783)
    target = Rect(0, 0, 1920, 1080)
    assert align_rect(origin, target, 0.0) == origin
    assert align_rect(origin, target, 1.0) == target


def test_插值中点是几何平均():
    origin = Rect(0, 0, 100, 100)
    target = Rect(200, 40, 300, 200)
    mid = align_rect(origin, target, 0.5)
    assert mid == Rect(100, 20, 200, 150)


def test_插值进度越界被钳制():
    origin = Rect(10, 10, 20, 20)
    target = Rect(0, 0, 100, 100)
    assert align_rect(origin, target, -0.5) == origin
    assert align_rect(origin, target, 1.5) == target


# ----------------------------------------------------------------------
# 像素分块
# ----------------------------------------------------------------------
def test_按行切分保持每行独立():
    width, height = 3, 2
    # 每个像素 4 字节，用序号填充便于验证切片边界。
    data = bytes(range(width * height * 4))
    rows = split_rows(data, width, height)
    assert len(rows) == height
    assert rows[0][0] == data[: width * 4]
    assert rows[1][0] == data[width * 4 :]
    assert rows[0][1] == width * 4


def test_数据不足时报中文错误():
    with pytest.raises(EmbedderError) as excinfo:
        split_rows(b"\x00" * 8, 2, 2)
    assert "像素数据长度不足" in str(excinfo.value)


# ----------------------------------------------------------------------
# Qt 管线（offscreen，无 X 依赖）
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def qt_app():
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def test_bgra_转_qimage_通道顺序正确(qt_app):
    from src.embedder import bgra_to_qimage

    # 单个像素：B=0x11 G=0x22 R=0x33 A=0xFF，即 XGetImage 的 BGRA 布局。
    data = bytes([0x11, 0x22, 0x33, 0xFF])
    image = bgra_to_qimage(data, 1, 1)
    color = image.pixelColor(0, 0)
    assert (color.red(), color.green(), color.blue()) == (0x33, 0x22, 0x11)


def test_bgra_转_qimage_保持尺寸(qt_app):
    from src.embedder import bgra_to_qimage


    width, height = 4, 3
    rows = []
    for y in range(height):
        for x in range(width):
            rows.extend([x * 10, y * 10, 0x80, 0xFF])
    image = bgra_to_qimage(bytes(rows), width, height)
    assert (image.width(), image.height()) == (width, height)
    # 抽查右下角像素：B=x*10, G=y*10, R=0x80。
    color = image.pixelColor(width - 1, height - 1)
    assert (color.red(), color.green(), color.blue()) == (
        0x80,
        (height - 1) * 10,
        (width - 1) * 10,
    )
