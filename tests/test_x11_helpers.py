"""X11 层的纯逻辑单元测试。

X11 交互需要真实显示服务器，不适合放进自动化测试。这里只覆盖
不依赖 X 连接的纯函数与闭包逻辑，保证阈值与判定语义不被改坏。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.x11 import (  # noqa: E402
    MIN_CLIENT_HEIGHT,
    MIN_CLIENT_WIDTH,
    X11WindowManager,
)

_near = X11WindowManager._near


def test_size_thresholds_match_upstream():
    # 上游 FindApp 用 450x280 过滤辅助窗口，改动会直接影响窗口挑选结果。
    assert MIN_CLIENT_WIDTH == 450
    assert MIN_CLIENT_HEIGHT == 280


@pytest.mark.parametrize(
    "actual,expected,ok",
    [
        (100, 100, True),
        (102, 100, True),
        (98, 100, True),
        (103, 100, False),
        (97, 100, False),
    ],
)
def test_near_uses_two_pixel_tolerance(actual, expected, ok):
    assert _near(actual, expected) is ok


def test_near_accepts_custom_tolerance():
    assert _near(110, 100, tolerance=10) is True
    assert _near(111, 100, tolerance=10) is False
