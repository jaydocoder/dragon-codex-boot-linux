"""启动状态机测试。

用假时钟取代 ``time.monotonic``，因为状态机的关键判断全部由「墙钟经过多久」
驱动（暂停等待、应用超时），而视频位置由调用方单独喂入。把两者分离后，
不需要真实播放视频就能精确验证每一条迁移路径。
"""

from __future__ import annotations

import unittest

from src.config import Config
from src.geometry import Rect
from src.launcher import (
    APP_TIMEOUT_GRACE_SECONDS,
    Phase,
    TERMINAL_PHASES,
    Launcher,
)

# 测试用客户端几何，取自上游实测的 ChatGPT 主窗口客户区。
CLIENT = Rect(236, 102, 1413, 783)
PLAYER = Rect(0, 0, 1920, 1080)


class FakeClock:
    """可控单调时钟。"""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_launcher(**overrides) -> tuple[Launcher, FakeClock]:
    clock = FakeClock()
    config = Config(**overrides)
    return Launcher(config, clock=clock), clock


class TestPhaseConstants(unittest.TestCase):
    def test_terminal_phases_include_all_failures(self) -> None:
        """三个终止态必须齐全，否则 tick() 会在失败后继续产生动作。"""

        self.assertIn(Phase.DONE, TERMINAL_PHASES)
        self.assertIn(Phase.FAILED_APP, TERMINAL_PHASES)
        self.assertIn(Phase.FAILED_VIDEO, TERMINAL_PHASES)
        self.assertNotIn(Phase.WAITING_APP, TERMINAL_PHASES)
        self.assertNotIn(Phase.HOLDING, TERMINAL_PHASES)
        self.assertNotIn(Phase.TRANSITION, TERMINAL_PHASES)

    def test_app_timeout_adds_grace(self) -> None:
        launcher, _ = make_launcher(max_wait_seconds=10.0)
        self.assertEqual(launcher.app_timeout(), 10.0 + APP_TIMEOUT_GRACE_SECONDS)


class TestPrepare(unittest.TestCase):
    def test_prepare_enters_waiting_with_player_rect(self) -> None:
        launcher, _ = make_launcher()
        result = launcher.prepare(PLAYER)

        self.assertEqual(launcher.state.phase, Phase.WAITING_APP)
        self.assertEqual(result.status, "waiting")
        self.assertEqual(launcher.state.player, PLAYER)
        self.assertEqual(launcher.elapsed(), 0.0)

    def test_elapsed_tracks_clock(self) -> None:
        launcher, clock = make_launcher()
        launcher.prepare(PLAYER)
        clock.advance(3.5)
        self.assertAlmostEqual(launcher.elapsed(), 3.5)


class TestWaitingApp(unittest.TestCase):
    def test_no_action_before_app_ready(self) -> None:
        """应用未就绪时视频照播，状态机不应产生任何动作。"""

        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        result = launcher.tick(position=5.0)
        self.assertEqual(launcher.state.phase, Phase.WAITING_APP)
        self.assertTrue(result.is_empty())

    def test_hold_only_after_app_ready(self) -> None:
        """视频已过 HoldAt 但应用没起来时不能提前暂停，否则用户会盯着静止画面。"""

        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        result = launcher.tick(position=12.0)
        self.assertIsNone(result.hold_at)
        self.assertEqual(launcher.state.phase, Phase.WAITING_APP)

        launcher.notify_app_ready(CLIENT)
        result = launcher.tick(position=12.0)
        self.assertAlmostEqual(result.hold_at, launcher.config.hold_at)
        self.assertEqual(launcher.state.phase, Phase.HOLDING)

    def test_notify_app_ready_ignores_degenerate_rect(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(Rect(0, 0, 0, 500))
        self.assertFalse(launcher.state.app_ready)
        self.assertIsNone(launcher.state.client)

    def test_notify_app_ready_ignored_in_terminal_phase(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.skip()
        launcher.complete_handoff()
        self.assertEqual(launcher.state.phase, Phase.DONE)
        launcher.notify_app_ready(CLIENT)
        self.assertFalse(launcher.state.app_ready)

    def test_ready_before_hold_waits_for_hold_at(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)

        result = launcher.tick(position=3.0)
        self.assertTrue(result.is_empty())
        self.assertEqual(launcher.state.phase, Phase.WAITING_APP)

    def test_timeout_fails(self) -> None:
        launcher, clock = make_launcher(max_wait_seconds=5.0)
        launcher.prepare(PLAYER)
        clock.advance(5.0 + APP_TIMEOUT_GRACE_SECONDS)

        result = launcher.tick(position=1.0)
        self.assertEqual(launcher.state.phase, Phase.FAILED_APP)
        self.assertEqual(result.status, "app-failed")
        self.assertEqual(result.failure, "target-app-timeout")

    def test_timeout_wins_over_app_ready(self) -> None:
        """超时判断在任何分支之前，应用刚就绪也不能改变失败结论。"""

        launcher, clock = make_launcher(max_wait_seconds=5.0)
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)
        clock.advance(5.0 + APP_TIMEOUT_GRACE_SECONDS)

        result = launcher.tick(position=0.1)
        self.assertEqual(launcher.state.phase, Phase.FAILED_APP)
        self.assertEqual(result.failure, "target-app-timeout")

    def test_video_end_without_app_keeps_waiting(self) -> None:
        """视频先播完不能算失败：要停在最后一帧继续等应用。"""

        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        result = launcher.tick(position=14.1, ended=True)
        self.assertTrue(result.is_empty())
        self.assertEqual(launcher.state.phase, Phase.WAITING_APP)

    def test_video_end_with_app_starts_transition(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)

        result = launcher.tick(position=14.1, ended=True)
        self.assertEqual(launcher.state.phase, Phase.TRANSITION)
        self.assertTrue(result.resume)
        self.assertAlmostEqual(result.progress, 0.0)


class TestHolding(unittest.TestCase):
    def _reach_holding(self, **overrides) -> tuple[Launcher, FakeClock]:
        launcher, clock = make_launcher(**overrides)
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)
        launcher.tick(position=launcher.config.hold_at)
        assert launcher.state.phase is Phase.HOLDING
        return launcher, clock

    def test_waits_full_gap_before_transition(self) -> None:
        """暂停时长等于 TransitionStart - HoldAt，与视频位置无关。"""

        launcher, clock = self._reach_holding()
        gap = launcher.config.transition_start - launcher.config.hold_at

        clock.advance(gap - 0.01)
        result = launcher.tick(position=launcher.config.hold_at)
        self.assertTrue(result.is_empty())
        self.assertEqual(launcher.state.phase, Phase.HOLDING)

        clock.advance(0.02)
        result = launcher.tick(position=launcher.config.hold_at)
        self.assertEqual(launcher.state.phase, Phase.TRANSITION)
        self.assertTrue(result.resume)

    def test_position_past_transition_start_skips_hold(self) -> None:
        """慢机器上视频可能一下子跳到过渡区，此时不应回退暂停。"""

        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)

        result = launcher.tick(position=13.0)
        self.assertEqual(launcher.state.phase, Phase.TRANSITION)
        self.assertIsNone(result.hold_at)
        self.assertTrue(result.resume)


class TestTransition(unittest.TestCase):
    def _reach_transition(self, **overrides) -> tuple[Launcher, FakeClock]:
        launcher, clock = make_launcher(**overrides)
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)
        launcher.tick(position=launcher.config.hold_at)
        gap = launcher.config.transition_start - launcher.config.hold_at
        clock.advance(gap)
        launcher.tick(position=launcher.config.hold_at)
        assert launcher.state.phase is Phase.TRANSITION
        return launcher, clock

    def test_alignment_emitted_once_on_entry(self) -> None:
        """对齐只在进入过渡的那一拍发出一次，重复对齐会让窗口抖动。"""

        launcher, clock = make_launcher()
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)
        launcher.tick(position=launcher.config.hold_at)
        clock.advance(launcher.config.transition_start - launcher.config.hold_at)

        entry = launcher.tick(position=launcher.config.hold_at)
        self.assertEqual(launcher.state.phase, Phase.TRANSITION)
        self.assertEqual(entry.align, launcher.target_client_rect())

        later = launcher.tick(position=launcher.config.transition_start + 0.1)
        self.assertIsNone(later.align)

    def test_progress_maps_video_position(self) -> None:
        launcher, _ = self._reach_transition()
        cfg = launcher.config
        span = cfg.transition_end - cfg.transition_start

        mid = launcher.tick(position=cfg.transition_start + span / 2.0)
        self.assertAlmostEqual(mid.progress, 0.5)

        # 视频位置回退（例如 mpv 重缓冲）不能让进度倒退到负数。
        before = launcher.tick(position=cfg.transition_start - 5.0)
        self.assertAlmostEqual(before.progress, 0.0)

    def test_progress_clamped_at_one(self) -> None:
        launcher, _ = self._reach_transition()
        result = launcher.tick(position=launcher.config.transition_end + 3.0)
        self.assertEqual(result.progress, 1.0)

    def test_transition_end_hands_off(self) -> None:
        launcher, _ = self._reach_transition()
        result = launcher.tick(position=launcher.config.transition_end)

        self.assertTrue(result.handoff)
        self.assertTrue(result.close_player)
        # 交接分两步：先关窗口（HANDOFF），窗口真的关掉后才落 DONE。
        self.assertEqual(launcher.state.phase, Phase.HANDOFF)

    def test_ended_forces_completion(self) -> None:
        """视频在过渡中提前播完（含帧率误差）也必须走完交接。"""

        launcher, _ = self._reach_transition()
        result = launcher.tick(position=launcher.config.transition_start + 0.01, ended=True)

        self.assertEqual(result.progress, 1.0)
        self.assertTrue(result.handoff)
        self.assertEqual(launcher.state.phase, Phase.HANDOFF)

    def test_alignment_deferred_until_client_known(self) -> None:
        """过渡开始时若还没拿到客户端几何，后续 tick 仍要补上对齐。"""

        launcher, clock = make_launcher()
        launcher.prepare(PLAYER)
        # 故意不让状态机知道客户端几何，只让视频推进触发过渡。
        launcher.tick(position=launcher.config.hold_at)  # 仍在 WAITING_APP
        self.assertEqual(launcher.state.phase, Phase.WAITING_APP)

        launcher.notify_app_ready(CLIENT)
        launcher.state.client = None  # 模拟窗口几何暂不可读
        result = launcher.tick(position=launcher.config.hold_at)
        self.assertIsNone(result.align)
        self.assertEqual(launcher.state.phase, Phase.HOLDING)

        # 拿到几何后进入过渡，对齐补发一次。
        launcher.state.client = CLIENT
        gap = launcher.config.transition_start - launcher.config.hold_at
        clock.advance(gap)
        result = launcher.tick(position=launcher.config.hold_at)
        self.assertEqual(launcher.state.phase, Phase.TRANSITION)
        self.assertEqual(result.align, launcher.target_client_rect())

    def test_terminal_phase_produces_no_action(self) -> None:
        launcher, _ = self._reach_transition()
        launcher.tick(position=launcher.config.transition_end)
        launcher.complete_handoff()
        self.assertEqual(launcher.state.phase, Phase.DONE)

        result = launcher.tick(position=20.0)
        self.assertTrue(result.is_empty())


class TestSkipAndFailure(unittest.TestCase):
    def test_skip_from_waiting_hands_off(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)

        result = launcher.skip()
        self.assertTrue(result.handoff)
        self.assertTrue(result.close_player)
        self.assertEqual(launcher.state.phase, Phase.HANDOFF)

    def test_skip_from_holding_hands_off(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)
        launcher.tick(position=launcher.config.hold_at)

        result = launcher.skip()
        self.assertTrue(result.handoff)
        self.assertEqual(launcher.state.phase, Phase.HANDOFF)

    def test_skip_is_idempotent_after_handoff_started(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.skip()

        # 已经进入 HANDOFF（正在关窗口）时再按 Esc 不能再发一次关窗动作。
        result = launcher.skip()
        self.assertTrue(result.is_empty())

    def test_skip_is_idempotent_after_terminal(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.skip()
        launcher.complete_handoff()

        result = launcher.skip()
        self.assertTrue(result.is_empty())

    def test_video_failed_closes_player(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)

        result = launcher.video_failed("解码器不可用")
        self.assertEqual(launcher.state.phase, Phase.FAILED_VIDEO)
        self.assertEqual(result.status, "video-failed")
        self.assertEqual(result.failure, "解码器不可用")
        self.assertTrue(result.close_player)

    def test_video_failed_after_done_is_noop(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.skip()
        launcher.complete_handoff()

        result = launcher.video_failed("迟到的错误")
        self.assertTrue(result.is_empty())
        self.assertEqual(launcher.state.phase, Phase.DONE)


class TestGeometry(unittest.TestCase):
    def test_player_rect_falls_back_to_config(self) -> None:
        launcher, _ = make_launcher(player_width=1600, player_height=900)
        self.assertEqual(launcher.player_rect(), Rect(0, 0, 1600, 900))

        launcher.prepare(Rect(10, 20, 800, 450))
        self.assertEqual(launcher.player_rect(), Rect(10, 20, 800, 450))

    def test_target_equals_player_rect(self) -> None:
        """过渡终点就是播放窗口本身：视频放大填满后客户端必须与之重合。"""

        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        self.assertEqual(launcher.target_client_rect(), PLAYER)

    def test_origin_is_reported_client_rect(self) -> None:
        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        self.assertIsNone(launcher.origin_client_rect())

        launcher.notify_app_ready(CLIENT)
        self.assertEqual(launcher.origin_client_rect(), CLIENT)

    def test_app_lost_clears_ready_but_keeps_geometry(self) -> None:
        """窗口消失时保留最后一次几何，便于过渡仍能算起点。"""

        launcher, _ = make_launcher()
        launcher.prepare(PLAYER)
        launcher.notify_app_ready(CLIENT)

        launcher.notify_app_lost()
        self.assertFalse(launcher.state.app_ready)
        self.assertEqual(launcher.state.client, CLIENT)


class TestFullHappyPath(unittest.TestCase):
    def test_drive_through_all_phases(self) -> None:
        """端到端：一次连续推进验证阶段顺序与关键动作齐全。"""

        launcher, clock = make_launcher()
        cfg = launcher.config

        self.assertEqual(launcher.prepare(PLAYER).status, "waiting")
        self.assertEqual(launcher.state.phase, Phase.WAITING_APP)

        # 视频播放到 HoldAt 之前应用就绪。
        launcher.notify_app_ready(CLIENT)
        self.assertTrue(launcher.tick(position=cfg.hold_at / 2).is_empty())

        hold = launcher.tick(position=cfg.hold_at)
        self.assertAlmostEqual(hold.hold_at, cfg.hold_at)
        self.assertEqual(launcher.state.phase, Phase.HOLDING)

        clock.advance(cfg.transition_start - cfg.hold_at)
        resume = launcher.tick(position=cfg.hold_at)
        self.assertTrue(resume.resume)
        self.assertEqual(launcher.state.phase, Phase.TRANSITION)
        self.assertEqual(resume.align, PLAYER)

        mid = launcher.tick(position=(cfg.transition_start + cfg.transition_end) / 2)
        self.assertAlmostEqual(mid.progress, 0.5)

        done = launcher.tick(position=cfg.transition_end)
        self.assertTrue(done.handoff)
        self.assertTrue(done.close_player)
        self.assertEqual(launcher.state.phase, Phase.HANDOFF)

        launcher.complete_handoff()
        self.assertEqual(launcher.state.phase, Phase.DONE)


if __name__ == "__main__":
    unittest.main()
