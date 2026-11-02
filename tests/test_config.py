"""配置模块单元测试：字段兼容、默认值、校验边界。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config as cfg  # noqa: E402

FRAMES_JSON = [
    {"Time": 12.65, "X": 0.123, "Y": 0.094, "Width": 0.736, "Height": 0.725},
    {"Time": 13.0, "X": 0.074, "Y": 0.061, "Width": 0.845, "Height": 0.825},
    {"Time": 13.5, "X": 0, "Y": 0, "Width": 1, "Height": 1},
]


def base_payload(**overrides):
    payload = {
        "DisplayName": "Dragon Codex Boot",
        "Video": "media/startup.mp4",
        "AppCommand": "/usr/bin/chatgpt",
        "ProcessNames": ["chatgpt"],
        "ProcessPathContains": "/chatgpt/",
        "TargetWMClass": "Chatgpt",
        "HoldAt": 11.3,
        "TransitionStart": 12.7,
        "TransitionEnd": 13.65,
        "MaxWaitSeconds": 60,
        "Volume": 1,
        "PlayerWidth": 1920,
        "PlayerHeight": 1080,
        "MatchClientToPlayer": True,
        "EmbedMode": "auto",
        "EmbedFps": 30,
        "AutoReplaceEntrypoints": False,
        "ScreenFrames": FRAMES_JSON,
    }
    payload.update(overrides)
    return payload


def test_example_config_file_loads_and_validates():
    # 仓库自带的示例配置必须能直接通过校验，这是发布前的硬性约束。
    data = json.loads(cfg.EXAMPLE_CONFIG_PATH.read_text(encoding="utf-8"))
    parsed = cfg.from_dict(data, base_dir=cfg.PROJECT_ROOT)
    assert cfg.validate(parsed) == []
    assert parsed.hold_at == pytest.approx(11.3)
    assert len(parsed.screen_frames) == 4


def test_upstream_pascal_case_fields_are_mapped():
    parsed = cfg.from_dict(base_payload(), base_dir=Path("/tmp"))
    assert parsed.display_name == "Dragon Codex Boot"
    assert parsed.player_width == 1920
    assert parsed.embed_mode == "auto"


def test_snake_case_fields_also_accepted():
    parsed = cfg.from_dict(
        {"video": "a.mp4", "hold_at": 5.0, "ScreenFrames": FRAMES_JSON},
        base_dir=Path("/tmp"),
    )
    assert parsed.video == "a.mp4"
    assert parsed.hold_at == 5.0


def test_windows_only_fields_raise_clear_error():
    with pytest.raises(cfg.ConfigError, match="AppLaunch"):
        cfg.from_dict(base_payload(AppLaunch="shell:AppsFolder\\X!App"), Path("/tmp"))
    with pytest.raises(cfg.ConfigError, match="ScanAllLocalDrives"):
        cfg.from_dict(base_payload(ScanAllLocalDrives=True), Path("/tmp"))


def test_unknown_fields_are_ignored_for_forward_compatibility():
    parsed = cfg.from_dict(base_payload(SomeFutureField=123), Path("/tmp"))
    assert parsed.player_width == 1920


@pytest.mark.parametrize(
    "overrides,keyword",
    [
        ({"HoldAt": 0}, "HoldAt 必须大于 0"),
        ({"HoldAt": 13.0, "TransitionStart": 12.7}, "HoldAt < TransitionStart"),
        ({"TransitionStart": 14.0}, "TransitionStart < TransitionEnd"),
        ({"Volume": 1.5}, "Volume 必须在 0~1"),
        ({"Volume": -0.1}, "Volume 必须在 0~1"),
        ({"PlayerWidth": 0}, "PlayerWidth/PlayerHeight"),
        ({"EmbedMode": "nope"}, "EmbedMode"),
        ({"EmbedFps": 0}, "EmbedFps"),
        ({"MaxWaitSeconds": 0}, "MaxWaitSeconds"),
    ],
)
def test_validate_rejects_bad_values(overrides, keyword):
    parsed = cfg.from_dict(base_payload(**overrides), Path("/tmp"))
    errors = cfg.validate(parsed)
    assert any(keyword in item for item in errors), errors


@pytest.mark.parametrize(
    "overrides",
    [
        {"HoldAt": "abc"},
        {"Volume": float("inf")},
        {"ProcessNames": "chatgpt"},
        {"ScreenFrames": {}},
        {"AutoReplaceEntrypoints": "yes"},
    ],
)
def test_from_dict_rejects_wrong_types(overrides):
    with pytest.raises(cfg.ConfigError):
        # inf 无法经由 JSON 传入，这里直接在字典层构造，同样必须被拒绝。
        cfg.from_dict(base_payload(**overrides), Path("/tmp"))


def test_resolve_video_relative_to_config_dir(tmp_path):
    # 配置目录旁存在视频时优先使用它（源码形态的标准布局）。
    video = tmp_path / "media" / "startup.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake")
    parsed = cfg.from_dict(base_payload(), base_dir=tmp_path)
    assert parsed.resolve_video() == video.resolve()


def test_resolve_video_falls_back_to_media_root(tmp_path, monkeypatch):
    # deb 安装形态：配置在 ~/.config，视频在 /usr/share，配置目录旁没有视频。
    share = tmp_path / "share"
    video = share / "media" / "startup.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"fake")
    monkeypatch.setattr(cfg.paths, "media_root", lambda: share)

    conf_dir = tmp_path / "config_home"
    conf_dir.mkdir()
    parsed = cfg.from_dict(base_payload(), base_dir=conf_dir)
    assert parsed.resolve_video() == video.resolve()


def test_resolve_video_returns_config_dir_when_nowhere_found(tmp_path, monkeypatch):
    # 两处都不存在时返回配置目录下的候选，便于报错信息显示预期路径。
    monkeypatch.setattr(cfg.paths, "media_root", lambda: tmp_path / "share")
    parsed = cfg.from_dict(base_payload(), base_dir=tmp_path)
    assert parsed.resolve_video() == (tmp_path / "media" / "startup.mp4").resolve()


def test_resolve_video_absolute_path_is_kept(tmp_path):
    parsed = cfg.from_dict(base_payload(Video="/abs/x.mp4"), base_dir=tmp_path)
    assert parsed.resolve_video() == Path("/abs/x.mp4")


def test_resolve_app_command_finds_binary_on_path():
    parsed = cfg.from_dict(base_payload(AppCommand="sh"), Path("/tmp"))
    assert parsed.resolve_app_command().endswith("/sh")


def test_load_copies_example_when_missing(tmp_path, monkeypatch):
    # 模拟「默认位置无配置」：把默认配置路径与示例模板都指向临时目录。
    example = tmp_path / "config" / "launcher.linux.example.json"
    example.parent.mkdir(parents=True)
    example.write_text(
        json.dumps(base_payload(), ensure_ascii=False), encoding="utf-8"
    )
    target = tmp_path / "launcher.json"
    monkeypatch.setattr(cfg.paths, "default_config_path", lambda: target)
    monkeypatch.setattr(cfg, "EXAMPLE_CONFIG_PATH", example)

    loaded = cfg.load()
    assert loaded.config_path == target.resolve()
    assert loaded.config_path.exists()


def test_load_rejects_broken_json(tmp_path):
    path = tmp_path / "launcher.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(cfg.ConfigError, match="不是合法 JSON"):
        cfg.load(path)


def test_roundtrip_to_dict_preserves_timing():
    parsed = cfg.from_dict(base_payload(), base_dir=Path("/tmp"))
    dumped = cfg.to_dict(parsed)
    assert dumped["HoldAt"] == 11.3
    assert dumped["TransitionEnd"] == 13.65
    assert dumped["ScreenFrames"][0]["Width"] == 0.736
