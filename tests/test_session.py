"""运行层串联测试。

``BootSession`` 的所有外部依赖都以工厂注入，这里全部换成假实现，
从而在无 X 服务器、无 mpv、无目标应用的环境下验证串联逻辑。
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from typing import Optional

from src.config import Config
from src.embedder import EmbedMode
from src.geometry import Rect
from src.launcher import Launcher, Phase
from src.session import BootSession, SessionResult, STATUS_TEXTS
from src.watcher import ProbeResult


# ----------------------------------------------------------------------
# 假实现
# ----------------------------------------------------------------------
class FakeX11:
    """假的 X11 窗口管理器。"""

    def __init__(self) -> None:
        self.screen = Rect(0, 0, 1920, 1080)
        self.geometry = Rect(236, 102, 1413, 783)
        self.app_window: Optional[int] = 0xAA00004
        self.aligned: list[tuple[int, Rect]] = []
        self.activated: list[int] = []
        self.closed = False
        self.minimized = False
        self.active_window_id = 0

    def screen_geometry(self) -> Rect:
        return self.screen

    def find_app_window(self, process_names, path_contains, wm_class_hint=""):
        return self.app_window

    def is_minimized(self, window_id: int) -> bool:
        return self.minimized

    def window(self, window_id: int):
        return window_id

    def geometry_of(self, window):
        @dataclass
        class Geometry:
            window: int
            frame: Rect
            client: Rect
            inset_left: int = 0
            inset_top: int = 0

        client = self.geometry
        return Geometry(window=window, frame=client, client=client)

    def align_client(self, window_id: int, desired: Rect, attempts: int = 3) -> bool:
        self.aligned.append((window_id, desired))
        return True

    def activate(self, window_id: int) -> bool:
        self.activated.append(window_id)
        # 真实窗口管理器会把请求落到 _NET_ACTIVE_WINDOW 上，假实现照此记账。
        self.active_window_id = window_id
        return True

    def active_window(self) -> int:
        return self.active_window_id

    def close(self) -> None:
        self.closed = True


class FakeEmbedder:
    """假的嵌入层。"""

    def __init__(self, mode: EmbedMode = EmbedMode.SHM) -> None:
        self.mode = mode
        self.selected: list[tuple[str, Optional[int]]] = []
        self.attached: list[int] = []
        self.begun = 0
        self.progress: list[float] = []
        self.ended = 0
        self.closed = False
        self.transitions: list[tuple[Rect, Rect, Rect]] = []
        self.compose_calls = 0

        class _State:
            window_id = 0

        self._state = _State()

    def select(self, requested: str, window_id: Optional[int] = None) -> EmbedMode:
        self.selected.append((requested, window_id))
        return self.mode

    def attach(self, window_id: int) -> None:
        self.attached.append(window_id)
        self._state.window_id = window_id

    def begin(self) -> bool:
        self.begun += 1
        return True

    def start_transition(self, origin: Rect, target: Rect, window: Rect) -> None:
        self.transitions.append((origin, target, window))

    def apply_progress(self, progress: float) -> None:
        self.progress.append(progress)

    def compose_frame(self, clip: Rect):
        self.compose_calls += 1
        return "frame"

    def end(self) -> None:
        self.ended += 1

    def close(self) -> None:
        self.closed = True


class FakePlayer:
    """假的 mpv 播放器。"""

    instances: list["FakePlayer"] = []
    fail_on_open = False
    next_position = 0.0
    next_ended = False

    def __init__(self, window_id: int, path, volume: float = 1.0) -> None:
        self.window_id = window_id
        self.path = path
        self.volume = volume
        self.seeked: list[float] = []
        self.paused = False
        self.resumed = False
        self.closed = False
        FakePlayer.instances.append(self)

    def open(self, timeout: float = 12.0):
        if FakePlayer.fail_on_open:
            from src.video import VideoError

            raise VideoError("模拟打开失败")

        @dataclass
        class Info:
            duration: float = 14.14
            width: int = 1920
            height: int = 1080

        return Info()

    def position(self) -> float:
        return FakePlayer.next_position

    def progress_position(self) -> float:
        """与真实播放器一致：过渡推进用这个位置。"""

        return FakePlayer.next_position

    def ended(self) -> bool:
        return FakePlayer.next_ended

    def seek(self, seconds: float) -> None:
        self.seeked.append(seconds)

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.resumed = True
        self.paused = False
        self.paused = False

    def close(self) -> None:
        self.closed = True


class FakeWindow:
    """假的播放窗口。"""

    def __init__(self, geometry: Rect) -> None:
        self._geometry = geometry
        self.statuses: list[str] = []
        self.overlays: list[object] = []
        self.shown = False
        self.closed = False
        self.skip_callback = None

        class _Signal:
            def __init__(self, owner):
                self._owner = owner

            def connect(self, fn):
                self._owner.skip_callback = fn

        self.skipRequested = _Signal(self)

    def show_centered(self) -> None:
        self.shown = True

    def winId(self) -> int:
        return 0x1A00001

    def show_status(self, text: str) -> None:
        self.statuses.append(text)

    def fixed_geometry(self) -> Rect:
        return self._geometry

    def set_overlay(self, frame) -> None:
        self.overlays.append(frame)

    def close(self) -> None:
        self.closed = True


class FakeTimer:
    """假的 QTimer：记录启动间隔与回调，由测试手动触发。"""

    def __init__(self, parent=None) -> None:
        self.parent = parent
        self.interval_ms: Optional[int] = None
        self.callback = None
        self.stopped = False

    def timeout_connect(self, fn) -> None:
        self.callback = fn

    def start(self, interval: int) -> None:
        self.interval_ms = interval

    def stop(self) -> None:
        self.stopped = True

    def fire(self) -> None:
        if self.callback is not None:
            self.callback()


def make_timer(parent=None) -> FakeTimer:
    """``QTimer`` 的替代：把 ``timeout.connect`` 记为普通属性。"""

    timer = FakeTimer(parent)
    # 模拟 ``timer.timeout.connect(fn)`` 的属性链。
    timer.timeout = _SignalProxy(timer)
    return timer


class _SignalProxy:
    def __init__(self, timer: FakeTimer) -> None:
        self._timer = timer

    def connect(self, fn) -> None:
        self._timer.callback = fn


class FakeWatcher:
    """假的窗口探测器：是否 ready 由测试直接控制。"""

    def __init__(self, find_window=None, is_responsive=None, clock=None, **kwargs) -> None:
        self.find_window = find_window
        self.ready = True
        self.polls = 0

    def poll(self, force: bool = False) -> ProbeResult:
        self.polls += 1
        candidate = self.find_window() if self.find_window is not None else None
        if candidate is None:
            self.ready = False
            return ProbeResult(candidate=None, ready=False)
        return ProbeResult(candidate=candidate, ready=self.ready)


def make_config(**overrides) -> Config:
    config = Config()
    config.video = "media/startup.mp4"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class SessionTestBase(unittest.TestCase):
    """提供统一的假依赖装配。"""

    def make_session(
        self, *, embed_mode=EmbedMode.SHM, config=None, embedder=None, finished=None
    ):
        self.clock = [1000.0]
        self.x11 = FakeX11()
        self.embedder = embedder if embedder is not None else FakeEmbedder(mode=embed_mode)
        self.player_cls = FakePlayer
        FakePlayer.instances = []
        FakePlayer.fail_on_open = False
        FakePlayer.next_position = 0.0
        FakePlayer.next_ended = False
        self.spawned: list[list[str]] = []

        def spawn(args, **kwargs):
            self.spawned.append(args)
            return None

        session = BootSession(
            config or make_config(),
            x11_factory=lambda: self.x11,
            embedder_factory=lambda: self.embedder,
            player_factory=FakePlayer,
            window_factory=FakeWindow,
            launcher_factory=lambda cfg: Launcher(cfg, clock=lambda: self.clock[0]),
            watcher_factory=FakeWatcher,
            timer_factory=make_timer,
            spawn=spawn,
            finished=finished,
        )
        self.finished_calls = []
        return session


# ----------------------------------------------------------------------
# 启动
# ----------------------------------------------------------------------
class TestStart(SessionTestBase):
    def test_start_builds_window_and_opens_video(self):
        session = self.make_session()
        result = session.start()
        self.assertTrue(result.ok)
        self.assertTrue(session._window.shown)
        self.assertEqual(len(FakePlayer.instances), 1)
        # 播放窗口的 X11 窗口号透传给 mpv 的 wid。
        self.assertEqual(FakePlayer.instances[0].window_id, 0x1A00001)

    def test_start_selects_embed_mode_twice(self):
        session = self.make_session()
        session.start()
        # 一次静态选档（无窗口号），一次带真实窗口号复选。
        self.assertEqual(len(self.embedder.selected), 1)
        self.assertIsNone(self.embedder.selected[0][1])
        session._on_tick()
        self.assertEqual(len(self.embedder.selected), 2)
        self.assertEqual(self.embedder.selected[1][1], 0xAA00004)

    def test_start_spawns_app_command(self):
        session = self.make_session(config=make_config(app_command="/usr/bin/chatgpt"))
        session.start()
        self.assertEqual(self.spawned, [["/usr/bin/chatgpt"]])

    def test_start_without_app_command_does_not_spawn(self):
        session = self.make_session(config=make_config(app_command=""))
        session.start()
        self.assertEqual(self.spawned, [])

    def test_player_rect_is_centered(self):
        session = self.make_session(
            config=make_config(player_width=800, player_height=600)
        )
        session.start()
        rect = session._window.fixed_geometry()
        self.assertEqual(rect, Rect(560, 240, 800, 600))

    def test_player_rect_shrinks_to_screen(self):
        session = self.make_session(
            config=make_config(player_width=4000, player_height=3000)
        )
        session.start()
        rect = session._window.fixed_geometry()
        # 配置尺寸超过屏幕时收缩到屏幕尺寸，位置归零。
        self.assertEqual(rect, Rect(0, 0, 1920, 1080))

    def test_video_failure_falls_back_to_app(self):
        session = self.make_session()
        window = None
        original_factory = session._window_factory

        def remember(rect):
            nonlocal window
            window = original_factory(rect)
            return window

        session._window_factory = remember
        FakePlayer.fail_on_open = True
        result = session.start()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, "video-failed")
        # 对应上游 MediaFailed：先写文案，再交接焦点、下线播放窗口。
        self.assertIn(STATUS_TEXTS["video-failed"], window.statuses)
        self.assertIn(0xAA00004, self.x11.activated)
        self.assertIsNone(session._window)

    def test_x11_unavailable_returns_failure(self):
        from src.x11 import X11Error

        def broken():
            raise X11Error("没有 DISPLAY")

        session = BootSession(make_config(), x11_factory=broken)
        result = session.start()
        self.assertFalse(result.ok)
        self.assertIn("无法连接 X 服务器", result.reason)


# ----------------------------------------------------------------------
# 每帧推进
# ----------------------------------------------------------------------
class TestTick(SessionTestBase):
    def test_app_ready_notifies_launcher_and_attaches_embedder(self):
        session = self.make_session()
        session.start()
        session._on_tick()
        self.assertTrue(session._launcher.state.app_ready)
        self.assertEqual(session._launcher.state.client, Rect(236, 102, 1413, 783))
        self.assertEqual(self.embedder.attached, [0xAA00004])

    def test_no_window_does_not_attach(self):
        session = self.make_session()
        self.x11.app_window = None
        session.start()
        session._on_tick()
        self.assertFalse(session._launcher.state.app_ready)
        self.assertEqual(self.embedder.attached, [])

    def test_progress_drives_embedder(self):
        session = self.make_session()
        session.start()
        # 直接构造一个带进度的 TickResult 验证分派路径。
        from src.launcher import TickResult

        session._transition_started = True
        session._apply_tick(TickResult(progress=0.4))
        self.assertEqual(self.embedder.progress, [0.4])

    def test_composite_mode_sets_overlay(self):
        session = self.make_session(embed_mode=EmbedMode.COMPOSITE)
        session.start()
        from src.launcher import TickResult

        session._transition_started = True
        session._apply_tick(TickResult(progress=0.5))
        self.assertEqual(session._window.overlays, ["frame"])
        self.assertEqual(self.embedder.compose_calls, 1)

    def test_shm_mode_does_not_set_overlay(self):
        session = self.make_session(embed_mode=EmbedMode.SHM)
        session.start()
        from src.launcher import TickResult

        session._transition_started = True
        session._apply_tick(TickResult(progress=0.5))
        self.assertEqual(session._window.overlays, [])
        self.assertEqual(self.embedder.compose_calls, 0)

    def test_progress_without_transition_is_ignored(self):
        session = self.make_session()
        session.start()
        from src.launcher import TickResult

        session._apply_tick(TickResult(progress=0.4))
        self.assertEqual(self.embedder.progress, [])


# ----------------------------------------------------------------------
# 对齐与过渡
# ----------------------------------------------------------------------
class TestAlign(SessionTestBase):
    def test_align_calls_x11_with_desired_rect(self):
        session = self.make_session()
        session.start()
        # 先跑一拍让探测器找到窗口并绑定到嵌入层，否则对齐没有作用对象。
        session._on_tick()
        session._align(Rect(10, 20, 300, 400))
        self.assertEqual(self.x11.aligned, [(0xAA00004, Rect(10, 20, 300, 400))])
        self.assertGreaterEqual(self.embedder.begun, 1)
        self.assertTrue(session._transition_started)
        self.assertEqual(len(self.embedder.transitions), 1)

    def test_align_skipped_without_attached_window(self):
        session = self.make_session()
        session.start()
        # 目标窗口不存在 → 嵌入层从未 attach，对齐必须安全跳过。
        self.x11.app_window = None
        session._on_tick()
        session._align(Rect(10, 20, 300, 400))
        self.assertEqual(self.x11.aligned, [])
        self.assertFalse(session._transition_started)

    def test_transition_resumes_playback(self):
        """回归：过渡第一拍必须真正恢复播放。

        状态机在过渡开始时给出 ``resume=True``；如果运行层只处理 ``hold_at``
        而不处理 ``resume``，播放位置会一直停在 HoldAt，过渡永远走不完
        （实测卡在 TransitionStart 直到 60 秒超时）。
        """

        session = self.make_session()
        session.start()
        session._on_tick()
        player = session._player
        self.assertFalse(player.resumed)

        FakePlayer.next_position = session.config.hold_at
        session._on_tick()
        self.assertTrue(player.paused)
        self.assertFalse(player.resumed)

        # 视频被 seek 到 HoldAt 后暂停；下一拍状态机要求恢复播放。
        # TransitionStart 由墙钟推算，与播放位置无关；推进时钟即可触发。
        self.clock[0] += session.config.transition_start - session.config.hold_at + 0.1
        session._on_tick()
        self.assertTrue(player.resumed)

    def test_align_skipped_without_embedder(self):
        session = self.make_session()
        session._embedder = None
        session._align(Rect(10, 20, 300, 400))
        self.assertEqual(self.x11.aligned, [])


# ----------------------------------------------------------------------
# 交接与终止
# ----------------------------------------------------------------------
class TestHandoff(SessionTestBase):
    def test_handoff_ends_embedder_and_activates(self):
        session = self.make_session()
        session.start()
        session._on_tick()
        session._handoff()
        self.assertEqual(self.embedder.ended, 1)
        self.assertIn(0xAA00004, self.x11.activated)
        self.assertTrue(session.result().ok)
        self.assertTrue(session.result().handed_off)

    def test_handoff_closes_player_before_activating(self):
        """回归：必须先关播放窗口再激活真实窗口。

        播放窗口是无边框置顶窗口，它盖住的真实窗口会被合成器标记成不可见；
        此时激活会被窗口管理器忽略（实测联调中交接就此中断）。
        """

        session = self.make_session()
        session.start()
        session._on_tick()  # 让嵌入层拿到真实窗口号
        order = []
        window = session._window
        origin_close = window.close
        window.close = lambda: (order.append("close-window"), origin_close())
        def fake_activate(wid):
            order.append("activate")
            self.x11.active_window_id = wid
            return True

        self.x11.activate = fake_activate

        # 走真实关闭路径（_close_player）再交接，与生产一致。
        session._close_player()
        session._handoff()

        self.assertEqual(order, ["close-window", "activate"])

    def test_handoff_waits_for_window_to_become_visible(self):
        """合成器还没来得及恢复遮挡时，要等一小会儿再激活。"""

        session = self.make_session()
        session.start()
        session._on_tick()  # 让嵌入层拿到真实窗口号
        session._close_player()
        # 前两次探测仍是「隐藏」，之后恢复可用。
        states = [True, True, False]
        self.x11.is_minimized = lambda wid: states.pop(0) if states else False

        session._handoff()

        self.assertIn(0xAA00004, self.x11.activated)

    def test_handoff_completes_launcher_phase(self):
        session = self.make_session()
        session.start()
        session._launcher.skip()
        session._handoff()
        self.assertEqual(session._launcher.state.phase, Phase.DONE)

    def test_handoff_activates_anyway_when_window_never_visible(self):
        """预算耗尽仍要最后尝试一次激活。

        窗口管理器可能已经就绪，只是 ``_NET_WM_STATE`` 还没刷新；
        漏掉这一枪，用户就只能看到播放窗口消失、焦点不知去向。
        """

        session = self.make_session()
        session.start()
        session._on_tick()  # 让嵌入层拿到真实窗口号
        session._wait_until_visible = lambda wid, budget=0.0: False

        session._handoff()

        self.assertIn(0xAA00004, self.x11.activated)
        self.assertTrue(session.result().handed_off)

    def test_handoff_retries_activation_until_confirmed(self):
        """窗口管理器忽略第一次激活时必须重试。

        ``_NET_ACTIVE_WINDOW`` 只是请求：刚退出全屏、或焦点被别的窗口抢走时，
        mutter 会直接忽略。此时若不复核，用户看到的就是「动画放完，焦点没交
        过去」，而这在日志里完全看不出来。
        """

        session = self.make_session()
        session.start()
        session._on_tick()
        ignored = [1]  # 第一次被忽略，第二次才落地
        calls = []

        def flaky_activate(wid):
            calls.append(wid)
            # 每次请求都先把「当前活动窗口」清空，模拟窗口管理器没照办；
            # 否则早前一次成功的激活会残留下来，测不出重试行为。
            self.x11.active_window_id = 0
            if ignored:
                ignored.pop()
                return True  # 请求发出去了，但窗口管理器没照办
            self.x11.active_window_id = wid
            return True

        self.x11.activate = flaky_activate

        session._handoff()

        self.assertEqual(len(calls), 2)
        self.assertEqual(self.x11.active_window_id, 0xAA00004)
        self.assertTrue(session.result().handed_off)

    def test_handoff_relooks_window_when_embedder_never_attached(self):
        """嵌入层没 attach 过时，交接仍要现找一个窗口再激活。

        应用识别较慢或嵌入层不可用时会出现这种情况；若直接放弃，
        用户看到的就只有「播放窗口消失了」，应用却没被拉到前面。
        """

        session = self.make_session()
        session.start()
        session._on_tick()
        session._embedder._state.window_id = 0
        self.x11.find_app_window = lambda **kwargs: 0xBB00007

        session._handoff()

        self.assertIn(0xBB00007, self.x11.activated)

    def test_handoff_relookup_misses_gracefully(self):
        """现找也找不到时不得抛异常，只记一条空窗口日志。"""

        session = self.make_session()
        session.start()
        session._on_tick()
        session._embedder._state.window_id = 0
        self.x11.find_app_window = lambda **kwargs: None

        session._handoff()

        self.assertEqual(self.x11.activated, [])
        self.assertTrue(session.result().handed_off)

    def test_close_player_clears_references(self):
        session = self.make_session()
        session.start()
        session._close_player()
        self.assertIsNone(session._player)
        self.assertIsNone(session._window)

    def test_terminal_notifies_finished_callback_once(self):
        """终止阶段必须通知入口退出事件循环。

        Qt 只在最后一个窗口关闭时自动退出；会话里还挂着定时器等非窗口对象，
        实测少了这一枪进程会一直空转不结束。
        """

        calls = []
        session = self.make_session(finished=lambda: calls.append(1))
        session.start()
        # HANDOFF 只是「已开始交接」，真正的终止阶段是 DONE。
        session._launcher._enter_handoff()
        session._launcher.complete_handoff()
        session._maybe_finish()
        self.assertEqual(calls, [1])

        # 幂等：再次进入终止阶段不得重复通知。
        session._maybe_finish()
        self.assertEqual(calls, [1])

    def test_finished_callback_failure_is_swallowed(self):
        """回调抛异常只能记日志，不得让收尾流程崩掉。"""

        def boom():
            raise RuntimeError("quit 失败")

        session = self.make_session(finished=boom)
        session.start()
        session._launcher._enter_handoff()
        session._launcher.complete_handoff()
        session._maybe_finish()
        self.assertTrue(session._terminal_done)

    def test_terminal_cleanup_runs_once(self):
        session = self.make_session()
        session.start()
        session._launcher._enter_handoff()
        session._launcher.complete_handoff()
        session._maybe_finish()
        self.assertTrue(session._terminal_done)
        self.assertTrue(self.x11.closed)
        # 第二次进入不再重复清理。
        self.embedder.closed = False
        session._maybe_finish()
        self.assertFalse(self.embedder.closed)

    def test_skip_closes_session(self):
        session = self.make_session()
        session.start()
        session._window.skip_callback()
        self.assertTrue(session._terminal_done)

    def test_terminal_survives_already_destroyed_timer(self):
        """回归：定时器挂在播放窗口上，窗口销毁后 stop() 会抛 RuntimeError。"""

        class DoomedTimer(FakeTimer):
            def stop(self) -> None:
                raise RuntimeError("wrapped C/C++ object of type QTimer has been deleted")

        session = self.make_session()
        session.start()
        session._timer = DoomedTimer()
        session._on_terminal()  # 必须吞掉 RuntimeError
        self.assertIsNone(session._timer)

    def test_close_player_stops_timer_first(self):
        session = self.make_session()
        session.start()
        timer = session._timer
        session._close_player()
        self.assertTrue(timer.stopped)
        self.assertIsNone(session._timer)

    def test_app_timeout_produces_failure(self):
        session = self.make_session()
        clock = [1000.0]
        session._launcher = Launcher(make_config(max_wait_seconds=1.0), clock=lambda: clock[0])
        session._launcher.prepare(Rect(0, 0, 1920, 1080))
        session._launcher._started_at = 0.0
        FakePlayer.next_position = 0.0
        # 让应用始终不可见，触发等待超时。
        self.x11.app_window = None
        session._player = FakePlayer(1, "x")
        # 直接把时间推到超时之后。
        clock[0] = 100.0
        result = session._launcher.tick(0.0)
        self.assertIsNotNone(result.failure)


# ----------------------------------------------------------------------
# 资源清理
# ----------------------------------------------------------------------
class TestShutdown(SessionTestBase):
    def test_shutdown_closes_everything(self):
        session = self.make_session()
        session.start()
        window = session._window
        session.shutdown()
        self.assertTrue(self.x11.closed)
        self.assertTrue(self.embedder.closed)
        self.assertTrue(FakePlayer.instances[0].closed)
        self.assertTrue(window.closed)

    def test_shutdown_is_idempotent(self):
        session = self.make_session()
        session.start()
        session.shutdown()
        session.shutdown()  # 不应抛异常
        self.assertIsNone(session._x11)

    def test_shutdown_survives_broken_components(self):
        class Broken(FakeX11):
            def close(self) -> None:
                raise RuntimeError("连接已断开")

        session = self.make_session()
        session.start()
        session._x11 = Broken()
        session.shutdown()  # 必须吞掉异常
        self.assertIsNone(session._x11)


# ----------------------------------------------------------------------
# 状态文案
# ----------------------------------------------------------------------
class TestStatusTexts(unittest.TestCase):
    def test_all_status_keys_are_mapped(self):
        self.assertEqual(set(STATUS_TEXTS), {"waiting", "holding", "video-failed", "app-failed"})

    def test_texts_match_upstream(self):
        self.assertIn("Codex", STATUS_TEXTS["waiting"])
        self.assertIn("视频无法播放", STATUS_TEXTS["video-failed"])
        self.assertIn("无法自动打开Codex", STATUS_TEXTS["app-failed"])


class TestSessionResult(unittest.TestCase):
    def test_defaults(self):
        result = SessionResult(ok=True)
        self.assertEqual(result.reason, "")
        self.assertFalse(result.handed_off)


if __name__ == "__main__":
    unittest.main()
