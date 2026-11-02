"""几何算法单元测试。

黄金值来源于独立复算上游 C# ``Geometry`` 表达式的结果，其中
``t=12.65, fade=0`` 的 ``(236, 102, 1413, 783)`` 与上游
``docs/CONFIGURATION.md`` 给出的文档示例完全一致，可作为移植正确性的交叉验证。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.geometry import (  # noqa: E402
    Rect,
    ScreenFrame,
    at,
    centered,
    clamp,
    dest,
    ease,
    lerp,
    validate_frames,
    window_for_client,
)

# 与上游 config/launcher.example.json 完全相同的关键帧。
FRAMES = [
    ScreenFrame(time=12.65, x=0.123, y=0.094, width=0.736, height=0.725),
    ScreenFrame(time=13.0, x=0.074, y=0.061, width=0.845, height=0.825),
    ScreenFrame(time=13.5, x=0.0, y=0.0, width=1.0, height=1.0),
    ScreenFrame(time=13.65, x=0.0, y=0.0, width=1.0, height=1.0),
]


def test_clamp_bounds():
    assert clamp(-3.0) == 0.0
    assert clamp(0.4) == 0.4
    assert clamp(9.0) == 1.0


def test_ease_endpoints_and_midpoint():
    # smoothstep 端点固定，且关于 0.5 对称。
    assert ease(0.0) == 0.0
    assert ease(1.0) == 1.0
    assert ease(0.5) == 0.5
    assert ease(0.25) == pytest.approx(0.15625)
    assert ease(0.75) == pytest.approx(0.84375)
    # 输入越界时钳制，与上游 Clamp 行为一致。
    assert ease(-1.0) == 0.0
    assert ease(2.0) == 1.0


def test_lerp_rect():
    a = Rect(0, 0, 100, 100)
    b = Rect(200, 40, 300, 200)
    assert lerp(a, b, 0.0) == a
    assert lerp(a, b, 1.0) == b
    assert lerp(a, b, 0.5) == Rect(100, 20, 200, 150)


def test_centered_on_1080p_monitor():
    monitor = Rect(0, 0, 1920, 1080)
    assert centered(monitor, 1920, 1080) == Rect(0, 0, 1920, 1080)
    assert centered(monitor, 1280, 720) == Rect(320, 180, 1280, 720)


def test_window_for_client_accounts_for_frame_insets():
    # 窗口外框比客户区大 20px 宽、50px 高，客户区原点偏移 (10, 40)。
    window = Rect(100, 100, 1220, 850)
    client = Rect(110, 140, 1200, 800)
    desired = Rect(0, 0, 1920, 1080)
    outer = window_for_client(window, client, desired)
    assert outer == Rect(-10, -40, 1940, 1130)


def test_at_clamps_before_first_and_after_last():
    assert at(FRAMES, 0.0) is FRAMES[0]
    assert at(FRAMES, 99.0) is FRAMES[-1]


def test_at_interpolates_linearly():
    frame = at(FRAMES, 12.825)  # 12.65 与 13.0 的中点
    assert frame.x == pytest.approx((0.123 + 0.074) / 2)
    assert frame.y == pytest.approx((0.094 + 0.061) / 2)
    assert frame.width == pytest.approx((0.736 + 0.845) / 2)
    assert frame.height == pytest.approx((0.725 + 0.825) / 2)


@pytest.mark.parametrize(
    "time,fade,expected",
    [
        # 过渡开始瞬间：屏幕矩形映射到 1080p 窗口，尚未放大。
        (12.65, 0.0, (236, 102, 1413, 783)),
        (12.65, 0.5, (118, 51, 1666, 932)),
        (12.65, 1.0, (0, 0, 1920, 1080)),
        (13.0, 0.0, (142, 66, 1622, 891)),
        (13.0, 0.5, (71, 33, 1771, 986)),
        (13.0, 1.0, (0, 0, 1920, 1080)),
        # 末段关键帧已是全屏矩形，任何 fade 都填满窗口。
        (13.5, 0.0, (0, 0, 1920, 1080)),
        (13.65, 0.0, (0, 0, 1920, 1080)),
    ],
)
def test_dest_golden_values_at_1080p(time, fade, expected):
    assert dest(FRAMES, time, 1920, 1080, fade).as_tuple() == expected


def test_dest_scales_with_player_size():
    # 1280×720 播放窗口下，fit 系数 2/3，矩形按比例缩小。
    assert dest(FRAMES, 13.0, 1280, 720, 0.0).as_tuple() == (95, 44, 1082, 594)


def test_dest_preserves_aspect_with_letterbox():
    # 非 16:9 窗口（1000×1000 正方形）时按「较短边」fit，视频上下留黑边。
    # fit = min(1000/1920, 1000/1080) = 0.5208，所以 video_w=1000、video_h≈562.5。
    result = dest(FRAMES, 12.65, 1000, 1000, 0.0)
    fit = min(1000 / 1920.0, 1000 / 1080.0)
    video_w, video_h = 1920 * fit, 1080 * fit
    offset_x, offset_y = (1000 - video_w) / 2, (1000 - video_h) / 2
    assert result.left == round(offset_x + 0.123 * video_w)
    assert result.top == round(offset_y + 0.094 * video_h)
    assert result.width == round(0.736 * video_w)
    # 归一化宽高各自乘以不同的基准（1920 与 1080），
    # 因此像素宽高比 = (0.736*1920)/(0.725*1080)，而非 0.736/0.725。
    expected_ratio = (0.736 * 1920) / (0.725 * 1080)
    assert result.width / result.height == pytest.approx(expected_ratio, rel=1e-3)


def test_validate_frames_accepts_upstream_config():
    assert validate_frames(FRAMES) == []


def test_validate_frames_rejects_empty():
    assert validate_frames([]) == ["ScreenFrames 不能为空"]


def test_validate_frames_rejects_out_of_range_and_bad_time():
    bad = [
        ScreenFrame(time=1.0, x=-0.1, y=0.0, width=0.5, height=0.5),
        ScreenFrame(time=1.0, x=0.0, y=0.0, width=0.5, height=0.5),
        ScreenFrame(time=2.0, x=0.8, y=0.0, width=0.5, height=0.5),
    ]
    errors = validate_frames(bad)
    assert any("不能为负" in item for item in errors)
    assert any("严格递增" in item for item in errors)
    assert any("超出画面" in item for item in errors)


def test_validate_frames_tolerates_float_sum_over_one():
    # 0.736 + 0.264 = 1.0000000000000002，不应被判为越界。
    frames = [ScreenFrame(time=1.0, x=0.736, y=0.0, width=0.264, height=1.0)]
    assert validate_frames(frames) == []
