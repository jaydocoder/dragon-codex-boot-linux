"""运行层：把状态机、播放窗口、X11 探测与嵌入层串成一次完整的启动动画。

各模块的分工：

- ``launcher.Launcher``：纯逻辑状态机，决定「现在该做什么」，不碰任何图形；
- ``player.PlayerWindow``：无边框置顶窗口，mpv 把视频直接渲染到它的 X11 窗口里；
- ``x11.X11WindowManager``：找到目标客户端窗口、测量几何、对齐、交接焦点；
- ``embedder.Embedder``：过渡阶段把真实客户端画面合成到播放窗口上。

本模块只负责「按状态机给的动作去调用上面四个模块」，以及在 Qt 事件循环里
按 ``EmbedFps`` 驱动一帧帧推进。所有耗时操作都限制在单帧预算内，避免动画卡顿。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from PyQt6.QtCore import QCoreApplication, QTimer

from .config import Config
from .embedder import Embedder, EmbedMode, EmbedderError
from .geometry import Rect
from .launcher import Launcher, Phase, TERMINAL_PHASES, TickResult
from .log import log
from .player import PlayerWindow, TEXT_APP_FAILED, TEXT_VIDEO_FAILED, TEXT_WAITING
from .sessionkind import (
    detect_session,
    embed_mode_reason,
    recommended_embed_mode,
    recommended_vo,
    vo_reason,
)
from .video import VideoError, VideoPlayer
from .watcher import WindowWatcher
from .x11 import X11Error, X11WindowManager

# 状态文案键 → 实际文案。与上游的状态文字保持一致。
STATUS_TEXTS = {
    "waiting": TEXT_WAITING,
    "holding": TEXT_WAITING,
    "video-failed": TEXT_VIDEO_FAILED,
    "app-failed": TEXT_APP_FAILED,
}


@dataclass
class SessionResult:
    """一次启动动画的结果，供命令行决定退出码。"""

    ok: bool
    reason: str = ""
    # 目标窗口是否已被成功交接。
    handed_off: bool = False


class BootSession:
    """串联一次完整的启动动画。

    生命周期：``start()`` → 反复 ``_on_tick()`` → ``finish()``。
    Qt 定时器负责驱动，会话自身不阻塞事件循环。
    """

    def __init__(
        self,
        config: Config,
        *,
        window_id: Optional[int] = None,
        x11_factory=X11WindowManager,
        embedder_factory=Embedder,
        player_factory=VideoPlayer,
        window_factory=PlayerWindow,
        launcher_factory=Launcher,
        watcher_factory=WindowWatcher,
        timer_factory=QTimer,
        spawn=subprocess.Popen,
        finished=None,
        session_factory=detect_session,
    ) -> None:
        """构造会话。

        所有外部依赖都以工厂形式注入：默认值生产可用，测试时可换成假实现，
        从而在没有 X 服务器、没有 mpv、没有真实应用的情况下验证串联逻辑。
        """
        self.config = config
        self._x11_factory = x11_factory
        self._embedder_factory = embedder_factory
        self._player_factory = player_factory
        self._window_factory = window_factory
        self._launcher_factory = launcher_factory
        self._watcher_factory = watcher_factory
        self._timer_factory = timer_factory
        self._spawn = spawn
        # 会话类型探测也走注入：测试里可以固定成 wayland/x11，避免依赖真实环境。
        self._session_factory = session_factory
        # 进入终止阶段后的回调。Qt 事件循环只会在最后一个窗口关闭时自动退出，
        # 而这里还留着非窗口的 QObject（定时器、信号连接），实测事件循环会一直
        # 空转，进程永不结束。入口必须靠这个回调显式 quit()。
        self._finished = finished
        self._window = None
        self._player: Optional[VideoPlayer] = None
        self._x11: Optional[X11WindowManager] = None
        self._embedder: Optional[Embedder] = None
        self._watcher: Optional[WindowWatcher] = None
        self._timer: Optional[QTimer] = None
        self._launcher: Optional[Launcher] = None
        self._result = SessionResult(ok=False)
        self._premeasured_window = window_id
        # 会话类型一次性探测：登录后不会变，且探测有 /proc 扫描开销。
        # 结果同时用于「推荐嵌入模式」和诊断输出。
        self._session = None
        self._embed_mode = (config.embed_mode or "auto")
        # mpv 视频输出驱动：按会话类型决定，见 sessionkind.recommended_vo。
        self._vo = ""
        self._transition_started = False
        self._saw_video_end = False
        # 终止清理只做一次：``_on_terminal`` 内部会把 ``_player``/``_x11``
        # 置空，重复进入只会对着半拆状态操作。
        self._terminal_done = False

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def start(self) -> SessionResult:
        """搭建窗口与子系统并开始推进。返回 ``ok=False`` 时说明已当场失败。"""

        # 先探测会话类型。它只影响「嵌入档位的推荐」，属于诊断信息，
        # 因此探测本身出任何问题都必须吞掉：绝不能因为诊断代码而让动画起不来。
        try:
            self._session = self._session_factory()
            self._embed_mode = recommended_embed_mode(
                self._session, self.config.embed_mode
            )
            log(
                f"session:kind {self._session.kind.value} "
                f"({self._session.label}) display={self._session.display or '-'} "
                f"xwayland={self._session.xwayland}"
            )
            log(
                f"session:embed-mode requested={self.config.embed_mode or 'auto'} "
                f"chosen={self._embed_mode} "
                f"reason={embed_mode_reason(self._session, self.config.embed_mode)}"
            )
            self._vo = recommended_vo(self._session)
            log(f"session:vo chosen={self._vo} reason={vo_reason(self._session)}")
        except Exception as exc:  # noqa: BLE001 - 诊断失败绝不影响主流程
            log(f"session:detect-failed err={exc}")
            # 回退到用户原始配置，等价于「没装这套探测」时的行为。
            self._embed_mode = self.config.embed_mode or "auto"

        try:
            self._x11 = self._x11_factory()
        except X11Error as exc:
            log(f"session:x11-unavailable err={exc}")
            return SessionResult(ok=False, reason=f"无法连接 X 服务器：{exc}")

        screen = self._x11.screen_geometry()
        rect = self._player_rect(screen)
        log(
            f"session:player-rect {rect.left},{rect.top} {rect.width}x{rect.height} "
            f"screen={screen.width}x{screen.height}"
        )

        self._window = self._window_factory(rect)
        self._window.skipRequested.connect(self._on_skip)
        self._window.show_centered()

        # 播放窗口必须真正映射到屏幕后，mpv 才能取到有效的 X11 窗口号。
        self._window.winId()
        window_id = int(self._window.winId())

        try:
            self._embedder = self._embedder_factory()
        except EmbedderError as exc:
            # 嵌入层不可用不应阻断启动：退化成纯播放 + 直接交接。
            log(f"session:embedder-unavailable err={exc}")
            self._embedder = None
        if self._embedder is not None:
            # 先按静态能力选一次档；找到目标窗口后会带上窗口号复选一次，
            # 因为「合成器已在接管该窗口」只有真实窗口才能探测出来。
            self._embedder.select(self._embed_mode)

        self._launcher = self._launcher_factory(self.config)
        self._launcher.prepare(rect)
        self._apply_tick(TickResult(status="waiting"))

        if not self._open_video(window_id):
            return self._finish_video_failure()

        self._start_watching()

        # 按配置的帧率推进；下限 5fps，避免异常配置把界面卡死。
        interval = int(1000 / max(5, min(120, self.config.embed_fps)))
        self._timer = self._timer_factory(self._window)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(interval)
        log(f"session:tick-interval {interval}ms")
        return SessionResult(ok=True)

    def _player_rect(self, screen: Rect) -> Rect:
        """播放窗口几何：取配置尺寸，并居中到当前屏幕。

        配置尺寸大于屏幕时按屏幕收缩，避免窗口伸出屏幕外导致看不到动画。
        """

        width = min(self.config.player_width, screen.width)
        height = min(self.config.player_height, screen.height)
        left = screen.left + (screen.width - width) // 2
        top = screen.top + (screen.height - height) // 2
        return Rect(left, top, width, height)

    def _open_video(self, window_id: int) -> bool:
        # 交给 resolve_video() 做路径回退：源码形态在配置目录旁，
        # deb 安装形态下视频在 /usr/share，而配置在 ~/.config。
        path = self.config.resolve_video()
        try:
            # vo 按会话类型选择：Wayland 下 vo=gpu 会静默不出画（见 sessionkind）。
            # 探测失败时 _vo 为空串，交给播放器用它自己的默认值。
            extra = {"vo": self._vo} if self._vo else {}
            self._player = self._player_factory(
                window_id, path, volume=self.config.volume, **extra
            )
            self._player.open()
            return True
        except VideoError as exc:
            log(f"session:video-open-failed err={exc}")
            self._player = None
            return False

    def _start_watching(self) -> None:
        assert self._x11 is not None

        def find():
            return self._x11.find_app_window(
                process_names=self.config.process_names,
                path_contains=self.config.process_path_contains,
                wm_class_hint=self.config.target_wm_class,
            )

        self._watcher = self._watcher_factory(
            find_window=find,
            is_responsive=lambda wid: not self._x11.is_minimized(wid),
        )
        self._launch_app()

    def _launch_app(self) -> None:
        """启动目标应用。

        优先使用桌面入口透传的完整命令（``launch_command_parts``）：它带着
        入口原本的参数，直接原样 ``exec`` 才能得到和点图标一致的行为。
        没有透传命令时退回 ``AppCommand`` 指定的单个可执行文件。
        """

        parts = list(self.config.launch_command_parts or [])
        command = parts[0] if parts else self.config.app_command
        if not command:
            log("session:no-app-command")
            return
        argv = parts or [command]
        try:
            self._spawn(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log(f"session:app-spawned cmd={command}")
        except OSError as exc:
            log(f"session:app-spawn-failed err={exc}")

    # ------------------------------------------------------------------
    # 每帧推进
    # ------------------------------------------------------------------
    def _on_tick(self) -> None:
        if self._launcher is None or self._player is None:
            return

        # 必须用 progress_position()：暂停时 time-pos 会退回关键帧，进度会来回跳。
        reader = getattr(self._player, "progress_position", None)
        position = reader() if callable(reader) else self._player.position()
        ended = self._player.ended()
        if ended and not self._saw_video_end:
            self._saw_video_end = True
            log("session:video-ended")

        self._probe_app()

        result = self._launcher.tick(position, ended=ended)
        self._apply_tick(result)
        self._maybe_finish()

    def _probe_app(self) -> None:
        assert self._watcher is not None and self._x11 is not None and self._launcher is not None
        probe = self._watcher.poll()
        if probe.ready and probe.candidate is not None:
            try:
                geometry = self._x11.geometry_of(self._x11.window(probe.candidate))
            except X11Error:
                return
            self._launcher.notify_app_ready(geometry.client)
            if self._embedder is not None and not self._embedder._state.window_id:
                # 用真实窗口号复选一次档位：XComposite 的 RedirectManual 是否
                # 被合成器拒绝，只有在该窗口上实测才能知道。
                self._embedder.select(self._embed_mode, probe.candidate)
                self._embedder.attach(probe.candidate)

    def _apply_tick(self, result: TickResult) -> None:
        if self._window is None:
            return
        if result.status is not None:
            text = STATUS_TEXTS.get(result.status)
            if text:
                self._window.show_status(text)

        if result.hold_at is not None and self._player is not None:
            log(f"session:hold-at {result.hold_at:.2f}s")
            self._player.seek(result.hold_at)
            self._player.pause()

        if result.resume and self._player is not None:
            # 暂停期间动画停在 HoldAt，过渡开始时必须真正恢复播放。
            # 漏掉这一步，播放位置会一直停在 HoldAt，过渡永远推进不下去。
            log("session:resume")
            self._player.resume()

        if result.align is not None:
            self._align(result.align)

        if result.progress is not None:
            self._drive_transition(result.progress)

        if result.close_player:
            self._close_player()
        if result.handoff:
            # 先关窗口再交接：播放窗口覆盖着的真实窗口会被合成器标记为
            # 不可见，只有窗口真的消失之后激活才找得到目标。
            self._handoff()

    def _align(self, desired: Rect) -> None:
        """把真实客户端窗口对齐到目标客户区矩形。

        mpv 渲染进播放窗口的 X11 表面，本身不经过 Qt 绘制，因此这里只需
        调整客户端窗口位置，并让嵌入层接管后续合成。
        """

        if self._x11 is None or self._embedder is None:
            return
        window_id = self._embedder._state.window_id
        if not window_id:
            log("session:align-skipped reason=no-window")
            return
        try:
            geometry = self._x11.geometry_of(self._x11.window(window_id))
        except X11Error as exc:
            log(f"session:align-failed err={exc}")
            return

        if not self._embedder.begin():
            log("session:embed-begin-failed")
        self._embedder.start_transition(geometry.client, desired, geometry.frame)
        ok = self._x11.align_client(window_id, desired)
        log(f"session:aligned ok={ok}")
        self._transition_started = True

    def _drive_transition(self, progress: float) -> None:
        if self._embedder is None or not self._transition_started:
            return
        self._embedder.apply_progress(progress)
        if self._window is not None and self._embedder.mode is EmbedMode.COMPOSITE:
            # composite 档：真实画面需要由播放窗口合成绘制。
            frame = self._embedder.compose_frame(self._window.fixed_geometry())
            self._window.set_overlay(frame)

    def _handoff(self) -> None:
        """关闭播放窗口之后把焦点交给真实应用。"""

        if self._embedder is not None:
            self._embedder.end()
        window_id = self._embedder._state.window_id if self._embedder else 0
        if not window_id and self._x11 is not None:
            # 嵌入层从未 attach（例如应用迟迟没被识别）时，仍然尝试现找一次，
            # 否则交接到最后什么都不会发生，用户只能看到播放窗口直接消失。
            window_id = self._x11.find_app_window(
                process_names=self.config.process_names,
                path_contains=self.config.process_path_contains,
                wm_class_hint=self.config.target_wm_class,
            ) or 0
            log(f"session:handoff-relookup window=0x{window_id:x}")
        if self._x11 is not None and window_id:
            if not self._wait_until_visible(window_id):
                # 预算耗尽仍不见可见也照样尝试一次：窗口管理器可能已就绪，
                # 只是 _NET_WM_STATE 还没来得及刷新。
                log("session:handoff-late-activate")
            log(f"session:handoff-activate ok={self._activate_confirmed(window_id)}")
        if self._launcher is not None:
            self._launcher.complete_handoff()
        self._result = SessionResult(ok=True, handed_off=True)
        log("session:handoff")

    def _activate_confirmed(self, window_id: int, budget: float = 0.6) -> bool:
        """激活目标窗口，并确认窗口管理器真的照办了。

        ``_NET_ACTIVE_WINDOW`` 是请求而不是命令：mutter 可能因为我们刚退出
        全屏、或焦点正被其它窗口抢占而忽略它。此时重试一次通常就够，但必须
        有确认环节，否则「动画放完了、焦点没交过去」会被静默吞掉。
        """

        assert self._x11 is not None
        deadline = time.monotonic() + budget
        while True:
            self._x11.activate(window_id)
            if self._x11.active_window() == window_id:
                return True
            if time.monotonic() >= deadline:
                return False
            self._pump(0.05)

    def _wait_until_visible(self, window_id: int, budget: float = 1.0) -> bool:
        """给合成器一点时间恢复被顶住的窗口。

        播放窗口刚销毁时，合成器需要一两帧重新计算遮挡关系，期间目标窗口
        仍然带着 ``_NET_WM_STATE_HIDDEN``。此时立刻激活会被窗口管理器忽略，
        因此这里轮询到窗口可用或预算耗尽为止；整个等待远小于一帧的观感阈值。
        """

        assert self._x11 is not None
        deadline = time.monotonic() + budget
        while True:
            if not self._x11.is_minimized(window_id):
                return True
            if time.monotonic() >= deadline:
                # 预算用完也照样尝试激活：窗口管理器那边可能已经就绪，
                # 只是 ``_NET_WM_STATE`` 还没刷新。
                return False
            self._pump(0.02)

    @staticmethod
    def _pump(seconds: float) -> None:
        """在不阻塞事件循环的前提下小睡一会儿。"""

        app = QCoreApplication.instance()
        if app is None:
            time.sleep(seconds)
            return
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.005)

    def _close_player(self) -> None:
        # 关窗口前先停定时器：定时器挂在窗口上，窗口没了它会一起没掉。
        self._stop_timer()
        if self._player is not None:
            self._player.close()
            self._player = None
        if self._window is not None:
            self._window.close()
            self._window = None

    # ------------------------------------------------------------------
    # 终止
    # ------------------------------------------------------------------
    def _on_skip(self) -> None:
        if self._launcher is None:
            return
        self._apply_tick(self._launcher.skip())
        self._maybe_finish()

    def _finish_video_failure(self) -> SessionResult:
        """视频打不开：立刻转为打开应用，不让用户干等。"""

        if self._launcher is not None:
            # 对应上游 ``MediaFailed``：写文案后立刻交接，播放窗口随之下线。
            # ``_apply_tick`` 会先画文案再关窗口，顺序由 ``TickResult`` 保证。
            self._apply_tick(self._launcher.video_failed("media-open-failed"))
        assert self._x11 is not None
        window_id = self._x11.find_app_window(
            process_names=self.config.process_names,
            path_contains=self.config.process_path_contains,
            wm_class_hint=self.config.target_wm_class,
        )
        if window_id:
            self._x11.activate(window_id)
        self._result = SessionResult(ok=False, reason="video-failed")
        log("session:video-failure-handled")
        return self._result

    def _maybe_finish(self) -> None:
        """状态机进入终止阶段时做一次收尾清理。"""

        if self._terminal_done or self._launcher is None:
            return
        if self._launcher.state.phase not in TERMINAL_PHASES:
            return
        self._terminal_done = True
        self._on_terminal()

    def _stop_timer(self) -> None:
        """停止并丢弃帧定时器。

        定时器以播放窗口为父对象：窗口一旦 close，Qt 会连带销毁定时器，
        此后调用 ``stop()`` 会抛 ``RuntimeError``（对象已被 C++ 侧删除）。
        这里统一吞掉这种情况——定时器本来就已经不在了。
        """

        timer = self._timer
        self._timer = None
        if timer is None:
            return
        try:
            timer.stop()
        except RuntimeError:
            log("session:timer-already-destroyed")

    def _on_terminal(self) -> None:
        """进入终止阶段：先停定时器，再清理资源。"""

        # 顺序很重要：必须先停定时器再关窗口，否则窗口销毁会带走定时器。
        self._stop_timer()
        if self._embedder is not None:
            self._embedder.close()
            self._embedder = None
        if self._x11 is not None:
            self._x11.close()
            self._x11 = None
        if self._launcher is not None and self._launcher.state.phase is Phase.FAILED_APP:
            self._result = SessionResult(ok=False, reason="app-timeout")
        self._close_player()
        callback = self._finished
        self._finished = None
        if callback is not None:
            try:
                callback()
            except Exception as exc:  # 回调失败不应影响退出
                log(f"session:finished-callback-failed err={exc}")

    def result(self) -> SessionResult:
        return self._result

    def shutdown(self) -> None:
        """进程退出前的兜底清理。"""

        self._stop_timer()
        if self._embedder is not None:
            try:
                self._embedder.close()
            except Exception:
                pass
            self._embedder = None
        if self._player is not None:
            try:
                self._player.close()
            except Exception:
                pass
            self._player = None
        if self._x11 is not None:
            try:
                self._x11.close()
            except Exception:
                pass
            self._x11 = None
        if self._window is not None:
            try:
                self._window.close()
            except Exception:
                pass
            self._window = None
