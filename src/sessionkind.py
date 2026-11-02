"""会话类型探测：判断当前跑在 X11 还是 Wayland 下，并据此决定嵌入策略。

为什么需要这个模块
==================

程序依赖 X11 的窗口几何与合成能力。在 Wayland 会话里，**原生 Wayland 窗口**
出于安全模型不允许第三方查询几何、抢占重定向或任意抓帧，这套能力并不存在。

但实践中有一条重要缝隙：Electron / Chrome 系的桌面应用（包括本项目默认的
目标 ``/usr/bin/chatgpt``）在 Wayland 会话下**默认仍通过 XWayland 运行**
（进程树里带 ``--ozone-platform=x``）。这类窗口对 X 服务器而言是普通 X11
客户端，因此原有的查找窗口、对齐几何、交接焦点三步完全有效。

真正受限的只有「把真实窗口画面合成进播放窗口」这一档：XWayland 客户端的
顶层窗口已被 GNOME 合成器做了 ``RedirectAutomatic``，第二个客户端再请求
``RedirectManual`` 会拿到 ``BadAccess``。此时嵌入层会自行降级到 ``shm``
（真实窗口留在屏幕上，仅做透明度交叉淡出），这属于预期行为而非故障。

模块职责因此收敛为两件事：

1. 判定会话类型（``x11`` / ``wayland``），判定失败时退化到 ``unknown``；
2. 给出该会话下推荐的 ``EmbedMode`` 与一句人类可读的解释，供日志与
   ``entries --verify`` 之类的诊断输出使用。

本模块只读环境变量与 ``/proc`` 里的进程信息，不读窗口标题、不截图，
与项目既有的隐私边界一致。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


class SessionKind(str, Enum):
    """会话类型。字符串值与 ``XDG_SESSION_TYPE`` 的取值保持一致。"""

    X11 = "x11"
    WAYLAND = "wayland"
    # 两套环境变量都缺失时的兜底值：既可能是不带图形会话的纯终端，
    # 也可能是非标准的嵌入式场景。
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class SessionInfo:
    """一次会话探测的结果。"""

    kind: SessionKind
    # ``XDG_SESSION_TYPE`` 的原始值，便于诊断时如实呈现。
    raw_type: str
    # ``DISPLAY``，X11 下必填；Wayland 下若存在说明有 XWayland 可用。
    display: Optional[str]
    # ``WAYLAND_DISPLAY``，Wayland 下必填。
    wayland_display: Optional[str]
    # Wayland 会话里是否存在 XWayland（决定能否沿用 X11 那套窗口操作）。
    xwayland: bool

    @property
    def label(self) -> str:
        """供日志与界面显示的中文描述。"""
        if self.kind is SessionKind.X11:
            return "X11 (Xorg)"
        if self.kind is SessionKind.WAYLAND:
            return "Wayland（经 XWayland）" if self.xwayland else "Wayland"
        return "未知会话"

    @property
    def supports_composite(self) -> bool:
        """能否尝试 ``composite`` 嵌入档。

        纯 X11 会话可以（最终仍要看 ``BadAccess`` 实测结果）；Wayland 会话
        下即使目标窗口在 XWayland 里，顶层窗口也已被合成器接管重定向，
        不必再徒劳尝试。
        """
        return self.kind is SessionKind.X11


def _xwayland_present() -> bool:
    """判断当前 Wayland 会话是否存在 XWayland 服务端。

    两条依据任一条成立即认为存在：

    1. ``/tmp/.X11-unix/`` 下有 X socket —— XWayland 会在这里注册；
    2. 进程表里能找到 ``Xwayland``。

    不直接连接 X 服务器：本函数会被用来在「决定是否连接」之前做判断，
    提前连接会在 XWayland 缺失时产生一次无谓的错误。
    """

    socket_dir = Path("/tmp/.X11-unix")
    try:
        if socket_dir.is_dir() and any(socket_dir.iterdir()):
            return True
    except OSError:
        # 目录存在但无权读取时不做结论，继续看进程表。
        pass

    proc = Path("/proc")
    try:
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                comm = (entry / "comm").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if comm.strip().lower() == "xwayland":
                return True
    except OSError:
        pass
    return False


def detect_session(env: Optional[dict] = None) -> SessionInfo:
    """探测当前会话类型。

    ``env`` 便于测试注入，默认取 ``os.environ``。

    判定顺序刻意保持简单：优先相信 ``XDG_SESSION_TYPE``，它由显示管理器
    在登录时写入，比逐个猜测环境变量可靠。缺失时再退到「谁的环境变量更
    完整」这种弱证据。
    """

    environ = os.environ if env is None else env
    raw = (environ.get("XDG_SESSION_TYPE") or "").strip().lower()
    display = environ.get("DISPLAY") or None
    wayland_display = environ.get("WAYLAND_DISPLAY") or None

    if raw == SessionKind.X11.value:
        return SessionInfo(
            kind=SessionKind.X11,
            raw_type=raw,
            display=display,
            wayland_display=wayland_display,
            xwayland=False,
        )

    if raw == SessionKind.WAYLAND.value:
        return SessionInfo(
            kind=SessionKind.WAYLAND,
            raw_type=raw,
            display=display,
            wayland_display=wayland_display,
            xwayland=_xwayland_present(),
        )

    # XDG_SESSION_TYPE 缺失或非标准：用弱证据推断。
    if wayland_display:
        return SessionInfo(
            kind=SessionKind.WAYLAND,
            raw_type=raw,
            display=display,
            wayland_display=wayland_display,
            xwayland=_xwayland_present(),
        )
    if display:
        return SessionInfo(
            kind=SessionKind.X11,
            raw_type=raw,
            display=display,
            wayland_display=wayland_display,
            xwayland=False,
        )
    return SessionInfo(
        kind=SessionKind.UNKNOWN,
        raw_type=raw,
        display=None,
        wayland_display=None,
        xwayland=False,
    )


def recommended_embed_mode(session: SessionInfo, requested: str) -> str:
    """给出该会话下推荐的嵌入模式，并附一句解释。

    返回值是 ``(模式, 解释)`` 里的模式。解释文本单独由
    :func:`embed_mode_reason` 提供，便于调用方分别使用。

    规则：

    - 用户显式指定的档位一律尊重，不擅自改写；
    - ``auto`` 在 X11 下保持原样，交给能力探测决定（可能命中 composite）；
    - ``auto`` 在 Wayland 下直接给 ``shm``：composite 需要抢占重定向，
      在 Wayland 会话里必然 BadAccess，提前避开可省掉一次失败的重定向尝试
      和对应的错误日志噪音。
    """

    text = (requested or "auto").strip().lower()
    if text != "auto":
        return text
    if session.kind is SessionKind.WAYLAND:
        # 没有 XWayland 时连透明度和几何都无从谈起，直接给最低档。
        return "shm" if session.xwayland else "fade"
    return "auto"


def embed_mode_reason(session: SessionInfo, requested: str) -> str:
    """解释为什么推荐这个嵌入模式，供日志与诊断输出。"""

    text = (requested or "auto").strip().lower()
    if text != "auto":
        return f"配置显式指定 EmbedMode={text}"

    if session.kind is SessionKind.WAYLAND:
        if session.xwayland:
            return (
                "Wayland 会话：合成器已接管顶层窗口重定向，composite 必然失败；"
                "目标应用经 XWayland 运行，故降级到 shm（真实窗口留在屏幕上做透明度渐变）"
            )
        return (
            "Wayland 会话且未检测到 XWayland：无法查询窗口几何，"
            "降级到 fade（仅整体渐隐）"
        )
    if session.kind is SessionKind.X11:
        return "X11 会话：交给能力探测，可能命中 composite 档"
    return "无法判定会话类型：交给能力探测决定"


# ----------------------------------------------------------------------
# mpv 视频输出驱动（vo）的选择
# ----------------------------------------------------------------------
# 为什么需要按会话选 vo
# ====================
# 默认的 ``vo=gpu`` 在纯 X11 会话下表现最好：走 OpenGL/EGL，画质与性能都优于
# 软件缩放。但在 Wayland 会话的 XWayland 窗口里（``QT_QPA_PLATFORM=xcb``，
# 窗口号是真实 X11 窗口 ID），实测 **``vo=gpu`` 会静默失效**：mpv 侧
# ``time-pos`` 正常推进、``vo-configured`` 也是 True、``current-vo`` 报 ``gpu``，
# 不打印任何错误，但窗口里一帧都没有——截屏出来是 100% 纯色底。
#
# 同一台机器、同一个窗口、同一条视频，换成 ``vo=xv`` 或 ``vo=x11`` 立刻出画面
# （截屏可得 5 万~6 万种颜色，属于真实视频内容）。因此这不是窗口号或路径问题，
# 而是 ``vo=gpu`` 在这条渲染路径上不可用。
#
# 依据上述实测结论：X11 会话保留 ``gpu``（既有行为不变，不冒回归风险）；
# Wayland 会话改用 ``xv``（带硬件加速的 X11 输出，画质优于纯软件 ``x11``）。

# 纯 X11 会话下的首选驱动：与原行为一致。
VO_X11 = "gpu"
# Wayland（经 XWayland）下的首选驱动：实测唯一稳定出画面的加速档。
VO_WAYLAND = "xv"
# 无法判定会话时的兜底驱动。
VO_FALLBACK = "xv"


def recommended_vo(session: SessionInfo) -> str:
    """按会话类型选择 mpv 的 ``vo`` 驱动。

    ``XAUTHORITY``/``DISPLAY`` 一类的环境细节不参与判断：真正决定因素只有
    「Qt 窗口是不是原生 X11 窗口」；只要走 ``QT_QPA_PLATFORM=xcb``，窗口号就
    是有效 X11 ID，两种会话下 mpv 都能把画面画进去，区别只在哪个驱动可靠。
    """

    if session.kind is SessionKind.X11:
        return VO_X11
    if session.kind is SessionKind.WAYLAND:
        return VO_WAYLAND
    return VO_FALLBACK


def vo_reason(session: SessionInfo) -> str:
    """解释为什么选这个 ``vo``，供日志输出。"""

    if session.kind is SessionKind.X11:
        return "X11 会话：沿用 vo=gpu（OpenGL 渲染，画质与性能最佳）"
    if session.kind is SessionKind.WAYLAND:
        return (
            "Wayland 会话（窗口经 XWayland）：vo=gpu 在此路径下实测会静默失败、"
            "画面全白，改用 vo=xv 确保真正出画"
        )
    return "无法判定会话类型：使用兜底 vo=xv"
