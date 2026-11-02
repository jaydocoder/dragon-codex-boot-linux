"""目标窗口探测与稳定性判定。

对应上游 ``BootWindow.Probe``：每 0.25 秒查一次目标进程的主窗口，
要求窗口「可见、可响应、且同一个 XID 持续稳定 ≥0.6 秒」才算就绪。
这是窗口层面的启发式判断，不是官方提供的「内容已就绪」事件。

本模块把「时间与状态判定」与「X11 查询」解耦，方便在无 X 服务器的
环境下做单元测试。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .log import log

# 上游阈值：探测间隔、稳定性窗口。
PROBE_INTERVAL = 0.25
STABLE_SECONDS = 0.6

# 候选窗口的提供者签名：返回 XID 或 None。
WindowProbe = Callable[[], Optional[int]]
# 窗口是否可响应的判定签名。
ResponsiveProbe = Callable[[int], bool]


@dataclass
class ProbeResult:
    """一次探测的结果。"""

    # 当前找到的候选窗口（可能尚未稳定）。
    candidate: Optional[int]
    # 是否已满足「可见 + 可响应 + 稳定」三个条件。
    ready: bool
    # 本次是否有状态迁移（供日志使用）。
    changed: bool = False


class WindowWatcher:
    """按固定间隔探测目标窗口，并维护稳定性计时。"""

    def __init__(
        self,
        find_window: WindowProbe,
        is_responsive: Optional[ResponsiveProbe] = None,
        clock: Callable[[], float] = time.monotonic,
        interval: float = PROBE_INTERVAL,
        stable_seconds: float = STABLE_SECONDS,
    ) -> None:
        self._find_window = find_window
        self._is_responsive = is_responsive or (lambda _wid: True)
        self._clock = clock
        self._interval = interval
        self._stable_seconds = stable_seconds

        self.target: Optional[int] = None
        self.ready = False
        self._stable_handle: Optional[int] = None
        self._stable_since = 0.0
        self._last_probe = float("-inf")

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空所有状态，用于重新开始探测。"""
        self.target = None
        self.ready = False
        self._stable_handle = None
        self._stable_since = 0.0
        self._last_probe = float("-inf")

    def poll(self, force: bool = False) -> ProbeResult:
        """执行一次探测；未到间隔时间时直接返回上次结果。"""
        now = self._clock()
        if not force and now - self._last_probe < self._interval:
            return ProbeResult(candidate=self.target, ready=self.ready)
        self._last_probe = now

        candidate = self._find_window()
        if candidate is None or not self._is_responsive(candidate):
            changed = self.ready or self.target is not None
            self.ready = False
            self._stable_handle = None
            self.target = None
            if changed:
                log("probe: 目标窗口暂时不可用")
            return ProbeResult(candidate=None, ready=False, changed=changed)

        changed = False
        if candidate != self._stable_handle:
            # 换了一个新窗口：重新开始计时，之前累积的稳定性作废。
            self._stable_handle = candidate
            self._stable_since = now
            self.ready = False
            changed = True
            log(f"probe: 记录候选窗口 0x{candidate:x}")
        else:
            was_ready = self.ready
            self.ready = (now - self._stable_since) >= self._stable_seconds
            changed = self.ready != was_ready
            if changed and self.ready:
                log(f"probe: 窗口 0x{candidate:x} 已稳定")

        self.target = candidate
        return ProbeResult(candidate=candidate, ready=self.ready, changed=changed)
