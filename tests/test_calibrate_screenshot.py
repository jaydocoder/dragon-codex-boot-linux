"""抓帧重试逻辑的回归测试。

背景：mpv 在 ``seek`` 之后到第一帧可截取之间存在异步空窗，
``screenshot-raw`` 会抛 ``SystemError(..., -12, ...)``
（``MPV_ERROR_PROPERTY_UNAVAILABLE``）。实测约 1/3 概率命中，
固定 ``sleep`` 无法消除，必须按错误码重试。

这里用假 player 复现该错误码序列，不依赖真实 libmpv 与视频文件。
"""

from __future__ import annotations

import unittest

from src.calibrate import (
    MPV_ERROR_PROPERTY_UNAVAILABLE,
    _screenshot_with_retry,
)
from src.video import VideoError


def _unavailable() -> SystemError:
    """构造与 python-mpv 一致的异常形态。"""

    return SystemError("Error running mpv command", MPV_ERROR_PROPERTY_UNAVAILABLE)


class FakePlayer:
    """按预设序列返回结果或抛异常的假播放器。"""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def command(self, *args: object) -> object:
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("调用次数超出预期")
        item = self.outcomes.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class ScreenshotRetryTest(unittest.TestCase):
    def test_returns_first_success_without_retry(self) -> None:
        """首次就成功时不应有额外调用。"""

        shot = {"format": "bgr0", "data": b"x", "stride": 4, "h": 1}
        player = FakePlayer([shot])

        self.assertIs(_screenshot_with_retry(player, 1.0), shot)
        self.assertEqual(player.calls, 1)

    def test_retries_until_frame_available(self) -> None:
        """连续几次 -12 之后成功，应最终返回截图。"""

        shot = {"format": "bgr0", "data": b"x", "stride": 4, "h": 1}
        player = FakePlayer([_unavailable(), _unavailable(), shot])

        self.assertIs(_screenshot_with_retry(player, 2.0), shot)
        self.assertEqual(player.calls, 3)

    def test_other_error_codes_are_not_swallowed(self) -> None:
        """文件损坏等其它错误必须原样抛出，不能被重试掩盖。"""

        # -9 = MPV_ERROR_UNINITIALIZED，与本竞态无关。
        player = FakePlayer([SystemError("boom", -9)])

        with self.assertRaises(SystemError) as ctx:
            _screenshot_with_retry(player, 3.0)
        self.assertEqual(ctx.exception.args[1], -9)
        self.assertEqual(player.calls, 1)

    def test_raises_video_error_after_budget(self) -> None:
        """持续 -12 时应在预算耗尽后报中文错误，而不是无限重试。"""

        player = FakePlayer([_unavailable() for _ in range(1000)])

        with self.assertRaises(VideoError) as ctx:
            _screenshot_with_retry(player, 4.0)
        self.assertIn("抓帧超时", str(ctx.exception))

    def test_error_code_is_the_documented_value(self) -> None:
        """常量必须与 mpv 的 client.h 一致，改错会静默失效。"""

        self.assertEqual(MPV_ERROR_PROPERTY_UNAVAILABLE, -12)


if __name__ == "__main__":
    unittest.main()
