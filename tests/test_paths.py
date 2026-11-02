"""运行时路径布局测试。

覆盖源码形态与 deb 安装形态两种布局，重点验证：
不把用户数据写进 /usr，以及 XDG 环境变量优先级。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src import paths


def test_app_id_is_stable():
    assert paths.APP_ID == "dragon-codex-boot"


def test_project_root_is_repo_root():
    # src/paths.py 的上一级应是仓库根，其中应存在 src/ 目录。
    assert (paths.PROJECT_ROOT / "src" / "paths.py").exists()


def test_xdg_config_home_respected(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.config_home() == tmp_path / "dragon-codex-boot"


def test_xdg_state_home_respected(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert paths.state_home() == tmp_path / "dragon-codex-boot"


def test_xdg_data_home_respected(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert paths.data_home() == tmp_path / "dragon-codex-boot"


def test_xdg_empty_value_falls_back_to_home(monkeypatch):
    # 按 XDG 规范，变量存在但为空等同于未设置。
    monkeypatch.setenv("XDG_CONFIG_HOME", "")
    assert paths.config_home() == Path.home() / ".config" / "dragon-codex-boot"


def test_source_layout_detected_by_example_config():
    # 源码形态：项目根下有 config/launcher.linux.example.json。
    example = paths.PROJECT_ROOT / "config" / "launcher.linux.example.json"
    assert example.exists()
    assert paths.is_installed_layout() is False


def test_installed_layout_detected_when_no_example(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    assert paths.is_installed_layout() is True


def test_source_layout_paths_stay_inside_project():
    assert paths.default_config_path() == paths.PROJECT_ROOT / "launcher.json"
    assert paths.log_dir() == paths.PROJECT_ROOT / "logs"
    assert paths.media_root() == paths.PROJECT_ROOT


def test_installed_layout_paths_use_xdg(monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    assert paths.default_config_path() == tmp_path / "cfg" / "dragon-codex-boot" / "launcher.json"
    assert paths.log_dir() == tmp_path / "state" / "dragon-codex-boot" / "logs"
    assert str(paths.media_root()) == "/usr/share/dragon-codex-boot"


def test_installed_layout_never_writes_under_usr_lib(monkeypatch, tmp_path):
    # 安装形态下代码在 /usr/lib，但配置与日志不能写在那里。
    monkeypatch.setattr(paths, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    for p in (paths.default_config_path(), paths.log_dir()):
        assert "/usr/lib" not in str(p)
