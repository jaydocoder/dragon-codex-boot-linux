"""命令行入口测试。

重点覆盖参数解析与命令构造：这些逻辑出错会让桌面入口接管后整个启动链路
失效，而且很难从界面现象上定位。
"""

from __future__ import annotations

import unittest
from pathlib import Path

from src.__main__ import (
    EXIT_ENV_ERROR,
    _apply_launch_command,
    _resolve_launch_command,
    build_parser,
)
from src.config import Config


class TestResolveLaunchCommand(unittest.TestCase):
    def test_empty_returns_none(self):
        self.assertIsNone(_resolve_launch_command(""))

    def test_simple_command(self):
        self.assertEqual(_resolve_launch_command("/usr/bin/chatgpt"), ["/usr/bin/chatgpt"])

    def test_quoted_argument_with_spaces(self):
        parts = _resolve_launch_command('/usr/bin/chatgpt --user-data-dir "/tmp/a b"')
        self.assertEqual(parts, ["/usr/bin/chatgpt", "--user-data-dir", "/tmp/a b"])

    def test_drops_field_codes(self):
        # %U / %F 是桌面规范的字段码，直接启动时没有意义。
        parts = _resolve_launch_command("/usr/bin/chatgpt --profile-directory=Default %U")
        self.assertEqual(parts, ["/usr/bin/chatgpt", "--profile-directory=Default"])

    def test_only_field_codes_yields_none(self):
        self.assertIsNone(_resolve_launch_command("%U"))


class TestApplyLaunchCommand(unittest.TestCase):
    def test_sets_launch_command_parts(self):
        config = Config()
        _apply_launch_command(config, '/usr/bin/chatgpt --profile-directory "Default Profile"')
        self.assertEqual(
            config.launch_command_parts,
            ["/usr/bin/chatgpt", "--profile-directory", "Default Profile"],
        )
        self.assertEqual(config.app_command, "/usr/bin/chatgpt")

    def test_empty_raw_leaves_config_untouched(self):
        config = Config()
        before = list(config.launch_command_parts)
        _apply_launch_command(config, "")
        self.assertEqual(config.launch_command_parts, before)

    def test_unparsable_raw_does_not_raise(self):
        config = Config()
        # 未闭合的引号会让 shlex 抛错，这里必须安全忽略。
        _apply_launch_command(config, '/usr/bin/chatgpt "unterminated')
        self.assertEqual(config.launch_command_parts, [])


class TestParser(unittest.TestCase):
    def test_no_command(self):
        args = build_parser().parse_args([])
        self.assertIsNone(args.command)

    def test_run_defaults(self):
        args = build_parser().parse_args(["run"])
        self.assertEqual(args.command, "run")
        self.assertEqual(args.launch_command, "")
        self.assertFalse(args.skip_preflight)

    def test_run_with_launch_command(self):
        args = build_parser().parse_args(["run", "--launch-command", "/usr/bin/chatgpt --x"])
        self.assertEqual(args.launch_command, "/usr/bin/chatgpt --x")

    def test_entries_requires_one_action(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["entries"])
        args = parser.parse_args(["entries", "--plan"])
        self.assertTrue(args.plan)

    def test_entries_actions_are_exclusive(self):
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["entries", "--plan", "--adopt"])

    def test_calibrate_paths(self):
        args = build_parser().parse_args(
            ["calibrate", "--seed-from", "/tmp/a.json", "--video", "/tmp/v.mp4"]
        )
        self.assertEqual(args.seed_from, Path("/tmp/a.json"))
        self.assertEqual(args.video, Path("/tmp/v.mp4"))


if __name__ == "__main__":
    unittest.main()
