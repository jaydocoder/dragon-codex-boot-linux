"""``src/calibrate.py`` 的单元测试。

标定算法的核心是「差分取外接矩形」，这里用合成帧验证它：
在一块静止背景上画一个已知位置的亮块，算法必须原样把这块框出来。
真实的 mpv 抓帧依赖显卡与视频文件，不在单元测试范围内。
"""

from __future__ import annotations

import unittest

from src.calibrate import (
    DEFAULT_NOISE_RATIO,
    DEFAULT_SNAP_TOLERANCE,
    FrameBox,
    calibrate,
    diff_box,
    format_frames,
    format_report,
    frames_to_json,
    sanity_check,
    stabilize_box,
)

# 合成帧尺寸，取小值让测试跑得快。
WIDTH = 40
HEIGHT = 30


def blank(value: int = 0) -> bytes:
    """生成纯色帧。"""

    return bytes([value] * (WIDTH * HEIGHT * 3))


def with_box(left: int, top: int, width: int, height: int, value: int = 200) -> bytes:
    """在纯黑帧上画一个纯色矩形。"""

    data = bytearray(blank())
    for y in range(top, top + height):
        for x in range(left, left + width):
            offset = (y * WIDTH + x) * 3
            data[offset : offset + 3] = bytes([value, value, value])
    return bytes(data)


class TestDiffBox(unittest.TestCase):
    def test_finds_exact_rectangle(self) -> None:
        box = diff_box(blank(), with_box(5, 4, 10, 8), WIDTH, HEIGHT, 1.0)
        self.assertIsNotNone(box)
        assert box is not None
        self.assertEqual(
            (box.left, box.top, box.width, box.height), (5, 4, 10, 8)
        )

    def test_identical_frames_report_no_change(self) -> None:
        self.assertIsNone(diff_box(blank(), blank(), WIDTH, HEIGHT, 1.0))

    def test_below_threshold_is_ignored(self) -> None:
        # 差异 20 小于阈值 24，应被当成噪点过滤掉。
        box = diff_box(blank(), with_box(1, 1, 2, 2, value=20), WIDTH, HEIGHT, 1.0)
        self.assertIsNone(box)

    def test_threshold_is_configurable(self) -> None:
        frame = with_box(1, 1, 2, 2, value=20)
        box = diff_box(blank(), frame, WIDTH, HEIGHT, 1.0, threshold=5)
        self.assertIsNotNone(box)

    def test_isolated_noise_below_ratio_is_dropped(self) -> None:
        # 单个像素：1/1200 ≈ 0.00083 < 默认噪声比例 0.002。
        box = diff_box(blank(), with_box(10, 10, 1, 1), WIDTH, HEIGHT, 1.0)
        self.assertIsNone(box)
        self.assertLess(1 / (WIDTH * HEIGHT), DEFAULT_NOISE_RATIO)

    def test_noise_ratio_can_be_disabled(self) -> None:
        box = diff_box(
            blank(), with_box(10, 10, 1, 1), WIDTH, HEIGHT, 1.0, noise_ratio=0.0
        )
        self.assertIsNotNone(box)

    def test_rejects_mismatched_buffer_size(self) -> None:
        with self.assertRaises(ValueError):
            diff_box(b"", blank(), WIDTH, HEIGHT, 0.0)

    def test_rejects_bad_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            diff_box(b"", b"", 0, HEIGHT, 0.0)

    def test_changed_ratio(self) -> None:
        frame = with_box(0, 0, WIDTH, HEIGHT)
        box = diff_box(blank(), frame, WIDTH, HEIGHT, 1.0, noise_ratio=0.0)
        assert box is not None
        self.assertAlmostEqual(box.changed_ratio, 1.0)


class TestFrameBox(unittest.TestCase):
    def test_as_screen_frame_normalizes(self) -> None:
        box = FrameBox(
            time=12.5,
            left=4,
            top=3,
            width=20,
            height=15,
            changed_pixels=300,
            total_pixels=WIDTH * HEIGHT,
        )
        frame = box.as_screen_frame(WIDTH, HEIGHT)
        self.assertAlmostEqual(frame.x, 0.1)
        self.assertAlmostEqual(frame.y, 0.1)
        self.assertAlmostEqual(frame.width, 0.5)
        self.assertAlmostEqual(frame.height, 0.5)
        self.assertEqual(frame.time, 12.5)

    def test_as_screen_frame_rejects_zero_size(self) -> None:
        box = FrameBox(0.0, 0, 0, 1, 1, 1, 1)
        with self.assertRaises(ValueError):
            box.as_screen_frame(0, HEIGHT)


class TestStabilize(unittest.TestCase):
    def _box(self, left: int, top: int, width: int, height: int, time=1.0) -> FrameBox:
        return FrameBox(time, left, top, width, height, 100, WIDTH * HEIGHT)

    def test_uses_trimmed_mean_to_reject_outlier(self) -> None:
        boxes = [
            self._box(5, 5, 10, 10),
            self._box(5, 5, 10, 10),
            self._box(5, 5, 10, 10),
            self._box(0, 0, 40, 30),  # 明显异常的一帧
        ]
        stable = stabilize_box(boxes)
        assert stable is not None
        # 截尾均值剔除异常帧后，边界应回到 5。
        self.assertLessEqual(stable.left, 6)
        self.assertLessEqual(stable.top, 6)
        self.assertLess(stable.width, 20)

    def test_returns_none_for_all_empty(self) -> None:
        self.assertIsNone(stabilize_box([None, None]))

    def test_single_box_passes_through(self) -> None:
        stable = stabilize_box([self._box(2, 3, 4, 5)])
        assert stable is not None
        self.assertEqual(
            (stable.left, stable.top, stable.width, stable.height), (2, 3, 4, 5)
        )

    def test_rejects_empty_sequence(self) -> None:
        with self.assertRaises(ValueError):
            stabilize_box([])


class TestCalibrate(unittest.TestCase):
    """标定判据是「同一时间点连拍帧之间的差异」。"""

    def test_builds_frames_for_changing_regions(self) -> None:
        # 真实场景：背景静止，只有屏幕区域内部在动。
        samples = {
            1.0: [blank(), with_box(4, 3, 8, 6)],
            2.0: [blank(), with_box(2, 1, 16, 12)],
        }
        result = calibrate(samples, WIDTH, HEIGHT)
        frames = result.frames
        self.assertEqual(len(frames), 2)
        self.assertLess(frames[0].width, frames[1].width)
        self.assertEqual(frames[0].time, 1.0)
        self.assertEqual(frames[1].time, 2.0)
        # 没有种子时结果标注为未校验。
        self.assertFalse(result.trusted)

    def test_skips_samples_without_motion(self) -> None:
        samples = {
            1.0: [blank(), blank()],
            2.0: [blank(), with_box(2, 1, 16, 12)],
        }
        result = calibrate(samples, WIDTH, HEIGHT)
        self.assertEqual([frame.time for frame in result.frames], [2.0])
        sources = {item.time: item.source for item in result.report}
        self.assertEqual(sources[1.0], "none")
        self.assertEqual(sources[2.0], "diff")

    def test_unions_adjacent_pairs(self) -> None:
        # 三帧：相邻两对差异区域不同，结果应取并集。
        samples = {1.0: [blank(), with_box(0, 0, 5, 5), with_box(10, 10, 5, 5)]}
        result = calibrate(samples, WIDTH, HEIGHT)
        self.assertEqual(len(result.frames), 1)
        self.assertGreater(result.frames[0].width, 0.3)


class TestSeedMode(unittest.TestCase):
    """种子模式：差分只用于校验与微调，绝不从零猜边界。"""

    def _seed(self, time: float, x: float, y: float, w: float, h: float):
        from src.geometry import ScreenFrame

        return ScreenFrame(time=time, x=x, y=y, width=w, height=h)

    def test_matching_diff_is_adopted(self) -> None:
        # 差分矩形与种子几乎一致 → 采纳差分值，标记 refined。
        seed = self._seed(1.0, 4 / WIDTH, 3 / HEIGHT, 8 / WIDTH, 6 / HEIGHT)
        samples = {1.0: [blank(), with_box(4, 3, 8, 6)]}
        result = calibrate(samples, WIDTH, HEIGHT, seeds=[seed])
        self.assertEqual(result.report[0].source, "refined")
        self.assertTrue(result.trusted)

    def test_divergent_diff_keeps_seed(self) -> None:
        # 差分矩形偏离种子很远 → 判定差分不可信，保留种子。
        seed = self._seed(1.0, 0.10, 0.10, 0.50, 0.50)
        samples = {1.0: [blank(), with_box(4, 3, 8, 6)]}
        result = calibrate(samples, WIDTH, HEIGHT, seeds=[seed])
        self.assertEqual(result.report[0].source, "seed")
        self.assertAlmostEqual(result.frames[0].x, 0.10)
        self.assertAlmostEqual(result.frames[0].width, 0.50)
        self.assertIn("保留种子值", result.report[0].note)

    def test_static_moment_falls_back_to_seed(self) -> None:
        # 画面静止、差分无信号 → 用种子兜底，而不是丢掉这个采样点。
        seed = self._seed(1.0, 0.1, 0.1, 0.5, 0.5)
        samples = {1.0: [blank(), blank()]}
        result = calibrate(samples, WIDTH, HEIGHT, seeds=[seed])
        self.assertEqual(result.report[0].source, "seed")
        self.assertEqual(len(result.frames), 1)
        self.assertAlmostEqual(result.frames[0].x, 0.1)
        self.assertTrue(result.trusted)

    def test_static_moment_without_seed_is_reported(self) -> None:
        result = calibrate({1.0: [blank(), blank()]}, WIDTH, HEIGHT)
        self.assertEqual(result.report[0].source, "none")
        self.assertEqual(result.frames, [])
        self.assertFalse(result.trusted)

    def test_snap_tolerance_is_configurable(self) -> None:
        seed = self._seed(1.0, 0.10, 0.10, 0.50, 0.50)
        samples = {1.0: [blank(), with_box(4, 3, 8, 6)]}
        # 容差放大到 1.0 后，任何差分都会被采纳。
        loose = calibrate(samples, WIDTH, HEIGHT, seeds=[seed], snap_tolerance=1.0)
        self.assertEqual(loose.report[0].source, "refined")


class TestFormatReport(unittest.TestCase):
    def test_renders_seed_and_refined_rows(self) -> None:
        from src.calibrate import SampleReport, CalibrationResult
        from src.geometry import ScreenFrame

        frame = ScreenFrame(time=1.0, x=0.1, y=0.2, width=0.3, height=0.4)
        result = CalibrationResult(
            frames=[frame],
            report=[
                SampleReport(time=1.0, source="seed", frame=frame, note="测试"),
            ],
        )
        text = format_report(result)
        self.assertIn("1.00", text)
        self.assertIn("seed", text)
        self.assertIn("测试", text)

    def test_warns_when_result_incomplete(self) -> None:
        from src.calibrate import SampleReport, CalibrationResult

        result = CalibrationResult(
            frames=[],
            report=[SampleReport(time=1.0, source="none", frame=None, note="无信号")],
        )
        self.assertIn("标定结果不完整", format_report(result))

    def test_requires_two_frames_per_moment(self) -> None:
        with self.assertRaises(ValueError):
            calibrate({1.0: [blank()]}, WIDTH, HEIGHT)

    def test_rejects_empty_samples(self) -> None:
        with self.assertRaises(ValueError):
            calibrate({}, WIDTH, HEIGHT)

    def test_rejects_bad_dimensions(self) -> None:
        with self.assertRaises(ValueError):
            calibrate({1.0: [blank(), blank()]}, 0, HEIGHT)


class TestSanityCheck(unittest.TestCase):
    def _frame(self, time: float, x: float, y: float, w: float, h: float):
        from src.geometry import ScreenFrame

        return ScreenFrame(time=time, x=x, y=y, width=w, height=h)

    def test_good_sequence_passes(self) -> None:
        frames = [
            self._frame(12.65, 0.123, 0.094, 0.736, 0.725),
            self._frame(13.0, 0.074, 0.061, 0.845, 0.825),
            self._frame(13.5, 0.0, 0.0, 1.0, 1.0),
        ]
        self.assertEqual(sanity_check(frames), [])

    def test_too_few_frames(self) -> None:
        problems = sanity_check([self._frame(13.5, 0.0, 0.0, 1.0, 1.0)])
        self.assertTrue(any("至少需要" in item for item in problems))

    def test_shrinking_region_is_reported(self) -> None:
        frames = [
            self._frame(12.0, 0.0, 0.0, 0.9, 0.9),
            self._frame(13.0, 0.0, 0.0, 1.0, 1.0),
            self._frame(14.0, 0.2, 0.2, 0.5, 0.5),
        ]
        problems = sanity_check(frames)
        self.assertTrue(any("宽度比前一帧小" in item for item in problems))

    def test_last_frame_not_fullscreen_is_reported(self) -> None:
        frames = [
            self._frame(12.0, 0.1, 0.1, 0.5, 0.5),
            self._frame(13.0, 0.1, 0.1, 0.6, 0.6),
        ]
        problems = sanity_check(frames)
        self.assertTrue(any("末帧宽度" in item for item in problems))

    def test_non_increasing_time_is_reported(self) -> None:
        frames = [
            self._frame(13.0, 0.1, 0.1, 0.5, 0.5),
            self._frame(13.0, 0.0, 0.0, 1.0, 1.0),
        ]
        problems = sanity_check(frames)
        self.assertTrue(any("时间未递增" in item for item in problems))


class TestExport(unittest.TestCase):
    def test_frames_to_json_uses_pascal_case(self) -> None:
        from src.geometry import ScreenFrame

        payload = frames_to_json(
            [ScreenFrame(time=12.65, x=0.1234567, y=0.094, width=0.736, height=0.725)]
        )
        self.assertEqual(
            set(payload[0]),
            {"Time", "X", "Y", "Width", "Height"},
        )
        # 归一化坐标保留 4 位小数。
        self.assertEqual(payload[0]["X"], 0.1235)
        self.assertEqual(payload[0]["Time"], 12.65)

    def test_format_frames_includes_pixel_rect(self) -> None:
        from src.geometry import ScreenFrame

        text = format_frames([ScreenFrame(time=12.65, x=0.0, y=0.0, width=1.0, height=1.0)])
        self.assertIn("1920×1080", text)
        self.assertIn("(0,0,1920,1080)", text)


if __name__ == "__main__":
    unittest.main()
