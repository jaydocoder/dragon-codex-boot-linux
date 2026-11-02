"""libmpv 视频播放封装。

上游使用 WPF ``MediaElement`` 播放启动动画；Linux 侧系统 PyQt6 缺少
QtMultimedia，因此改用 ``libmpv``，把画面直接渲染进 Qt 的无边框窗口。

关键实现细节（均为实测结论）：

1. **必须把 ``LC_NUMERIC`` 保持为 ``C``**。mpv 会拒绝非 C 的数值区域设置，
   实测 Qt 创建 ``QApplication`` 时会把 ``LC_NUMERIC`` 重置为 ``zh_CN.UTF-8``。
   因此在每次进入 Qt 事件循环前统一调用 :func:`enforce_numeric_locale`。
2. **``wid`` 必须传字符串**，``python-mpv`` 会把它拼成 ``--wid=N``。
3. ``pause`` / ``seek`` / ``time_pos`` 都是同步可读写的属性，seek 后立刻
   读到的就是目标时间，不需要额外等待。
"""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .log import log

# mpv 的 --vo 选项：X11 下 gpu 会走 OpenGL/EGL，能正确嵌入外部窗口。
DEFAULT_VO = "gpu"


class VideoError(Exception):
    """视频播放失败时抛出，消息为中文。"""


def enforce_numeric_locale() -> None:
    """把 ``LC_NUMERIC`` 强制设为 ``C``。

    mpv 启动时会检查数值区域设置，非 C 值直接拒绝工作。Qt 初始化会把
    它改回系统值，因此这个函数需要在使用 mpv 前后反复调用。
    """
    try:
        ctypes.CDLL("libc.so.6").setlocale(1, b"C")  # LC_NUMERIC = 1
    except Exception:
        # 非 glibc 环境（如 musl）退化为 Python 侧设置，失败也不影响主流程。
        try:
            import locale

            locale.setlocale(locale.LC_NUMERIC, "C")
        except Exception:
            pass


@dataclass
class VideoInfo:
    """视频基础信息。"""

    path: Path
    duration: float
    width: int
    height: int


class VideoPlayer:
    """把视频渲染进指定 X11 窗口的 mpv 播放器。"""

    def __init__(
        self,
        window_id: int,
        video_path: Path,
        volume: float = 1.0,
        vo: str = DEFAULT_VO,
    ) -> None:
        self.window_id = window_id
        self.video_path = Path(video_path)
        if not self.video_path.is_file():
            raise VideoError(f"视频文件不存在：{self.video_path}")
        self.volume = max(0.0, min(100.0, volume * 100.0))
        self.vo = vo
        self._player = None
        self._ended = False
        # 最近一次暂停时记下的位置：mpv 暂停后 time-pos 会退回关键帧，
        # 记下暂停瞬间的位置才能在恢复前算对「已经播到哪」。
        self._paused_at = 0.0
        # 上一次报告给状态机的进度：保证单调递增，避免进度回退。
        self._last_progress = 0.0

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def open(self, timeout: float = 12.0) -> VideoInfo:
        """创建 mpv 实例并开始播放，返回视频信息。

        上游对 ``MediaOpened`` 有 12 秒超时保护（``media-open-timeout``），
        这里用同样的默认值。
        """
        enforce_numeric_locale()
        try:
            import mpv
        except Exception as exc:  # 缺 python-mpv 或 libmpv
            raise VideoError(f"无法加载 libmpv 绑定：{exc}") from exc

        try:
            self._player = mpv.MPV(
                wid=str(self.window_id),
                vo=self.vo,
                # 动画播放窗口只需要视频，禁用全部交互控件。
                osc="no",
                input_default_bindings="no",
                input_vo_keyboard="no",
                keep_open="yes",
                hwdec="auto-safe",
                volume=self.volume,
                # 视频结束时不自动退出 mpv，由状态机决定何时收尾。
                idle="yes",
                # 播完后保留最后一帧。默认的 idle 画面是一片纯色，会盖住
                # 播放窗口底下的真实窗口，导致合成器把被覆盖的窗口标记成
                # 不可见（实测会让后续的窗口探测全部失败）。保留末帧时，
                # 过渡结束时的画面本身就是满屏视频帧，与交接后的观感一致。
                keepaspect_window="yes",
            )
        except Exception as exc:
            raise VideoError(f"创建 mpv 实例失败：{exc}") from exc

        enforce_numeric_locale()
        self._player.pause = False
        self._player.play(str(self.video_path))

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            enforce_numeric_locale()
            if self._player.time_pos is not None:
                break
            time.sleep(0.05)
        else:
            raise VideoError(f"视频在 {timeout:.0f} 秒内未能开始播放：{self.video_path}")

        info = VideoInfo(
            path=self.video_path,
            duration=float(self._player.duration or 0.0),
            width=int(self._player.width or 0),
            height=int(self._player.height or 0),
        )
        log(
            f"media-opened duration={info.duration:.2f}s size={info.width}x{info.height}"
        )
        return info

    def close(self) -> None:
        """停止播放并销毁 mpv 实例。"""
        player = self._player
        self._player = None
        if player is None:
            return
        enforce_numeric_locale()
        try:
            player.command("stop")
        except Exception:
            pass
        try:
            player.terminate()
        except Exception:
            # mpv 在窗口已被销毁时可能抛错，此时已经没有可恢复动作。
            pass

    # ------------------------------------------------------------------
    # 播放控制
    # ------------------------------------------------------------------
    def progress_position(self) -> float:
        """供状态机推进用的播放位置。

        过渡阶段的节奏是「seek 到 HoldAt → 暂停 → 等墙钟到 TransitionStart →
        恢复播放」。``time-pos`` 在 mpv 恢复播放之后才继续前进，暂停期间停在
        关键帧上，因此不能只读它；内部属性 ``playback-time`` 虽然不退回关键帧，
        但它在恢复后的一段解码时间内同样停在原地（实测停在 11.33s 两秒多）。

        状态机只需要「单调递增的进度」：因此这里取两者较大值，再与暂停时
        记下的位置取大，保证进度永不回退。
        """

        if self._player is None:
            return 0.0
        enforce_numeric_locale()
        candidates = [self._paused_at, self._last_progress, self.position()]
        value = self._playback_time()
        if value is not None:
            candidates.append(value)
        current = max(candidates)
        self._last_progress = current
        return current

    @property
    def is_open(self) -> bool:
        return self._player is not None

    def position(self) -> float:
        """当前播放时间（秒）；未就绪时返回 0。"""
        if self._player is None:
            return 0.0
        enforce_numeric_locale()
        return float(self._player.time_pos or 0.0)

    def _playback_time(self) -> Optional[float]:
        """读取 mpv 的内部属性 ``playback-time``（真正的播放进度）。

        ``time-pos`` 在暂停时会退回上一个关键帧；``playback-time`` 不会。
        内部属性需要 ``_get_property``，拿不到就返回 ``None`` 由调用方兜底。
        """

        if self._player is None:
            return None
        reader = getattr(self._player, "_get_property", None)
        if reader is None:
            return None
        try:
            value = reader("playback-time")
        except Exception:
            return None
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @property
    def duration(self) -> float:
        if self._player is None:
            return 0.0
        return float(self._player.duration or 0.0)

    @property
    def paused(self) -> bool:
        if self._player is None:
            return False
        return bool(self._player.pause)

    def pause(self) -> None:
        if self._player is None:
            return
        enforce_numeric_locale()
        # 暂停前先把播放进度记下来：暂停后 time-pos 会退回关键帧。
        value = self._playback_time()
        if value is None:
            value = self.position()
        self._paused_at = max(self._paused_at, value)
        self._player.pause = True

    def resume(self) -> None:
        if self._player is not None:
            enforce_numeric_locale()
            self._player.pause = False

    def seek(self, seconds: float) -> None:
        """精确跳转到指定时间（对应上游 ``media.Position = ...``）。"""
        if self._player is None:
            return
        enforce_numeric_locale()
        self._player.seek(max(0.0, seconds), reference="absolute", precision="exact")

    def ended(self) -> bool:
        """视频是否已播放到结尾。

        ``eof-reached`` 是 libmpv 的观察属性，并非所有版本都能直接读取，
        因此这里用「时间位置逼近总时长」作为主判据。
        """
        if self._player is None:
            return self._ended
        enforce_numeric_locale()
        try:
            reached = self._player.get_property("eof-reached")
        except Exception:
            reached = None
        if reached:
            self._ended = True
            return True
        duration = self.duration
        if duration > 0 and self.progress_position() >= duration - 0.08:
            self._ended = True
        return self._ended

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(100.0, volume * 100.0))
        if self._player is not None:
            enforce_numeric_locale()
            self._player.volume = self.volume


def probe_duration(path: Path, timeout: float = 8.0) -> Optional[float]:
    """不创建窗口，仅探测视频时长（供标定工具使用）。"""
    enforce_numeric_locale()
    try:
        import mpv
    except Exception:
        return None
    try:
        player = mpv.MPV(vo="null", ao="null", idle="yes")
    except Exception:
        return None
    try:
        player.play(str(path))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            enforce_numeric_locale()
            duration = player.duration
            if duration:
                return float(duration)
            time.sleep(0.05)
        return None
    finally:
        enforce_numeric_locale()
        try:
            player.terminate()
        except Exception:
            pass


def run_mpv_preflight() -> tuple[bool, str]:
    """自检：确认 libmpv 可用且能读到时长长的信息。"""
    enforce_numeric_locale()
    try:
        import mpv  # noqa: F401
    except Exception as exc:
        return False, f"python-mpv 不可用：{exc}"
    if not os.path.exists("libmpv.so.2") and not any(
        (Path(p) / "libmpv.so.2").exists()
        for p in ("/lib/x86_64-linux-gnu", "/usr/lib/x86_64-linux-gnu")
    ):
        return False, "未找到 libmpv.so.2，请安装 libmpv2（或 libmpv1）"
    return True, "libmpv 可用"
