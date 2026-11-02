"""启动动画状态机。

对应上游 C# ``DragonCodexBoot`` 的主流程：打开动画播放窗口 → 启动目标应用 →
等待其主窗口就绪 → 播放到 ``HoldAt`` 暂停 → 对齐/过渡 → 移交焦点。

之所以把状态机与 Qt 解耦，是因为逻辑里全是时间和外部事件驱动的判断，
用纯 Python 写可以在没有图形环境的情况下用假时钟跑完整测试。

状态迁移::

    IDLE
      └─ prepare() → WAITING_APP      播放窗口已显示，等待目标窗口就绪
            ├─ 就绪且到达 HoldAt → HOLDING     暂停视频等待过渡时机
            │     └─ 到达 TransitionStart → TRANSITION
            │            └─ 到达 TransitionEnd → HANDOFF
            │                  └─ finish() → DONE
            ├─ 超过 MaxWaitSeconds + 超时余量 → FAILED_APP
            └─ 用户跳过（Esc） → HANDOFF
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from .config import Config
from .geometry import Rect
from .log import log

# 上游在 MaxWaitSeconds 之外额外给的宽限期：慢机器上 Electron 首帧可能很迟。
APP_TIMEOUT_GRACE_SECONDS = 20.0

# 过渡阶段的进度采样间隔（秒），用于驱动 shm/fade 档的透明度插值。
TRANSITION_TICK_SECONDS = 1.0 / 60.0


class Phase(str, Enum):
    """状态机阶段标识。"""

    IDLE = "idle"
    WAITING_APP = "waiting-app"
    HOLDING = "holding"
    TRANSITION = "transition"
    HANDOFF = "handoff"
    DONE = "done"
    FAILED_APP = "failed-app"
    FAILED_VIDEO = "failed-video"


# 终止状态：进入后状态机不再产生新动作。
TERMINAL_PHASES = (Phase.DONE, Phase.FAILED_APP, Phase.FAILED_VIDEO)


@dataclass
class TickResult:
    """单次 ``tick()`` 产生的动作清单，供调用方执行副作用。"""

    # 需要把视频跳到指定秒数并暂停。
    hold_at: Optional[float] = None
    # 需要恢复播放。
    resume: bool = False
    # 过渡进度（0~1），仅 TRANSITION 阶段出现。
    progress: Optional[float] = None
    # 需要把客户端窗口对齐到给定客户区矩形。
    align: Optional[Rect] = None
    # 需要移交控制权：关闭播放窗口，随后把焦点交给客户端窗口。
    handoff: bool = False
    # 需要关闭播放窗口。
    close_player: bool = False
    # 需要显示的状态文案键（None 表示不改变）。
    status: Optional[str] = None
    # 进入失败态时携带的原因。
    failure: Optional[str] = None

    def is_empty(self) -> bool:
        return self == TickResult()


@dataclass
class LauncherState:
    """状态机对外暴露的可观测状态。"""

    phase: Phase = Phase.IDLE
    # 视频当前播放位置（秒），由调用方每帧同步进来。
    position: float = 0.0
    # 目标应用主窗口是否已就绪。
    app_ready: bool = False
    # 已就绪窗口的客户区几何。
    client: Optional[Rect] = None
    # 播放窗口在屏幕上的几何。
    player: Optional[Rect] = None
    # 过渡阶段的进度快照（0~1）。
    progress: float = 0.0
    notes: list[str] = field(default_factory=list)


class Launcher:
    """启动动画状态机。"""

    def __init__(
        self,
        config: Config,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._clock = clock
        self.state = LauncherState()
        self._started_at: Optional[float] = None
        self._hold_started_at: Optional[float] = None
        self._alignment_done = False

    # ------------------------------------------------------------------
    # 时间与超时
    # ------------------------------------------------------------------
    def app_timeout(self) -> float:
        """应用就绪等待上限（含宽限期）。"""
        return float(self.config.max_wait_seconds) + APP_TIMEOUT_GRACE_SECONDS

    def elapsed(self) -> float:
        if self._started_at is None:
            return 0.0
        return self._clock() - self._started_at

    # ------------------------------------------------------------------
    # 阶段驱动
    # ------------------------------------------------------------------
    def prepare(self, player: Optional[Rect] = None) -> TickResult:
        """进入 WAITING_APP 阶段，返回需要执行的初始动作。"""

        self._started_at = self._clock()
        self.state = LauncherState(phase=Phase.WAITING_APP, player=player)
        log("launcher:phase WAITING_APP")
        return TickResult(status="waiting")

    def notify_app_ready(self, client: Rect) -> None:
        """外部探测到目标窗口就绪时调用。"""

        if self.state.phase not in (Phase.WAITING_APP, Phase.HOLDING):
            return
        if client.width <= 0 or client.height <= 0:
            return
        if not self.state.app_ready:
            log(
                f"launcher:app-ready client={client.left},{client.top} "
                f"{client.width}x{client.height}"
            )
        self.state.app_ready = True
        self.state.client = client

    def notify_app_lost(self) -> None:
        """目标窗口消失（例如用户中途关闭）时调用。"""

        if self.state.phase in TERMINAL_PHASES:
            return
        self.state.app_ready = False

    def skip(self) -> TickResult:
        """用户按 Esc 或点「跳过」。"""

        # HANDOFF 也算已交接：此时正在关窗口，不能再发第二次关窗动作。
        if self.state.phase in TERMINAL_PHASES or self.state.phase is Phase.HANDOFF:
            return TickResult()
        log("launcher:skip")
        return self._enter_handoff()

    def video_failed(self, reason: str) -> TickResult:
        """视频无法播放：直接转为打开应用，等同上游的状态文案。"""

        if self.state.phase in TERMINAL_PHASES:
            return TickResult()
        log(f"launcher:video-failed reason={reason}")
        self.state.phase = Phase.FAILED_VIDEO
        return TickResult(
            status="video-failed",
            failure=reason,
            close_player=True,
        )

    def tick(self, position: float, ended: bool = False) -> TickResult:
        """推进状态机，返回本次需要执行的动作。

        ``position`` 是当前视频播放位置（秒），``ended`` 表示视频已播完。
        """

        if self.state.phase in TERMINAL_PHASES:
            return TickResult()
        self.state.position = position

        if self.state.phase is Phase.WAITING_APP:
            return self._tick_waiting(position, ended)
        if self.state.phase is Phase.HOLDING:
            return self._tick_holding(position)
        if self.state.phase is Phase.TRANSITION:
            return self._tick_transition(position, ended)
        return TickResult()

    def _tick_waiting(self, position: float, ended: bool) -> TickResult:
        # 超时优先于一切：应用一直起不来时不能再让用户干等。
        if self.elapsed() >= self.app_timeout():
            log(f"launcher:app-timeout elapsed={self.elapsed():.1f}s")
            self.state.phase = Phase.FAILED_APP
            return TickResult(
                status="app-failed",
                failure="target-app-timeout",
            )
        if ended:
            # 视频播完但应用还没就绪：停在最后一帧继续等，不阻塞。
            if self.state.app_ready:
                return self._enter_transition()
            return TickResult()
        if not self.state.app_ready:
            return TickResult()
        # 应用就绪后仍需让视频播到 HoldAt，保证画面与上游一致。
        if position >= self.config.hold_at:
            return self._enter_holding(position)
        return TickResult()

    def _enter_holding(self, position: float) -> TickResult:
        self.state.phase = Phase.HOLDING
        self._hold_started_at = self._clock()
        log(f"launcher:phase HOLDING at={position:.3f}")
        # 已经越过过渡起点：不再回退暂停，直接进入过渡。
        if position >= self.config.transition_start:
            follow = self._enter_transition()
            follow.status = "holding"
            return follow
        return TickResult(hold_at=self.config.hold_at, status="holding")

    def _tick_holding(self, position: float) -> TickResult:
        if self._hold_started_at is None:
            self._hold_started_at = self._clock()
        # 暂停期间 position 不会前进，过渡时机必须用墙钟推算：
        # 剩余时间 = TransitionStart - HoldAt，从进入暂停那一刻开始计时。
        wait = max(0.0, self.config.transition_start - self.config.hold_at)
        if (self._clock() - self._hold_started_at) >= wait:
            return self._enter_transition()
        return TickResult()

    def _enter_transition(self) -> TickResult:
        if self.state.phase is Phase.TRANSITION:
            return TickResult(progress=self.state.progress)
        self.state.phase = Phase.TRANSITION
        self.state.progress = 0.0
        log("launcher:phase TRANSITION")
        result = TickResult(resume=True, progress=0.0)
        # 过渡第一拍就把客户端窗口移到终点矩形并由嵌入层接管绘制；
        # 若此时还不知道客户端几何，就等到 _tick_transition 拿到再对齐。
        align = self._take_alignment()
        if align is not None:
            result.align = align
        return result

    def _take_alignment(self) -> Optional[Rect]:
        """返回一次性对齐动作；未就绪或已对齐时返回 None。"""

        if self._alignment_done or self.state.client is None:
            return None
        self._alignment_done = True
        return self._target_client_rect()

    def _tick_transition(self, position: float, ended: bool) -> TickResult:
        span = max(1e-6, self.config.transition_end - self.config.transition_start)
        progress = (position - self.config.transition_start) / span
        if ended:
            progress = 1.0
        progress = min(1.0, max(0.0, progress))
        self.state.progress = progress

        result = TickResult(progress=progress)
        align = self._take_alignment()
        if align is not None:
            result.align = align
        if progress >= 1.0:
            handoff = self._enter_handoff()
            result.handoff = handoff.handoff
            result.close_player = handoff.close_player
        return result

    def _enter_handoff(self) -> TickResult:
        """进入 HANDOFF：立即关闭播放窗口，把屏幕让给真实应用。

        交接本身（激活真实窗口）不能在这里做。播放窗口是无边框置顶窗口，
        只要它还映射在屏幕上，被它覆盖的真实窗口就会被合成器标记为不可见，
        外部探测也就找不到目标窗口（实测：提前激活后探测只剩「暂时不可用」）。
        因此这里只发出「关窗口」的动作，激活推迟到 :meth:`complete_handoff`，
        由运行层在窗口真正关闭之后再执行。
        """

        if self.state.phase in TERMINAL_PHASES:
            return TickResult()
        log("launcher:phase HANDOFF")
        self.state.phase = Phase.HANDOFF
        return TickResult(handoff=True, close_player=True)

    def complete_handoff(self) -> TickResult:
        """播放窗口已关闭后收尾：终结状态机，并由运行层激活真实窗口。"""

        if self.state.phase is not Phase.HANDOFF:
            return TickResult()
        log("launcher:phase DONE")
        self.state.phase = Phase.DONE
        return TickResult()

    # ------------------------------------------------------------------
    # 几何
    # ------------------------------------------------------------------
    def player_rect(self) -> Rect:
        """播放窗口在屏幕上的几何（配置值，未做屏幕适配）。"""

        if self.state.player is not None:
            return self.state.player
        return Rect(0, 0, self.config.player_width, self.config.player_height)

    def target_client_rect(self) -> Rect:
        """目标客户区在过渡终点应处的矩形。"""

        return self._target_client_rect()

    def _target_client_rect(self) -> Rect:
        """过渡终点：退出过渡时视频画面被放满整个播放窗口，
        因此目标客户区就是播放窗口本身在屏幕上的矩形。"""

        return self.player_rect()

    def origin_client_rect(self) -> Optional[Rect]:
        """过渡起点：客户端窗口当前所在的客户区矩形。"""

        return self.state.client

    def snapshot(self) -> LauncherState:
        return self.state
