"""运行时路径布局。

本项目有两种运行形态，路径解析必须同时兼容：

1. **源码运行**：项目根目录即 ``src/`` 的上一级，配置、日志、视频都在项目内。
   这是开发者与 ``scripts/install.sh`` 的默认形态。
2. **系统安装**（deb 包）：代码位于 ``/usr/lib/dragon-codex-boot``，
   默认配置模板在 ``/usr/share/dragon-codex-boot``，用户配置在
   ``~/.config/dragon-codex-boot/launcher.json``，日志在
   ``~/.local/state/dragon-codex-boot/logs/launcher.log``。

采用 XDG Base Directory 规范，避免把用户数据写进 ``/usr`` 或 ``/etc``。
判断依据是"代码目录下是否存在可写的 ``launcher.json`` 或 ``config/`` 模板"，
不依赖环境变量，以免用户手动 ``cd`` 到别处时行为漂移。
"""

from __future__ import annotations

import os
from pathlib import Path

# 代码根目录：src/ 的上一级。源码形态下是项目根，安装形态下是 /usr/lib/dragon-codex-boot。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# XDG 应用标识，用于配置与日志目录命名。
APP_ID = "dragon-codex-boot"


def _xdg(env: str, fallback: str) -> Path:
    """读取 XDG 目录变量，为空或缺失时用规范给出的默认值。"""

    value = os.environ.get(env, "").strip()
    return Path(value).expanduser() if value else Path.home() / fallback


def config_home() -> Path:
    """用户配置文件目录（``~/.config/dragon-codex-boot``）。"""

    return _xdg("XDG_CONFIG_HOME", ".config") / APP_ID


def state_home() -> Path:
    """用户运行状态目录（``~/.local/state/dragon-codex-boot``），存放日志。"""

    return _xdg("XDG_STATE_HOME", ".local/state") / APP_ID


def data_home() -> Path:
    """用户数据目录，安装形态下用于扩展媒体资源。"""

    return _xdg("XDG_DATA_HOME", ".local/share") / APP_ID


def is_installed_layout() -> bool:
    """判断当前是否为系统安装形态。

    判据：代码目录不是项目源码目录。源码形态下 ``src/`` 的上一级必然含有
    ``config/launcher.linux.example.json``；deb 安装后代码在
    ``/usr/lib/dragon-codex-boot``，模板被放到 ``/usr/share``，因此不存在。
    """

    return not (PROJECT_ROOT / "config" / "launcher.linux.example.json").exists()


def example_config_path() -> Path:
    """示例配置模板路径，按运行形态选择位置。"""

    if is_installed_layout():
        return Path("/usr/share") / APP_ID / "launcher.example.json"
    return PROJECT_ROOT / "config" / "launcher.linux.example.json"


def default_config_path() -> Path:
    """默认配置文件路径。

    源码形态沿用项目内 ``launcher.json``（与既有文档、脚本一致）；
    安装形态使用 XDG 配置目录，避免写入 ``/usr``。
    """

    if is_installed_layout():
        return config_home() / "launcher.json"
    return PROJECT_ROOT / "launcher.json"


def log_dir() -> Path:
    """日志目录。

    源码形态仍写到项目内 ``logs/``；安装形态写到 XDG 状态目录，
    因为 ``/usr/lib`` 对普通用户不可写。
    """

    if is_installed_layout():
        return state_home() / "logs"
    return PROJECT_ROOT / "logs"


def media_root() -> Path:
    """内置媒体根目录（安装形态为 ``/usr/share/dragon-codex-boot``）。"""

    if is_installed_layout():
        return Path("/usr/share") / APP_ID
    return PROJECT_ROOT
