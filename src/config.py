"""配置加载与校验模块。

字段名与上游 ``launcher.json`` 保持兼容：同名同义字段直接沿用，
Windows 专属字段（``AppLaunch`` / ``ScanAllLocalDrives``）被替换或移除，
Linux 侧新增少量扩展字段。
"""

from __future__ import annotations

import json
import math
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .geometry import ScreenFrame, validate_frames

# 合法的嵌入渲染模式。auto 表示按能力探测顺序降级。
EMBED_MODES = ("auto", "composite", "shm", "fade")

# 项目根目录：src/ 的上一级。源码形态下就是仓库根；deb 安装后为
# /usr/lib/dragon-codex-boot。具体路径策略见 paths.py。
PROJECT_ROOT = paths.PROJECT_ROOT

DEFAULT_CONFIG_NAME = "launcher.json"

# 示例模板路径。用函数而非模块级常量：模板位置取决于运行形态与安装前缀，
# 必须在实际使用时求值，否则解包到非标准前缀运行会取到错误位置。
def example_config_path() -> Path:
    """当前运行形态下的示例配置模板路径。"""

    return paths.example_config_path()


# 兼容既有调用方（测试与文档按常量名引用）。
EXAMPLE_CONFIG_PATH = paths.example_config_path()


class ConfigError(Exception):
    """配置不合法时抛出，消息面向用户的中文说明。"""


@dataclass
class Config:
    """启动器配置。

    默认值有意与上游示例保持一致（14 秒视频、1920×1080 播放窗口），
    这样同一段 ``startup.mp4`` 与同一份关键帧可以直接复用。
    """

    display_name: str = "Dragon Codex Boot"
    video: str = "media/startup.mp4"

    # —— Linux 客户端定位 ——
    # 启动命令：真实客户端可执行文件的绝对路径。
    app_command: str = "/usr/bin/chatgpt"
    process_names: List[str] = field(default_factory=lambda: ["chatgpt", "ChatGPT"])
    process_path_contains: str = "/chatgpt/"
    target_wm_class: str = "Chatgpt"

    # —— 时间轴（单位：秒）——
    hold_at: float = 11.3
    transition_start: float = 12.7
    transition_end: float = 13.65
    max_wait_seconds: float = 60.0
    volume: float = 1.0

    # —— 播放窗口尺寸（物理像素）——
    player_width: int = 1920
    player_height: int = 1080
    match_client_to_player: bool = True

    # —— Linux 扩展 ——
    embed_mode: str = "auto"
    embed_fps: int = 30

    # —— 启动入口集成 ——
    auto_replace_entrypoints: bool = False
    # 本工具自己的启动命令；留空则推断为项目根下的 run.sh。
    launcher_command: str = ""
    # 是否接管已被第三方脚本包装的 .desktop（例如 chatgpt-codexhost-launcher）。
    include_wrapped_entrypoints: bool = False
    exclude_entrypoints: List[str] = field(default_factory=list)
    # 桌面入口透传的完整命令（含参数）。非空时优先于 ``app_command``，
    # 因为它可能带 ``--profile-directory`` 之类本工具不该理解的参数。
    launch_command_parts: List[str] = field(default_factory=list)

    screen_frames: List[ScreenFrame] = field(default_factory=list)

    # —— 运行时元信息 ——
    config_path: Optional[Path] = None
    base_dir: Path = PROJECT_ROOT

    # ------------------------------------------------------------------
    # 路径解析
    # ------------------------------------------------------------------
    def resolve_video(self) -> Path:
        """把 ``video`` 解析为绝对路径。

        相对路径按以下顺序查找，取第一个存在的：

        1. 配置所在目录（源码形态的标准布局，也支持用户放自己的视频）；
        2. 内置媒体目录（deb 安装形态下视频在 ``/usr/share``，
           而配置文件在 ``~/.config``，两者不在同一目录）。

        都不存在时返回第一个候选，让调用方在报错信息里显示预期路径。
        """
        candidate = Path(self.video).expanduser()
        if candidate.is_absolute():
            return candidate

        first = (self.base_dir / candidate).resolve()
        if first.exists():
            return first

        fallback = (paths.media_root() / candidate).resolve()
        if fallback.exists() and fallback != first:
            return fallback
        return first

    def resolve_app_command(self) -> str:
        """解析客户端启动命令为绝对路径；接受命令名（走 PATH 查找）。"""
        command = self.app_command
        if os.path.isabs(command):
            return command
        found = shutil.which(command)
        return found or command

    def resolve_launcher_command(self) -> str:
        """本工具自己的启动命令，用于写入 .desktop 的 Exec。

        未显式配置时按运行形态推断：

        - 源码形态：项目根目录的 ``run.sh``（``scripts/install.sh`` 生成的标准入口）；
        - 安装形态：``/usr/bin/dragon-codex-boot``（deb 包安装的命令）。
        """

        command = self.launcher_command.strip()
        if not command:
            if paths.is_installed_layout():
                installed = Path("/usr/bin") / paths.APP_ID
                if installed.exists():
                    return str(installed)
            return str(self.base_dir / "run.sh")
        candidate = Path(command).expanduser()
        if candidate.is_absolute():
            return str(candidate)
        # 相对路径以配置目录为基准，避免受调用时的工作目录影响。
        return str((self.base_dir / candidate).resolve())


# ----------------------------------------------------------------------
# 字段名映射：兼容上游 PascalCase 与本项目 snake_case
# ----------------------------------------------------------------------
_FIELD_ALIASES: Dict[str, str] = {
    "AutoReplaceEntrypoints": "auto_replace_entrypoints",
    "LauncherCommand": "launcher_command",
    "Video": "video",
    "AppCommand": "app_command",
    "ProcessNames": "process_names",
    "ProcessPathContains": "process_path_contains",
    "TargetWMClass": "target_wm_class",
    "HoldAt": "hold_at",
    "TransitionStart": "transition_start",
    "TransitionEnd": "transition_end",
    "MaxWaitSeconds": "max_wait_seconds",
    "Volume": "volume",
    "PlayerWidth": "player_width",
    "PlayerHeight": "player_height",
    "MatchClientToPlayer": "match_client_to_player",
    "EmbedMode": "embed_mode",
    "EmbedFps": "embed_fps",
    "AutoReplaceEntrypoints": "auto_replace_entrypoints",
    "IncludeWrappedEntrypoints": "include_wrapped_entrypoints",
    "ExcludeEntrypoints": "exclude_entrypoints",
    "ScreenFrames": "screen_frames",
}

# 上游存在但 Linux 版不再支持的字段，读取时给出明确提示而不是静默忽略。
_UNSUPPORTED_FIELDS = {
    "AppLaunch": "Windows 专用启动标识（shell:AppsFolder），请改用 AppCommand",
    "ScanAllLocalDrives": "Linux 没有盘符概念，该字段已移除",
}


def _as_float(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是数值，实际为 {value!r}") from exc
    if not math.isfinite(result):
        raise ConfigError(f"{name} 必须是有限数值")
    return result


def _as_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数，实际为 {value!r}") from exc


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ConfigError(f"{name} 必须是 true 或 false，实际为 {value!r}")


def _as_str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} 必须是非空字符串")
    return value


def _as_str_list(value: Any, name: str) -> List[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{name} 必须是字符串数组")
    return list(value)


def _parse_frames(value: Any) -> List[ScreenFrame]:
    if not isinstance(value, list):
        raise ConfigError("ScreenFrames 必须是对象数组")
    frames: List[ScreenFrame] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(f"ScreenFrames[{index}] 必须是对象")
        label = f"ScreenFrames[{index}]"
        frames.append(
            ScreenFrame(
                # 上游字段名为 PascalCase，这里两种写法都接受。
                time=_as_float(item.get("Time", item.get("time")), f"{label}.Time"),
                x=_as_float(item.get("X", item.get("x")), f"{label}.X"),
                y=_as_float(item.get("Y", item.get("y")), f"{label}.Y"),
                width=_as_float(
                    item.get("Width", item.get("width")), f"{label}.Width"
                ),
                height=_as_float(
                    item.get("Height", item.get("height")), f"{label}.Height"
                ),
            )
        )
    return frames


def from_dict(data: Dict[str, Any], base_dir: Path) -> Config:
    """由字典构造配置，未知字段会被忽略但记录到警告列表。"""
    if not isinstance(data, dict):
        raise ConfigError("配置根节点必须是 JSON 对象")

    config = Config(base_dir=base_dir)

    for key, hint in _UNSUPPORTED_FIELDS.items():
        if key in data:
            raise ConfigError(f"字段 {key} 不受支持：{hint}")

    for raw_key, raw_value in data.items():
        key = _FIELD_ALIASES.get(raw_key, raw_key)
        if key == "screen_frames":
            config.screen_frames = _parse_frames(raw_value)
        elif key == "display_name":
            config.display_name = _as_str(raw_value, raw_key)
        elif key == "video":
            config.video = _as_str(raw_value, raw_key)
        elif key == "app_command":
            config.app_command = _as_str(raw_value, raw_key)
        elif key == "process_names":
            config.process_names = _as_str_list(raw_value, raw_key)
        elif key == "process_path_contains":
            config.process_path_contains = _as_str(raw_value, raw_key)
        elif key == "target_wm_class":
            config.target_wm_class = _as_str(raw_value, raw_key)
        elif key == "hold_at":
            config.hold_at = _as_float(raw_value, raw_key)
        elif key == "transition_start":
            config.transition_start = _as_float(raw_value, raw_key)
        elif key == "transition_end":
            config.transition_end = _as_float(raw_value, raw_key)
        elif key == "max_wait_seconds":
            config.max_wait_seconds = _as_float(raw_value, raw_key)
        elif key == "auto_replace_entrypoints":
            config.auto_replace_entrypoints = _as_bool(raw_value, raw_key)
        elif key == "launcher_command":
            config.launcher_command = _as_str(raw_value, raw_key)
        elif key == "volume":
            config.volume = _as_float(raw_value, raw_key)
        elif key == "player_width":
            config.player_width = _as_int(raw_value, raw_key)
        elif key == "player_height":
            config.player_height = _as_int(raw_value, raw_key)
        elif key == "match_client_to_player":
            config.match_client_to_player = _as_bool(raw_value, raw_key)
        elif key == "embed_mode":
            config.embed_mode = _as_str(raw_value, raw_key)
        elif key == "embed_fps":
            config.embed_fps = _as_int(raw_value, raw_key)
        elif key == "auto_replace_entrypoints":
            config.auto_replace_entrypoints = _as_bool(raw_value, raw_key)
        elif key == "include_wrapped_entrypoints":
            config.include_wrapped_entrypoints = _as_bool(raw_value, raw_key)
        elif key == "exclude_entrypoints":
            config.exclude_entrypoints = _as_str_list(raw_value, raw_key)
        # 其余未知字段直接忽略，保持对上游新增字段的向前兼容。

    return config


def validate(config: Config) -> List[str]:
    """校验配置，返回错误列表（空表示通过）。

    校验项对应上游 ``Entry.ValidateConfig``，并补充 Linux 侧约束。
    """
    errors: List[str] = []

    # 时间关系：0 < HoldAt < TransitionStart < TransitionEnd
    if config.hold_at <= 0:
        errors.append("HoldAt 必须大于 0")
    if config.hold_at >= config.transition_start:
        errors.append("必须满足 HoldAt < TransitionStart")
    if config.transition_start >= config.transition_end:
        errors.append("必须满足 TransitionStart < TransitionEnd")
    if config.max_wait_seconds <= 0:
        errors.append("MaxWaitSeconds 必须大于 0")

    # 音量：0~1
    if not 0.0 <= config.volume <= 1.0:
        errors.append("Volume 必须在 0~1 之间")

    # 播放窗口尺寸
    if config.player_width <= 0 or config.player_height <= 0:
        errors.append("PlayerWidth/PlayerHeight 必须为正整数")

    # 嵌入模式与帧率
    if config.embed_mode not in EMBED_MODES:
        errors.append(f"EmbedMode 必须是 {'/'.join(EMBED_MODES)} 之一")
    if config.embed_fps <= 0 or config.embed_fps > 120:
        errors.append("EmbedFps 必须在 1~120 之间")

    # 关键帧
    errors.extend(validate_frames(config.screen_frames))

    return errors


def load(path: Optional[Path] = None) -> Config:
    """加载配置文件；未指定时按运行形态选择默认位置。

    源码形态用项目根目录的 ``launcher.json``；deb 安装形态用
    ``~/.config/dragon-codex-boot/launcher.json``，具体见 ``paths.py``。

    若配置文件不存在且目标是默认位置，则从示例模板复制一份，
    行为与上游「首次运行复制配置模板」一致。
    """
    config_path = Path(path).expanduser() if path else paths.default_config_path()
    config_path = config_path.resolve()

    if not config_path.exists():
        # 只有"未显式指定路径"才允许按模板生成；显式指定却不存在时报错，
        # 避免用户以为读的是自己的配置、实际读了模板副本。
        template = example_config_path() if path is None else None
        if template is not None and template.exists():
            config_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(template, config_path)
        else:
            raise ConfigError(f"配置文件不存在：{config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{config_path}（{exc}）") from exc

    config = from_dict(raw, base_dir=config_path.parent)
    config.config_path = config_path

    errors = validate(config)
    if errors:
        raise ConfigError("配置校验失败：\n  - " + "\n  - ".join(errors))

    return config


def to_dict(config: Config) -> Dict[str, Any]:
    """把配置序列化回上游兼容的 JSON 结构，供标定工具回写。"""
    return {
        "DisplayName": config.display_name,
        "Video": config.video,
        "AutoReplaceEntrypoints": config.auto_replace_entrypoints,
        "LauncherCommand": config.launcher_command,
        "ProcessNames": list(config.process_names),
        "ProcessPathContains": config.process_path_contains,
        "TargetWMClass": config.target_wm_class,
        "HoldAt": round(config.hold_at, 3),
        "TransitionStart": round(config.transition_start, 3),
        "TransitionEnd": round(config.transition_end, 3),
        "MaxWaitSeconds": config.max_wait_seconds,
        "Volume": config.volume,
        "PlayerWidth": config.player_width,
        "PlayerHeight": config.player_height,
        "MatchClientToPlayer": config.match_client_to_player,
        "EmbedMode": config.embed_mode,
        "EmbedFps": config.embed_fps,
        "AutoReplaceEntrypoints": config.auto_replace_entrypoints,
        "IncludeWrappedEntrypoints": config.include_wrapped_entrypoints,
        "ExcludeEntrypoints": list(config.exclude_entrypoints),
        "ScreenFrames": [
            {
                "Time": round(frame.time, 3),
                "X": round(frame.x, 3),
                "Y": round(frame.y, 3),
                "Width": round(frame.width, 3),
                "Height": round(frame.height, 3),
            }
            for frame in config.screen_frames
        ],
    }
