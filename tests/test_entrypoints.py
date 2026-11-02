"""桌面入口管理测试。

全部在临时目录里构造 .desktop 文件，不触碰真实的 ``~/.local/share/applications``。
本机真实的入口既是测试素材也是风险来源，因此测试只依赖可控副本。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.config import Config
from src.entrypoints import (
    BACKUP_SUFFIX,
    EntryPlan,
    adopt,
    backup_path,
    build_plan,
    looks_wrapped,
    parse_desktop_entry,
    program_exists,
    restore,
    sha256_of,
    verify,
)

# 官方系统级入口：Exec 是裸命令名。
OFFICIAL = """[Desktop Entry]
Name=ChatGPT
Comment=Official client
Exec=chatgpt %U
Icon=chatgpt
Type=Application
Categories=Utility;
StartupWMClass=Chatgpt
"""

WRAPPED_TEMPLATE = """[Desktop Entry]
Name=ChatGPT
Exec={wrapper} %U
Type=Application
StartupWMClass=Chatgpt
"""


def wrapped_entry(wrapper: Path) -> str:
    """构造「已被第三方脚本包装」的入口内容。

    包装脚本路径由调用方给出，必须落在临时目录里并真实存在：
    ``looks_wrapped()`` 按 ``Path.home()`` 下的用户目录判定，
    ``program_exists()`` 又要求该文件真的存在。写死 ``/home/neo/...``
    会让 CI（HOME=/home/runner）判成「未包装 + 程序不存在」。
    """

    return WRAPPED_TEMPLATE.format(wrapper=wrapper)

# 与本工具无关的入口。
UNRELATED = """[Desktop Entry]
Name=Telegram
Exec=/opt/Telegram/Telegram -- %U
Type=Application
"""


class TempDirs(unittest.TestCase):
    """提供 project_root 与 applications 两级临时目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.apps = self.root / "applications"
        self.apps.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_entry(self, name: str, content: str, *, directory: Path | None = None) -> Path:
        path = (directory or self.apps) / name
        path.write_text(content, encoding="utf-8")
        return path

    def make_config(self, **overrides) -> Config:
        # base_dir 指向临时项目根，让 run.sh 推断落在沙箱里。
        overrides.setdefault("base_dir", self.root)
        overrides.setdefault("app_command", "chatgpt")
        return Config(**overrides)

    def write_wrapper(self, *, home_local: bool = False) -> Path:
        """落一个真实存在的包装脚本，供 WRAPPED 入口引用。

        ``home_local=True`` 时写到 ``Path.home()/.local/bin``，用于验证
        「Exec 指向用户目录下的脚本即视为已包装」这一判据；否则写在临时
        目录里，测试结束随临时目录一起清理。
        """

        if home_local:
            wrapper = Path.home() / ".local" / "bin" / "chatgpt-codexhost-launcher"
            wrapper.parent.mkdir(parents=True, exist_ok=True)
            self.addCleanup(wrapper.unlink, missing_ok=True)
        else:
            wrapper = self.root / "bin" / "chatgpt-codexhost-launcher"
            wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        wrapper.chmod(0o755)
        return wrapper


class TestParse(TempDirs):
    def test_parses_top_level_fields(self) -> None:
        path = self.write_entry("chatgpt.desktop", OFFICIAL)
        entry = parse_desktop_entry(path)

        self.assertIsNotNone(entry)
        self.assertEqual(entry.name, "ChatGPT")
        self.assertEqual(entry.exec_line, "chatgpt %U")
        self.assertEqual(entry.startup_wm_class, "Chatgpt")
        self.assertEqual(entry.action_sections, [])

    def test_section_scoped_parsing(self) -> None:
        """动作组里的字段不能混进主组，否则会误判 Exec。"""

        content = """[Desktop Entry]
Name=App
Exec=/usr/bin/app %U
Type=Application

[Desktop Action new-window]
Name=New Window
Exec=/usr/bin/app --new
"""
        path = self.write_entry("app.desktop", content)
        entry = parse_desktop_entry(path)

        self.assertEqual(entry.exec_line, "/usr/bin/app %U")
        self.assertEqual(entry.action_sections, ["Desktop Action new-window"])

    def test_missing_exec_returns_none(self) -> None:
        path = self.write_entry("broken.desktop", "[Desktop Entry]\nName=X\n")
        self.assertIsNone(parse_desktop_entry(path))

    def test_unreadable_file_returns_none(self) -> None:
        missing = self.apps / "nope.desktop"
        self.assertIsNone(parse_desktop_entry(missing))

    def test_exec_arguments_and_program(self) -> None:
        path = self.write_entry("c.desktop", OFFICIAL)
        entry = parse_desktop_entry(path)
        self.assertEqual(entry.exec_program(), "chatgpt")
        self.assertEqual(entry.exec_arguments(), "%U")

    def test_quoted_exec_program(self) -> None:
        content = '[Desktop Entry]\nName=Q\nExec="/opt/My App/bin/app" %U\n'
        path = self.write_entry("q.desktop", content)
        entry = parse_desktop_entry(path)
        self.assertEqual(entry.exec_program(), "/opt/My App/bin/app")
        self.assertEqual(entry.exec_arguments(), "%U")


class TestFlags(TempDirs):
    def test_hidden_and_nodisplay(self) -> None:
        content = "[Desktop Entry]\nName=H\nExec=chatgpt\nHidden=true\nNoDisplay=true\n"
        entry = parse_desktop_entry(self.write_entry("h.desktop", content))
        self.assertTrue(entry.is_hidden)
        self.assertTrue(entry.is_nodisplay)

    def test_managed_detects_marker(self) -> None:
        content = "[Desktop Entry]\nName=M\nExec=/x/run.sh --launch-command chatgpt\n"
        entry = parse_desktop_entry(self.write_entry("m.desktop", content))
        self.assertTrue(entry.is_managed)

        plain = parse_desktop_entry(self.write_entry("p.desktop", OFFICIAL))
        self.assertFalse(plain.is_managed)

    def test_looks_wrapped_for_user_local_binary(self) -> None:
        wrapper = self.write_wrapper(home_local=True)
        entry = parse_desktop_entry(self.write_entry("w.desktop", wrapped_entry(wrapper)))
        self.assertTrue(looks_wrapped(entry))

    def test_looks_wrapped_false_for_official(self) -> None:
        entry = parse_desktop_entry(self.write_entry("o.desktop", OFFICIAL))
        self.assertFalse(looks_wrapped(entry))

    def test_program_exists_for_bare_command(self) -> None:
        """chatgpt 是裸命令名，走 PATH 判断；不存在时返回 False。"""

        content = "[Desktop Entry]\nName=X\nExec=/definitely/not/here/app\n"
        entry = parse_desktop_entry(self.write_entry("x.desktop", content))
        self.assertFalse(program_exists(entry))


class TestBuildPlan(TempDirs):
    def test_unrelated_entry_not_planned(self) -> None:
        self.write_entry("telegram.desktop", UNRELATED)
        plans = build_plan(self.make_config(), directories=[self.apps])
        self.assertEqual(plans, [])

    def test_wrapped_entry_skipped_by_default(self) -> None:
        # 「已包装」判据依赖 Exec 落在用户目录下，这里必须用 ~/.local/bin。
        wrapper = self.write_wrapper(home_local=True)
        self.write_entry("chatgpt.desktop", wrapped_entry(wrapper))
        plans = build_plan(self.make_config(), directories=[self.apps])

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].action, EntryPlan.SKIP)
        self.assertIn("包装", plans[0].reason)

    def test_wrapped_entry_adopted_when_enabled(self) -> None:
        self.write_entry("chatgpt.desktop", wrapped_entry(self.write_wrapper()))
        config = self.make_config(include_wrapped_entrypoints=True)
        plans = build_plan(config, directories=[self.apps])
        self.assertEqual(plans[0].action, EntryPlan.ADOPT)

    def test_exclude_list_wins(self) -> None:
        self.write_entry("chatgpt.desktop", wrapped_entry(self.write_wrapper()))
        config = self.make_config(
            include_wrapped_entrypoints=True,
            exclude_entrypoints=["chatgpt.desktop"],
        )
        plans = build_plan(config, directories=[self.apps])
        self.assertEqual(plans[0].action, EntryPlan.SKIP)
        self.assertIn("ExcludeEntrypoints", plans[0].reason)

    def test_user_dir_shadows_system_dir(self) -> None:
        """同名入口只处理最靠前的那个，符合 XDG 覆盖语义。"""

        system = self.root / "system-apps"
        system.mkdir()
        self.write_entry("chatgpt.desktop", wrapped_entry(self.write_wrapper()))
        self.write_entry("chatgpt.desktop", OFFICIAL, directory=system)

        config = self.make_config(include_wrapped_entrypoints=True)
        plans = build_plan(config, directories=[self.apps, system])

        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0].entry.path.parent, self.apps)
        self.assertEqual(plans[0].action, EntryPlan.ADOPT)
        self.assertEqual(plans[1].action, EntryPlan.SKIP)
        self.assertIn("遮蔽", plans[1].reason)

    def test_nodisplay_skipped(self) -> None:
        content = "[Desktop Entry]\nName=X\nExec=chatgpt %U\nNoDisplay=true\n"
        self.write_entry("chatgpt.desktop", content)
        plans = build_plan(self.make_config(), directories=[self.apps])
        self.assertEqual(plans[0].action, EntryPlan.SKIP)

    def test_already_managed_detected(self) -> None:
        content = (
            "[Desktop Entry]\nName=X\n"
            "Exec=/tmp/run.sh --launch-command \"chatgpt %U\"\n"
        )
        self.write_entry("chatgpt.desktop", content)
        plans = build_plan(self.make_config(), directories=[self.apps])
        self.assertEqual(plans[0].action, EntryPlan.ALREADY)

    def test_missing_directory_is_harmless(self) -> None:
        plans = build_plan(self.make_config(), directories=[self.root / "nope"])
        self.assertEqual(plans, [])


class TestAdoptAndRestore(TempDirs):
    def test_adopt_writes_exec_and_creates_backup(self) -> None:
        # 用一个真实存在的可执行文件，避免被 program_exists 挡掉。
        fake = self.root / "bin" / "chatgpt"
        fake.parent.mkdir()
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)

        content = OFFICIAL.replace("Exec=chatgpt %U", f"Exec={fake} %U")
        path = self.write_entry("chatgpt.desktop", content)
        original = path.read_bytes()

        config = self.make_config(app_command=str(fake), launcher_command=str(self.root / "run.sh"))
        plans = build_plan(config, directories=[self.apps])
        self.assertEqual(plans[0].action, EntryPlan.ADOPT)

        backup = adopt(config, plans[0])

        self.assertTrue(backup.exists())
        self.assertEqual(backup.name, path.name + BACKUP_SUFFIX)
        # 备份必须与原始内容逐字节一致。
        self.assertEqual(backup.read_bytes(), original)

        entry = parse_desktop_entry(path)
        self.assertTrue(entry.is_managed)
        self.assertIn(str(self.root / "run.sh"), entry.exec_line)
        self.assertIn("--launch-command", entry.exec_line)
        # 其它字段与顺序不变。
        self.assertEqual(entry.name, "ChatGPT")
        self.assertEqual(entry.startup_wm_class, "Chatgpt")

    def test_adopt_preserves_original_command(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)

        content = f"[Desktop Entry]\nName=C\nExec={fake} --profile-directory=Default %U\n"
        path = self.write_entry("chatgpt.desktop", content)
        config = self.make_config(
            app_command=str(fake), launcher_command=str(self.root / "run.sh")
        )
        plan = build_plan(config, directories=[self.apps])[0]
        adopt(config, plan)

        entry = parse_desktop_entry(path)
        # 原命令整体透传，参数与占位符都不能丢。
        self.assertIn("--profile-directory=Default %U", entry.exec_line)

    def test_adopt_is_idempotent(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        path = self.write_entry("chatgpt.desktop", f"[Desktop Entry]\nName=C\nExec={fake} %U\n")

        config = self.make_config(
            app_command=str(fake), launcher_command=str(self.root / "run.sh")
        )
        plan = build_plan(config, directories=[self.apps])[0]
        adopt(config, plan)
        first = path.read_bytes()

        # 再次规划时应识别为已接管，不再改动。
        again = build_plan(config, directories=[self.apps])
        self.assertEqual(again[0].action, EntryPlan.ALREADY)
        self.assertEqual(path.read_bytes(), first)

    def test_backup_not_overwritten_on_second_adopt(self) -> None:
        """重复接管不能覆盖备份，否则回滚会退回到半接管状态。"""

        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        path = self.write_entry("chatgpt.desktop", f"[Desktop Entry]\nName=C\nExec={fake} %U\n")
        original = path.read_bytes()

        config = self.make_config(
            app_command=str(fake), launcher_command=str(self.root / "run.sh")
        )
        plan = build_plan(config, directories=[self.apps])[0]
        backup = adopt(config, plan)
        adopt(config, plan)

        self.assertEqual(backup.read_bytes(), original)

    def test_restore_returns_original(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        path = self.write_entry("chatgpt.desktop", f"[Desktop Entry]\nName=C\nExec={fake} %U\n")
        original = path.read_bytes()

        config = self.make_config(
            app_command=str(fake), launcher_command=str(self.root / "run.sh")
        )
        adopt(config, build_plan(config, directories=[self.apps])[0])
        self.assertNotEqual(path.read_bytes(), original)

        restored = restore(config, directories=[self.apps])
        self.assertEqual(restored, [path])
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(backup_path(path).exists())

    def test_restore_without_backup_is_noop(self) -> None:
        self.write_entry("chatgpt.desktop", OFFICIAL)
        config = self.make_config()
        self.assertEqual(restore(config, directories=[self.apps]), [])

    def test_rewrite_preserves_crlf(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        content = f"[Desktop Entry]\r\nName=C\r\nExec={fake} %U\r\n"
        path = self.apps / "chatgpt.desktop"
        path.write_bytes(content.encode("utf-8"))

        config = self.make_config(
            app_command=str(fake), launcher_command=str(self.root / "run.sh")
        )
        plan = build_plan(config, directories=[self.apps])[0]
        adopt(config, plan)

        raw = path.read_bytes().decode("utf-8")
        self.assertIn("\r\n", raw)
        # CRLF 不能被混成 CRLF+LF。
        self.assertNotIn("\r\r\n", raw)

    def test_quoted_launcher_path_when_it_has_spaces(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        path = self.write_entry("chatgpt.desktop", f"[Desktop Entry]\nName=C\nExec={fake} %U\n")

        config = self.make_config(
            app_command=str(fake),
            launcher_command=str(self.root / "my tools" / "run.sh"),
        )
        adopt(config, build_plan(config, directories=[self.apps])[0])

        entry = parse_desktop_entry(path)
        self.assertTrue(entry.exec_line.startswith('"'))
        self.assertIn("my tools", entry.exec_line)


class TestVerify(TempDirs):
    def test_clean_state_has_no_problems(self) -> None:
        self.write_entry("chatgpt.desktop", wrapped_entry(self.write_wrapper()))
        config = self.make_config()
        self.assertEqual(verify(config, directories=[self.apps]), [])

    def test_managed_without_backup_is_reported(self) -> None:
        content = "[Desktop Entry]\nName=X\nExec=/x/run.sh --launch-command chatgpt\n"
        self.write_entry("chatgpt.desktop", content)
        problems = verify(self.make_config(), directories=[self.apps])
        self.assertTrue(any("缺少备份" in p for p in problems))

    def test_stale_backup_is_reported(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        path = self.write_entry("chatgpt.desktop", f"[Desktop Entry]\nName=C\nExec={fake} %U\n")
        # 模拟上次接管后手工还原了文件但漏删备份。
        backup_path(path).write_bytes(path.read_bytes())

        config = self.make_config(app_command=str(fake))
        problems = verify(config, directories=[self.apps])
        self.assertTrue(any("遗留备份" in p for p in problems))


class TestSha256(TempDirs):
    def test_sha256_matches_known_value(self) -> None:
        path = self.root / "x.txt"
        path.write_bytes(b"abc")
        # abc 的 SHA-256 是公开固定值，用来验证没写错算法。
        self.assertEqual(
            sha256_of(path),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )


class TestManagedDetection(TempDirs):
    """已接管标记的两种写法都要能识别。"""

    def _managed_entry(self, exec_line: str) -> Path:
        content = f"[Desktop Entry]\nName=C\nExec={exec_line}\nType=Application\n"
        return self.write_entry("chatgpt.desktop", content)

    def test_quoted_marker_is_managed(self) -> None:
        path = self._managed_entry('/x/run.sh --launch-command "chatgpt %U"')
        entry = parse_desktop_entry(path)
        self.assertTrue(entry.is_managed)

    def test_bare_marker_is_managed(self) -> None:
        path = self._managed_entry("/x/run.sh --launch-command chatgpt")
        entry = parse_desktop_entry(path)
        self.assertTrue(entry.is_managed)

    def test_bare_marker_still_matches_target(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        # 无引号写法：内部原始命令是裸命令名，仍需与 app_command 对上。
        self._managed_entry(f"/x/run.sh --launch-command {fake.name}")
        config = self.make_config(app_command=str(fake))
        plans = build_plan(config, directories=[self.apps])
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].action, EntryPlan.ALREADY)


class TestAdoptRestoreRoundTrip(TempDirs):
    """接管→回滚必须逐字节回到原始内容。"""

    def test_round_trip_is_byte_exact(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        original = f"[Desktop Entry]\nName=ChatGPT\nExec={fake} %U\nIcon=chatgpt\n"
        path = self.write_entry("chatgpt.desktop", original)
        before = path.read_bytes()

        config = self.make_config(
            app_command=str(fake), launcher_command=str(self.root / "run.sh")
        )
        adopt(config, build_plan(config, directories=[self.apps])[0])
        self.assertNotEqual(path.read_bytes(), before)
        self.assertTrue(parse_desktop_entry(path).is_managed)

        restore(config, directories=[self.apps])
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(backup_path(path).exists())

    def test_adopt_then_replan_reports_already(self) -> None:
        fake = self.root / "chatgpt"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        self.write_entry(
            "chatgpt.desktop",
            f"[Desktop Entry]\nName=C\nExec={fake} %U\nType=Application\n",
        )
        config = self.make_config(
            app_command=str(fake), launcher_command=str(self.root / "run.sh")
        )
        adopt(config, build_plan(config, directories=[self.apps])[0])

        plans = build_plan(config, directories=[self.apps])
        self.assertEqual([p.action for p in plans], [EntryPlan.ALREADY])
        # 二次运行时 verify 不应再报问题。
        self.assertEqual(verify(config, directories=[self.apps]), [])


if __name__ == "__main__":
    unittest.main()
