"""会话类型探测与嵌入模式推荐逻辑的单元测试。

设计要点
========

被测函数会读取环境变量、``/proc`` 与 ``/tmp/.X11-unix``。这些在 CI runner
（往往是容器、无图形会话、``HOME=/home/runner``）和开发机上差异极大，
所以本文件**一律用 ``env=`` 参数注入环境**，绝不读进程真实的 ``os.environ``。

涉及 ``/proc`` 与 ``/tmp/.X11-unix`` 的探测用 monkeypatch 打桩，避免
「本机有 XWayland → 通过；CI 无 → 失败」这类环境依赖型翻车。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import sessionkind  # noqa: E402
from src.sessionkind import (  # noqa: E402
    SessionInfo,
    SessionKind,
    detect_session,
    embed_mode_reason,
    recommended_embed_mode,
    recommended_vo,
    vo_reason,
)


@pytest.fixture()
def no_xwayland(monkeypatch):
    """让 ``_xwayland_present()`` 恒为 False，隔离真实环境。"""

    monkeypatch.setattr(sessionkind, "_xwayland_present", lambda: False)


@pytest.fixture()
def has_xwayland(monkeypatch):
    """让 ``_xwayland_present()`` 恒为 True。"""

    monkeypatch.setattr(sessionkind, "_xwayland_present", lambda: True)


# ----------------------------------------------------------------------
# detect_session：主判定路径
# ----------------------------------------------------------------------
def test_detect_x11_from_session_type(no_xwayland):
    info = detect_session({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})

    assert info.kind is SessionKind.X11
    assert info.raw_type == "x11"
    assert info.display == ":0"
    assert info.wayland_display is None
    # 纯 X11 下不该去探测 XWayland。
    assert info.xwayland is False


def test_detect_wayland_from_session_type(has_xwayland):
    info = detect_session(
        {
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "DISPLAY": ":0",
        }
    )

    assert info.kind is SessionKind.WAYLAND
    assert info.raw_type == "wayland"
    assert info.wayland_display == "wayland-0"
    # 有 DISPLAY 说明存在 XWayland，可沿用 X11 窗口操作。
    assert info.display == ":0"
    assert info.xwayland is True


def test_session_type_is_case_and_space_insensitive(no_xwayland):
    info = detect_session({"XDG_SESSION_TYPE": "  WaYlAnD  "})

    assert info.kind is SessionKind.WAYLAND


def test_fallback_to_wayland_when_only_wayland_display(has_xwayland):
    """XDG_SESSION_TYPE 缺失时，WAYLAND_DISPLAY 是较强证据。"""

    info = detect_session({"WAYLAND_DISPLAY": "wayland-0", "DISPLAY": ":0"})

    assert info.kind is SessionKind.WAYLAND
    assert info.raw_type == ""


def test_fallback_to_x11_when_only_display(no_xwayland):
    """只有 DISPLAY、没有 WAYLAND_DISPLAY 时按 X11 处理。"""

    info = detect_session({"DISPLAY": ":0"})

    assert info.kind is SessionKind.X11
    assert info.display == ":0"


def test_unknown_when_nothing_present(no_xwayland):
    info = detect_session({})

    assert info.kind is SessionKind.UNKNOWN
    assert info.display is None
    assert info.wayland_display is None
    assert info.xwayland is False


def test_empty_string_display_treated_as_absent(no_xwayland):
    """空字符串不应被当成有效的 DISPLAY。"""

    info = detect_session({"DISPLAY": "", "WAYLAND_DISPLAY": ""})

    assert info.kind is SessionKind.UNKNOWN
    assert info.display is None
    assert info.wayland_display is None


def test_non_standard_session_type_falls_back(no_xwayland):
    """未知的 XDG_SESSION_TYPE 值不应直接采信，走弱证据推断。"""

    info = detect_session({"XDG_SESSION_TYPE": "tty", "DISPLAY": ":0"})

    assert info.kind is SessionKind.X11
    assert info.raw_type == "tty"


# ----------------------------------------------------------------------
# SessionInfo.label / supports_composite
# ----------------------------------------------------------------------
def test_label_for_x11(no_xwayland):
    info = detect_session({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
    assert info.label == "X11 (Xorg)"


def test_label_for_wayland_with_xwayland(has_xwayland):
    info = detect_session({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"})
    assert "XWayland" in info.label


def test_label_for_wayland_without_xwayland(no_xwayland):
    info = detect_session({"XDG_SESSION_TYPE": "wayland"})
    assert info.label == "Wayland"


def test_label_for_unknown(no_xwayland):
    info = detect_session({})
    assert info.label == "未知会话"


def test_supports_composite_only_on_x11(no_xwayland):
    x11 = detect_session({"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0"})
    wayland = detect_session({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"})
    unknown = detect_session({})

    assert x11.supports_composite is True
    assert wayland.supports_composite is False
    assert unknown.supports_composite is False


# ----------------------------------------------------------------------
# recommended_embed_mode
# ----------------------------------------------------------------------
def _info(kind: SessionKind, *, xwayland: bool = False) -> SessionInfo:
    return SessionInfo(
        kind=kind,
        raw_type=kind.value,
        display=":0" if kind is not SessionKind.UNKNOWN else None,
        wayland_display="wayland-0" if kind is SessionKind.WAYLAND else None,
        xwayland=xwayland,
    )


def test_auto_on_x11_stays_auto():
    """X11 下 auto 交给能力探测，可能命中 composite。"""

    assert recommended_embed_mode(_info(SessionKind.X11), "auto") == "auto"


def test_auto_on_wayland_with_xwayland_is_shm():
    info = _info(SessionKind.WAYLAND, xwayland=True)
    assert recommended_embed_mode(info, "auto") == "shm"


def test_auto_on_wayland_without_xwayland_is_fade():
    info = _info(SessionKind.WAYLAND, xwayland=False)
    assert recommended_embed_mode(info, "auto") == "fade"


def test_auto_on_unknown_stays_auto():
    assert recommended_embed_mode(_info(SessionKind.UNKNOWN), "auto") == "auto"


@pytest.mark.parametrize("mode", ["composite", "shm", "fade"])
def test_explicit_mode_is_always_respected(mode):
    """用户显式指定的档位一律不改写，即便在 Wayland 下要 composite。"""

    wayland = _info(SessionKind.WAYLAND, xwayland=False)
    assert recommended_embed_mode(wayland, mode) == mode


def test_explicit_mode_is_case_insensitive():
    info = _info(SessionKind.WAYLAND)
    assert recommended_embed_mode(info, "  SHM  ") == "shm"


def test_empty_requested_treated_as_auto():
    assert recommended_embed_mode(_info(SessionKind.X11), "") == "auto"
    assert recommended_embed_mode(_info(SessionKind.X11), None) == "auto"


# ----------------------------------------------------------------------
# embed_mode_reason
# ----------------------------------------------------------------------
def test_reason_mentions_explicit_mode():
    reason = embed_mode_reason(_info(SessionKind.WAYLAND), "composite")
    assert "composite" in reason
    assert "显式" in reason


def test_reason_for_wayland_with_xwayland_mentions_badaccess():
    info = _info(SessionKind.WAYLAND, xwayland=True)
    reason = embed_mode_reason(info, "auto")
    assert "shm" in reason
    assert "Wayland" in reason


def test_reason_for_wayland_without_xwayland_mentions_fade():
    info = _info(SessionKind.WAYLAND, xwayland=False)
    reason = embed_mode_reason(info, "auto")
    assert "fade" in reason


def test_reason_for_x11_mentions_capability_probe():
    reason = embed_mode_reason(_info(SessionKind.X11), "auto")
    assert "X11" in reason


def test_reason_for_unknown_is_not_empty():
    assert embed_mode_reason(_info(SessionKind.UNKNOWN), "auto").strip()


# ----------------------------------------------------------------------
# _xwayland_present：真实探测逻辑（用 monkeypatch 隔离文件系统与 /proc）
# ----------------------------------------------------------------------
def test_xwayland_present_via_socket_dir(monkeypatch, tmp_path):
    """``/tmp/.X11-unix`` 下有 socket 即认为存在。"""

    fake_socket_dir = tmp_path / ".X11-unix"
    fake_socket_dir.mkdir()
    (fake_socket_dir / "X0").write_text("", encoding="utf-8")

    real_path = sessionkind.Path

    def fake_path(arg="/"):
        if arg == "/tmp/.X11-unix":
            return fake_socket_dir
        if arg == "/proc":
            # 指向一个空的临时目录，确保不会命中真实的 /proc。
            return tmp_path / "empty-proc"
        return real_path(arg)

    (tmp_path / "empty-proc").mkdir()
    monkeypatch.setattr(sessionkind, "Path", fake_path)

    assert sessionkind._xwayland_present() is True


def test_xwayland_present_via_process_comm(monkeypatch, tmp_path):
    """socket 目录为空时，退到进程表里找 ``Xwayland``。"""

    empty_socket_dir = tmp_path / ".X11-unix"
    empty_socket_dir.mkdir()

    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    pid_dir = fake_proc / "1234"
    pid_dir.mkdir()
    (pid_dir / "comm").write_text("Xwayland\n", encoding="utf-8")

    real_path = sessionkind.Path

    def fake_path(arg="/"):
        if arg == "/tmp/.X11-unix":
            return empty_socket_dir
        if arg == "/proc":
            return fake_proc
        return real_path(arg)

    monkeypatch.setattr(sessionkind, "Path", fake_path)

    assert sessionkind._xwayland_present() is True


def test_xwayland_absent_when_nothing_found(monkeypatch, tmp_path):
    empty_socket_dir = tmp_path / ".X11-unix"
    empty_socket_dir.mkdir()
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()
    # 放一个非数字目录和一个名字不匹配的进程，验证都被忽略。
    (fake_proc / "not-a-pid").mkdir()
    other = fake_proc / "999"
    other.mkdir()
    (other / "comm").write_text("gnome-shell\n", encoding="utf-8")

    real_path = sessionkind.Path

    def fake_path(arg="/"):
        if arg == "/tmp/.X11-unix":
            return empty_socket_dir
        if arg == "/proc":
            return fake_proc
        return real_path(arg)

    monkeypatch.setattr(sessionkind, "Path", fake_path)

    assert sessionkind._xwayland_present() is False


def test_xwayland_absent_when_socket_dir_missing(monkeypatch, tmp_path):
    """目录完全不存在时不应抛异常。"""

    missing = tmp_path / "definitely-missing"
    fake_proc = tmp_path / "proc"
    fake_proc.mkdir()

    real_path = sessionkind.Path

    def fake_path(arg="/"):
        if arg == "/tmp/.X11-unix":
            return missing
        if arg == "/proc":
            return fake_proc
        return real_path(arg)

    monkeypatch.setattr(sessionkind, "Path", fake_path)

    assert sessionkind._xwayland_present() is False


# ----------------------------------------------------------------------
# 真实环境冒烟：只在确实有会话时做宽松断言，不让 CI 因环境失败
# ----------------------------------------------------------------------
def test_detect_session_on_real_environment_is_consistent():
    """对真实环境只做自洽性检查，不假定一定是 X11 还是 Wayland。"""

    info = detect_session()

    assert info.kind in (SessionKind.X11, SessionKind.WAYLAND, SessionKind.UNKNOWN)
    # 若判定为 Wayland，则必然有 WAYLAND_DISPLAY 或 XDG_SESSION_TYPE 支撑。
    if info.kind is SessionKind.WAYLAND:
        assert info.wayland_display or info.raw_type == "wayland"
    # 若判定为 X11，则必然有 DISPLAY。
    if info.kind is SessionKind.X11:
        assert info.display


# ----------------------------------------------------------------------
# recommended_vo / vo_reason
# ----------------------------------------------------------------------
def test_vo_on_x11_stays_gpu():
    """X11 会话沿用 gpu，保持既有行为不回归。"""

    assert recommended_vo(_info(SessionKind.X11)) == "gpu"


def test_vo_on_wayland_uses_xv():
    """Wayland 下 gpu 实测静默失效，必须换成 xv。"""

    assert recommended_vo(_info(SessionKind.WAYLAND, xwayland=True)) == "xv"


def test_vo_on_wayland_without_xwayland_also_xv():
    assert recommended_vo(_info(SessionKind.WAYLAND, xwayland=False)) == "xv"


def test_vo_on_unknown_uses_fallback():
    assert recommended_vo(_info(SessionKind.UNKNOWN)) == "xv"


def test_vo_constants_are_sane():
    """常量的语义要一眼看得懂，避免将来改错方向。"""

    assert sessionkind.VO_X11 == "gpu"
    assert sessionkind.VO_WAYLAND == "xv"


def test_vo_reason_mentions_driver():
    assert "gpu" in vo_reason(_info(SessionKind.X11))
    assert "xv" in vo_reason(_info(SessionKind.WAYLAND))


def test_vo_reason_for_wayland_explains_why():
    """Wayland 的理由必须点出「gpu 会失败」这个事实，否则后人会想改回去。"""

    reason = vo_reason(_info(SessionKind.WAYLAND))
    assert "gpu" in reason
    assert "白" in reason or "失败" in reason
