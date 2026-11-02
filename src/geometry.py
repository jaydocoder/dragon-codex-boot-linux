"""几何计算模块。

本模块是参考项目 ``src/DragonCodexBoot.cs`` 中 ``Geometry`` 类的等价移植。
所有函数保持与上游一致的数值语义，便于用同一套 ``launcher.json`` 配置与
同一段视频得到相同的动画行为。

坐标系约定：
- ``Rect`` 使用整数像素，``left/top/width/height``。
- ``ScreenFrame`` 使用画面归一化坐标（0~1），相对于视频可视区域。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence

# 上游默认视频的逻辑分辨率，用于把归一化坐标映射到实际播放窗口。
VIDEO_LOGICAL_WIDTH = 1920.0
VIDEO_LOGICAL_HEIGHT = 1080.0


@dataclass(frozen=True)
class Rect:
    """整数像素矩形，对应上游 ``Bounds`` 结构。"""

    left: int = 0
    top: int = 0
    width: int = 0
    height: int = 0

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    def as_tuple(self) -> tuple[int, int, int, int]:
        return (self.left, self.top, self.width, self.height)


@dataclass(frozen=True)
class ScreenFrame:
    """视频中屏幕区域的矩形关键帧，坐标为归一化值。"""

    time: float
    x: float
    y: float
    width: float
    height: float


def clamp(value: float) -> float:
    """把数值钳制到 0~1，对应上游 ``Geometry.Clamp``。"""
    return max(0.0, min(1.0, value))


def ease(value: float) -> float:
    """smoothstep 缓动，对应上游 ``Geometry.Ease``。

    上游实现为 ``x*x*(3-2*x)``，端点值 0 与 1 处导数为 0。
    """
    x = clamp(value)
    return x * x * (3.0 - 2.0 * x)


def lerp(a: Rect, b: Rect, t: float) -> Rect:
    """按比例插值两个矩形，对应上游 ``Geometry.Lerp``。

    上游对每个分量做 ``Math.Round``（银行家舍入）。这里使用 Python 的
    ``round``，两者在同一实现（IEEE 754 round-half-to-even）下行为一致。
    """

    def mix(p: int, q: int) -> int:
        return int(round(p + (q - p) * t))

    return Rect(
        left=mix(a.left, b.left),
        top=mix(a.top, b.top),
        width=mix(a.width, b.width),
        height=mix(a.height, b.height),
    )


def centered(monitor: Rect, width: int, height: int) -> Rect:
    """在显示器矩形内居中放置给定尺寸，对应上游 ``Geometry.Centered``。

    上游使用整数除法，负偏移会被截断向零；这里保持一致。
    """
    return Rect(
        left=monitor.left + int((monitor.width - width) / 2),
        top=monitor.top + int((monitor.height - height) / 2),
        width=width,
        height=height,
    )


def window_for_client(window: Rect, client: Rect, desired: Rect) -> Rect:
    """由客户区目标尺寸反推窗口外框，对应上游 ``Geometry.WindowForClient``。

    上游注释说明：Chromium/Electron 窗口恢复后边框内缩量可能变化，
    因此调用方需要重新测量并再次计算。
    """
    return Rect(
        left=desired.left - (client.left - window.left),
        top=desired.top - (client.top - window.top),
        width=desired.width + window.width - client.width,
        height=desired.height + window.height - client.height,
    )


def at(frames: Sequence[ScreenFrame], t: float) -> ScreenFrame:
    """取时间点 t 的关键帧，对应上游 ``Geometry.At``。

    - 早于首个关键帧时返回首帧；
    - 晚于末个关键帧时返回末帧；
    - 之间按相邻两帧线性插值。
    """
    if not frames:
        raise ValueError("ScreenFrames 不能为空")
    if t <= frames[0].time:
        return frames[0]
    for i in range(1, len(frames)):
        if t > frames[i].time:
            continue
        a, b = frames[i - 1], frames[i]
        span = b.time - a.time
        # 上游未对 span 为 0 做保护；配置校验阶段已禁止时间重复，
        # 这里仍做防御，避免除零崩溃。
        u = clamp((t - a.time) / span) if span > 0 else 0.0
        return ScreenFrame(
            time=t,
            x=a.x + (b.x - a.x) * u,
            y=a.y + (b.y - a.y) * u,
            width=a.width + (b.width - a.width) * u,
            height=a.height + (b.height - a.height) * u,
        )
    return frames[-1]


def dest(
    frames: Sequence[ScreenFrame],
    time: float,
    width: int,
    height: int,
    fade: float,
) -> Rect:
    """计算真实窗口应被绘制到的目标矩形，对应上游 ``Geometry.Dest``。

    两步：
    1. 按 16:9 letterbox fit 把归一化屏幕矩形映射到实际窗口尺寸（不拉伸变形）；
    2. 用 ``ease(fade)`` 从该矩形插值到整个窗口 (0,0,width,height)，
       实现「屏幕区域随动画放大逐渐填满窗口」。
    """
    frame = at(frames, time)
    fit = min(width / VIDEO_LOGICAL_WIDTH, height / VIDEO_LOGICAL_HEIGHT)
    video_w = VIDEO_LOGICAL_WIDTH * fit
    video_h = VIDEO_LOGICAL_HEIGHT * fit
    offset_x = (width - video_w) / 2.0
    offset_y = (height - video_h) / 2.0

    in_video = Rect(
        left=int(round(offset_x + frame.x * video_w)),
        top=int(round(offset_y + frame.y * video_h)),
        width=max(1, int(round(frame.width * video_w))),
        height=max(1, int(round(frame.height * video_h))),
    )
    return lerp(in_video, Rect(0, 0, width, height), ease(fade))


def validate_frames(frames: Sequence[ScreenFrame]) -> List[str]:
    """校验关键帧序列，返回中文错误列表（空列表表示通过）。

    对应上游对 ``ScreenFrames`` 的约束：坐标非负、矩形不越界、
    时间严格递增。
    """
    errors: List[str] = []
    if not frames:
        return ["ScreenFrames 不能为空"]

    last_time = None
    for index, frame in enumerate(frames):
        label = f"ScreenFrames[{index}]"
        for name in ("time", "x", "y", "width", "height"):
            value = getattr(frame, name)
            if not math.isfinite(value):
                errors.append(f"{label}.{name} 必须是有限数值")
        if frame.x < 0 or frame.y < 0:
            errors.append(f"{label} 的 X/Y 不能为负")
        if frame.width <= 0 or frame.height <= 0:
            errors.append(f"{label} 的宽高必须为正数")
        # 浮点归一化坐标允许极小误差，避免 0.736+0.264 这类求和溢出被判失败。
        if frame.x + frame.width > 1.0 + 1e-6 or frame.y + frame.height > 1.0 + 1e-6:
            errors.append(f"{label} 的矩形超出画面范围（0~1）")
        if last_time is not None and frame.time <= last_time:
            errors.append(f"{label}.time 必须严格递增")
        last_time = frame.time

    return errors
