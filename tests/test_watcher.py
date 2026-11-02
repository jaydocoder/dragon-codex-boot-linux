"""窗口探测与稳定性判定测试（使用可注入时钟，无需 X 服务器）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import watcher as w  # noqa: E402


class FakeClock:
    """可手动推进的单调时钟。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeFinder:
    """可切换返回值的窗口查找器。"""

    def __init__(self, window=None, responsive=True) -> None:
        self.window = window
        self.responsive = responsive
        self.calls = 0

    def find(self):
        self.calls += 1
        return self.window

    def is_responsive(self, _wid):
        return self.responsive


def make_watcher(finder, clock):
    return w.WindowWatcher(
        finder.find, finder.is_responsive, clock=clock
    )


def test_no_window_means_not_ready():
    clock = FakeClock()
    watcher = make_watcher(FakeFinder(None), clock)
    result = watcher.poll(force=True)
    assert result.candidate is None
    assert result.ready is False


def test_same_window_must_stay_stable_for_point_six_seconds():
    clock = FakeClock()
    finder = FakeFinder(0x1234)
    watcher = make_watcher(finder, clock)

    assert watcher.poll(force=True).ready is False  # 首次出现，开始计时

    clock.advance(0.5)
    assert watcher.poll(force=True).ready is False

    clock.advance(0.1)  # 累计 0.6 秒
    assert watcher.poll(force=True).ready is True

    clock.advance(1.0)
    assert watcher.poll(force=True).ready is True


def test_switching_window_resets_stability_timer():
    clock = FakeClock()
    finder = FakeFinder(0x1)
    watcher = make_watcher(finder, clock)

    watcher.poll(force=True)
    clock.advance(0.7)
    assert watcher.poll(force=True).ready is True

    # 窗口换成另一个：稳定性必须重新累计。
    finder.window = 0x2
    result = watcher.poll(force=True)
    assert result.candidate == 0x2
    assert result.ready is False
    assert result.changed is True

    clock.advance(0.4)
    assert watcher.poll(force=True).ready is False
    clock.advance(0.3)
    assert watcher.poll(force=True).ready is True


def test_unresponsive_window_is_dropped():
    clock = FakeClock()
    finder = FakeFinder(0x1)
    watcher = make_watcher(finder, clock)
    watcher.poll(force=True)
    clock.advance(1.0)
    assert watcher.poll(force=True).ready is True

    finder.responsive = False
    result = watcher.poll(force=True)
    assert result.candidate is None
    assert result.ready is False
    assert result.changed is True

    # 恢复响应后要重新累计稳定时间。
    finder.responsive = True
    clock.advance(0.1)
    assert watcher.poll(force=True).ready is False
    clock.advance(0.7)
    assert watcher.poll(force=True).ready is True


def test_probe_is_throttled_within_interval():
    clock = FakeClock()
    finder = FakeFinder(None)
    watcher = make_watcher(finder, clock)

    watcher.poll(force=True)
    assert finder.calls == 1
    clock.advance(0.1)  # 未到 0.25 秒
    watcher.poll()
    assert finder.calls == 1
    clock.advance(0.2)  # 累计 0.3 秒
    watcher.poll()
    assert finder.calls == 2


def test_reset_clears_state():
    clock = FakeClock()
    watcher = make_watcher(FakeFinder(0x9), clock)
    watcher.poll(force=True)
    clock.advance(1.0)
    assert watcher.poll(force=True).ready is True

    watcher.reset()
    assert watcher.target is None
    assert watcher.ready is False
    # 重置后首次探测必须重新计时，不能直接沿用之前的稳定状态。
    assert watcher.poll(force=True).ready is False


@pytest.mark.parametrize("stable_seconds", [0.0, 0.1, 2.0])
def test_stable_seconds_is_configurable(stable_seconds):
    clock = FakeClock()
    watcher = w.WindowWatcher(
        FakeFinder(0x5).find, clock=clock, stable_seconds=stable_seconds
    )
    watcher.poll(force=True)
    clock.advance(stable_seconds)
    assert watcher.poll(force=True).ready is True
