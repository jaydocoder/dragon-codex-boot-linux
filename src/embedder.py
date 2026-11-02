"""把真实客户端窗口「嵌进」播放窗口的三档渲染方案。

上游 Windows 版用 ``DwmRegisterThumbnail`` 把 ChatGPT 窗口的实时缩略图画到
``BootWindow`` 上，并在过渡阶段把缩略图从旧矩形渐变到新矩形。X11 没有这套
DWM 私有 API，因此本模块提供三档等价方案，按能力从高到低自动降级：

1. ``composite``：用 XComposite ``RedirectWindow`` 把客户端窗口从屏幕渲染
   中移出（但仍保留离屏 pixmap），再用 ``XGetImage`` 实时抓取客户区像素，
   在播放窗口里合成。这是最接近 DWM 缩略图的方案：画面实时、位置与尺寸
   完全由播放窗口控制。要求合成器正在运行且 Composite 扩展可用。
2. ``shm``：客户端窗口留在屏幕上，播放窗口只在过渡阶段把它对齐到目标矩形，
   再用 ``_NET_WM_WINDOW_OPACITY`` 做交叉淡出。画面是真实窗口本身，
   不抓像素，代价是过渡动画只能做透明度插值。
3. ``fade``：只对客户端窗口做整体透明渐隐，位置与尺寸保持现状，
   等价于 macOS 版 ``DragonCodexBoot`` 的表现。

所有方案都遵守上游隐私边界：只读取像素与窗口几何，不读取窗口标题，
不把任何抓帧结果写入磁盘。
"""

from __future__ import annotations

import ctypes
import ctypes.util
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional, Protocol

from Xlib import X, Xatom, display as xdisplay, error as xerror

from .geometry import Rect
from .log import log

# XComposite 重定向模式常量（与 Xcomposite.h 保持一致）。
REDIRECT_AUTOMATIC = 0
REDIRECT_MANUAL = 1

# 抓帧失败时的退避时间：避免在窗口不可见时疯狂重试。
GRAB_RETRY_DELAY = 0.1


class EmbedMode(str, Enum):
    """嵌入方案标识，字符串值与配置项一致。"""

    COMPOSITE = "composite"
    SHM = "shm"
    FADE = "fade"


# ``auto`` 的探测顺序：能力强的排在前面。
AUTO_ORDER = (EmbedMode.COMPOSITE, EmbedMode.SHM, EmbedMode.FADE)


class EmbedderError(Exception):
    """嵌入层失败时抛出，消息为中文。"""


class PixelSource(Protocol):
    """抓帧来源的最小接口，便于在测试中注入假实现。"""

    def grab(self, rect: Rect) -> Optional[bytes]:
        """抓取屏幕上指定矩形区域的像素（BGRA 字节串）。"""


# ----------------------------------------------------------------------
# 纯逻辑部分（不依赖 X 服务器，可在无图形环境下单测）
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class CapabilityReport:
    """一次能力探测的结果，用于解释为什么选择了某个方案。

    ``redirect`` 表示「能否由本程序独占重定向目标窗口」。GNOME/KDE 等桌面
    的合成器（mutter/kwin）已经对每个顶层窗口做了 ``RedirectAutomatic``，
    X 服务端不允许第二个客户端再用 ``RedirectManual`` 抢同一个窗口，
    重定向请求会直接触发 ``BadAccess``。``XCompositeQueryExtension`` 只看
    扩展是否存在，因此静态探测在这里必然偏乐观，需要实测确认。
    """

    composite: bool
    shm: bool
    compositor: bool
    redirect: bool = False
    notes: tuple[str, ...] = field(default=())

    def supports(self, mode: EmbedMode) -> bool:
        if mode is EmbedMode.COMPOSITE:
            # composite 档必须同时满足：扩展可用 + 能抢占重定向 + 有合成器。
            return self.composite and self.redirect and self.compositor
        if mode is EmbedMode.SHM:
            # shm 档只依赖透明度属性，合成器不在时透明无效，故同样要求合成器。
            return self.compositor
        return True


def choose_mode(requested: str, report: CapabilityReport) -> EmbedMode:
    """按 ``auto`` 顺序或显式请求选择嵌入方案。

    显式请求但能力不足时**降级**而不是报错：启动动画属于锦上添花的功能，
    任何情况下都不应阻止用户进入应用。
    """

    text = (requested or "auto").strip().lower()
    if text == "auto":
        for mode in AUTO_ORDER:
            if report.supports(mode):
                return mode
        return EmbedMode.FADE
    try:
        mode = EmbedMode(text)
    except ValueError as exc:
        raise EmbedderError(
            f"未知的 EmbedMode 配置：{requested}（可选 auto/composite/shm/fade）"
        ) from exc
    if not report.supports(mode):
        return EmbedMode.FADE
    return mode


def frame_alpha(progress: float) -> float:
    """把 0~1 的过渡进度映射成播放窗口自身的不透明度。

    上游在过渡阶段直接绘制缩略图，不改变窗口透明度；X11 下为了让真实窗口
    透出来，播放窗口需要同步淡出。前 60% 进度保持全不透明，后 40% 线性淡出。
    """

    value = min(1.0, max(0.0, progress))
    if value <= 0.6:
        return 1.0
    return max(0.0, (1.0 - value) / 0.4)


def align_rect(origin: Rect, target: Rect, progress: float) -> Rect:
    """在起点与终点矩形之间线性插值（供 shm 档对齐真实窗口）。"""

    value = min(1.0, max(0.0, progress))
    return Rect(
        round(origin.left + (target.left - origin.left) * value),
        round(origin.top + (target.top - origin.top) * value),
        round(origin.width + (target.width - origin.width) * value),
        round(origin.height + (target.height - origin.height) * value),
    )


def split_rows(data: bytes, width: int, height: int) -> list[tuple[bytes, int]]:
    """把整块 BGRA 像素按行切分，返回 ``(行字节, 步长)`` 列表。

    ``XGetImage`` 返回的像素行步长恰好是 ``width*4``，但 Qt 的 ``QImage``
    在持有外部缓冲区时不会拷贝数据。逐行切片可以让每行都是独立的
    ``bytes`` 对象，从而把生命周期交给 Qt，避免出现悬空指针。
    """

    stride = width * 4
    if len(data) < stride * height:
        raise EmbedderError(
            f"像素数据长度不足：期望 {stride * height} 字节，实际 {len(data)} 字节"
        )
    return [(data[y * stride : (y + 1) * stride], stride) for y in range(height)]


# ----------------------------------------------------------------------
# X 服务器部分
# ----------------------------------------------------------------------
class _XComposite:
    """XComposite 的最小 ctypes 绑定。

    python-xlib 的 ``Xlib.ext.composite`` 只暴露 ``redirect_window`` 等少数
    python-xlib 却报缺失），因此直接绑定 ``libXcomposite.so.1`` 更可靠。

    python-xlib 是纯 Python 实现，只持有 socket，不暴露 C 层 ``Display*``
    指针，所以这里通过 ``libX11`` 的 ``XOpenDisplay`` 单独开一条原生连接。
    这条连接只用于重定向/解除重定向，不做任何事件读取，开销可以忽略。
    """

    def __init__(self, display_name: Optional[str] = None) -> None:
        self._x11 = None
        self._lib = None
        self._display = None
        composite_path = ctypes.util.find_library("Xcomposite")
        x11_path = ctypes.util.find_library("X11")
        if not composite_path or not x11_path:
            raise EmbedderError(
                "未找到 libXcomposite 或 libX11，无法使用 composite 嵌入方案"
            )
        x11 = ctypes.CDLL(x11_path, use_errno=True)
        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.restype = ctypes.c_int
        x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        x11.XSync.restype = ctypes.c_int
        lib = ctypes.CDLL(composite_path, use_errno=True)
        lib.XCompositeQueryExtension.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.XCompositeQueryExtension.restype = ctypes.c_int
        lib.XCompositeRedirectWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
        ]
        lib.XCompositeRedirectWindow.restype = None
        lib.XCompositeUnredirectWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
        ]
        lib.XCompositeUnredirectWindow.restype = None
        self._x11 = x11
        self._lib = lib
        self._display_name = display_name
        self._open()

    def _open(self) -> None:
        name = (
            self._display_name.encode()
            if self._display_name
            else None
        )
        handle = self._x11.XOpenDisplay(name)
        self._display = ctypes.c_void_p(handle) if handle else None

    def available(self) -> bool:
        if self._lib is None or self._display is None:
            return False
        event_base = ctypes.c_int()
        error_base = ctypes.c_int()
        try:
            ok = self._lib.XCompositeQueryExtension(
                self._display,
                ctypes.byref(event_base),
                ctypes.byref(error_base),
            )
        except Exception:
            return False
        return bool(ok)

    def redirect(self, window_id: int, mode: int) -> bool:
        """请求重定向，成功返回 ``True``。

        合成器已经持有该窗口时 X 会返回 ``BadAccess``。Xlib 默认的异步错误
        处理会直接终止进程（``X Error of failed request``），所以这里在
        python-xlib 侧注册一个临时错误处理器，把错误降级成返回值。
        """

        return self._with_error_capture(
            lambda: self._lib.XCompositeRedirectWindow(
                self._display, window_id, mode
            )
        )

    def unredirect(self, window_id: int, mode: int) -> bool:
        return self._with_error_capture(
            lambda: self._lib.XCompositeUnredirectWindow(
                self._display, window_id, mode
            )
        )

    def _with_error_capture(self, action) -> bool:
        """执行 X 请求并强制同步，捕获 BadAccess 等错误。

        注意：原生连接与 python-xlib 是两条独立连接，Xlib 的错误是
        按显示连接投递的，因此必须在原生连接上同步才能拿到错误码。
        这里用 ``XSetErrorHandler`` 拦截，避免默认处理器直接杀进程。
        """

        if self._display is None or self._x11 is None:
            return False
        try:
            self._x11.XSetErrorHandler.argtypes = [ctypes.c_void_p]
            self._x11.XSetErrorHandler.restype = ctypes.c_void_p
            errors: list[int] = []

            def _handler(_display, event):
                try:
                    errors.append(int(event.contents.error_code))
                except Exception:
                    errors.append(-1)
                return 0

            callback = ctypes.CFUNCTYPE(
                ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p
            )(_handler)
            previous = self._x11.XSetErrorHandler(callback)
            try:
                action()
                self._x11.XSync(self._display, 0)
            finally:
                self._x11.XSetErrorHandler(previous)
        except Exception as exc:
            log(f"embedder:x-error err={exc}")
            return False
        if errors:
            log(f"embedder:x-error codes={errors}")
            return False
        return True

    def close(self) -> None:
        if self._display is not None and self._x11 is not None:
            try:
                self._x11.XCloseDisplay(self._display)
            except Exception:
                pass
            self._display = None


class ScreenGrabber:
    """基于 ``XGetImage`` 的抓帧器。"""

    def __init__(self, display) -> None:
        self._display = display
        self._root = display.screen().root

    def grab(self, rect: Rect) -> Optional[bytes]:
        if rect.width <= 0 or rect.height <= 0:
            return None
        try:
            image = self._root.get_image(
                rect.left,
                rect.top,
                rect.width,
                rect.height,
                X.ZPixmap,
                0xFFFFFFFF,
            )
        except (xerror.BadMatch, xerror.BadDrawable, xerror.BadValue, xerror.BadWindow):
            return None
        data = image.data
        if not isinstance(data, bytes):
            data = bytes(data)
        if len(data) < rect.width * rect.height * 4:
            return None
        return data


@dataclass
class _EmbedState:
    """单个窗口的嵌入状态，便于统一清理。"""

    window_id: int = 0
    redirected: bool = False
    opacity: Optional[float] = None


class Embedder:
    """嵌入层统一入口，内部按选定的方案分派。"""

    def __init__(
        self,
        xdisplay_name: Optional[str] = None,
        *,
        display=None,
        grabber: Optional[PixelSource] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # 允许外部注入已经打开的连接（状态机复用同一条 X 连接）。
        if display is None:
            try:
                display = xdisplay.Display(xdisplay_name)
            except Exception as exc:
                raise EmbedderError(f"无法连接 X 服务器：{exc}") from exc
        self.display = display
        self._grabber: PixelSource = grabber or ScreenGrabber(display)
        self._display_name = xdisplay_name
        self._composite: Optional[_XComposite] = None
        try:
            self._composite = _XComposite(xdisplay_name)
        except EmbedderError as exc:
            # composite 不可用并不意味着整体不可用，交给 probe() 降级处理。
            log(f"embedder:composite-unavailable err={exc}")
        self._clock = clock
        self.mode: EmbedMode = EmbedMode.FADE
        self._state = _EmbedState()
        self._last_grab_at = 0.0
        self._last_frame: Optional[bytes] = None
        self._transition: dict[str, Rect] = {}


    # ------------------------------------------------------------------
    # 能力探测
    # ------------------------------------------------------------------
    def probe(self, window_id: Optional[int] = None) -> CapabilityReport:
        """探测 X 服务器能力，返回可用于解释降级原因的报告。

        ``window_id`` 非空时会真实验证重定向权限（见 ``_probe_redirect``）。
        """

        notes: list[str] = []
        composite = self._composite is not None and self._composite.available()
        if not composite:
            notes.append("Composite 扩展不可用")

        shm = self._probe_shm()
        if not shm:
            notes.append("MIT-SHM 扩展不可用")
        compositor = self._has_compositor()
        if not compositor:
            notes.append("没有运行中的合成器，透明度属性不会生效")

        redirect = False
        if composite and window_id:
            redirect = self._probe_redirect(window_id)
            if not redirect:
                notes.append(
                    "合成器已接管该窗口的重定向，无法抢占（X 返回 BadAccess）"
                )
        return CapabilityReport(
            composite=composite,
            shm=shm,
            compositor=compositor,
            redirect=redirect,
            notes=tuple(notes),
        )

    def _probe_redirect(self, window_id: int) -> bool:
        """实测能否独占重定向目标窗口，成功则立刻解除。"""

        if self._composite is None:
            return False
        if not self._composite.redirect(window_id, REDIRECT_MANUAL):
            return False
        self._composite.unredirect(window_id, REDIRECT_MANUAL)
        return True

    def _probe_shm(self) -> bool:
        """通过 X 服务器扩展列表确认 MIT-SHM 是否存在。

        注意不能用 ``Xlib.ext.shm`` 是否可导入来判断：python-xlib 把所有扩展
        模块都打包在一起，可导入并不代表服务端支持。
        """

        try:
            info = self.display.query_extension("MIT-SHM")
        except Exception:
            return False
        return info is not None

    def _has_compositor(self) -> bool:
        try:
            atom = self.display.intern_atom("_NET_WM_CM_S0")
            owner = self.display.get_selection_owner(atom)
        except Exception:
            return False
        return bool(owner)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def select(self, requested: str, window_id: Optional[int] = None) -> EmbedMode:
        """按配置选择方案并记录日志。

        传入 ``window_id`` 时会做一次真实的重定向探测：在合成器已经接管
        该窗口时，X 服务端会拒绝我们的 ``RedirectManual`` 请求，
        静态探测无法发现这一点。
        """

        report = self.probe(window_id)
        mode = choose_mode(requested, report)
        if report.notes:
            log(f"embedder:capability notes={'; '.join(report.notes)}")
        log(f"embedder:select requested={requested} mode={mode.value}")
        self.mode = mode
        return mode

    def attach(self, window_id: int) -> None:
        """绑定目标客户端窗口。"""

        self._state.window_id = int(window_id)

    def begin(self) -> bool:
        """进入过渡阶段前调用，返回该方案是否可用。

        composite 档如果在这一刻失败（例如合成器刚刚重启），就地降级到
        shm 并继续，绝不让启动动画的失败阻断用户进入应用。
        """

        if not self._state.window_id:
            return False
        if self.mode is not EmbedMode.COMPOSITE:
            return True
        if self._begin_composite():
            return True
        self.mode = EmbedMode.SHM if self._has_compositor() else EmbedMode.FADE
        log(f"embedder:degraded mode={self.mode.value}")
        return True

    def _begin_composite(self) -> bool:
        if self._composite is None:
            return False
        window_id = self._state.window_id
        if not self._composite.redirect(window_id, REDIRECT_MANUAL):
            return False
        self._state.redirected = True
        # 重定向后客户端需要重新绘制一帧，短暂等待以避免抓到空像素。
        time.sleep(0.05)
        return True

    def end(self) -> None:
        """过渡结束时调用，恢复窗口到正常渲染。"""

        if not self._state.window_id:
            return
        if self._state.redirected:
            try:
                if self._composite is not None:
                    self._composite.unredirect(self._state.window_id, REDIRECT_MANUAL)
            except Exception as exc:
                log(f"embedder:unredirect-failed err={exc}")
            self._state.redirected = False
        if self._state.opacity is not None:
            self.set_window_opacity(1.0)
        self._last_frame = None

    def close(self) -> None:
        self.end()
        if self._composite is not None:
            self._composite.close()

    # ------------------------------------------------------------------
    # 每帧回调
    # ------------------------------------------------------------------
    def grab_client(self, client: Rect) -> Optional[bytes]:
        """抓取客户区像素，失败时返回 ``None``（调用方保留上一帧）。"""

        if self.mode is not EmbedMode.COMPOSITE:
            return None
        now = self._clock()
        # 抓帧有成本（1920×1080 约 90ms），低于 20fps 的请求直接复用上一帧。
        if now - self._last_grab_at < 0.05 and self._last_frame is not None:
            return self._last_frame
        data = self._grabber.grab(client)
        self._last_grab_at = now
        if data is None:
            return self._last_frame
        self._last_frame = data
        return data

    # ------------------------------------------------------------------
    # 真实窗口操作
    # ------------------------------------------------------------------
    def set_window_opacity(self, opacity: float) -> bool:
        """设置真实客户端窗口的不透明度（0~1）。"""

        window_id = self._state.window_id
        if not window_id:
            return False
        raw = max(0, min(0xFFFFFFFF, int(round(opacity * 0xFFFFFFFF))))
        try:
            window = self.display.create_resource_object("window", window_id)
            window.change_property(
                self.display.intern_atom("_NET_WM_WINDOW_OPACITY"),
                Xatom.CARDINAL,
                32,
                [raw],
            )
            self.display.flush()
        except (xerror.BadWindow, xerror.BadMatch, xerror.BadValue):
            return False
        self._state.opacity = opacity
        return True

    def move_window(self, rect: Rect) -> bool:
        """把真实客户端窗口移动到指定客户区矩形（shm 档使用）。"""

        window_id = self._state.window_id
        if not window_id:
            return False
        try:
            window = self.display.create_resource_object("window", window_id)
            window.configure(
                x=rect.left,
                y=rect.top,
                width=max(1, rect.width),
                height=max(1, rect.height),
            )
            self.display.flush()
        except (xerror.BadWindow, xerror.BadValue, xerror.BadMatch):
            return False
        return True

    # ------------------------------------------------------------------
    # 过渡驱动
    # ------------------------------------------------------------------
    def start_transition(
        self,
        origin: Rect,
        target: Rect,
        window_rect: Rect,
    ) -> None:
        """记录过渡起止状态。

        ``origin``/``target`` 是客户区在屏幕上的起点与终点矩形，
        ``window_rect`` 是播放窗口在屏幕上的外框位置（用于换算绘制偏移）。
        """

        self._transition = {
            "origin": origin,
            "target": target,
            "window": window_rect,
        }
        if self.mode is EmbedMode.SHM:
            # shm 档让真实窗口原地可见：起始时把播放窗口整体淡出到底，
            # 之后随进度把真实窗口对齐到目标矩形。
            self.set_window_opacity(0.0)

    def apply_progress(self, progress: float) -> None:
        """按过渡进度驱动真实窗口（shm/fade 档）。"""

        transition = self._transition
        if not transition or not self._state.window_id:
            return
        value = min(1.0, max(0.0, progress))
        if self.mode is EmbedMode.SHM:
            # 真实窗口在屏幕上保持可见，透明度随进度升到 1。
            self.set_window_opacity(value)
            self.move_window(align_rect(
                transition["origin"], transition["target"], value
            ))
        elif self.mode is EmbedMode.FADE:
            # 只做整体渐隐：位置与尺寸保持现状，与 macOS 版表现一致。
            self.set_window_opacity(value)

    def compose_frame(self, clip: Rect):
        """抓取并组装当前帧，返回 ``QImage``；无内容时返回 ``None``。

        ``clip`` 是播放窗口的屏幕矩形，返回的图像与 ``clip`` 同尺寸，
        可直接用 ``drawImage(0, 0, frame)`` 画到播放窗口上。
        """

        if self.mode is not EmbedMode.COMPOSITE:
            return None
        transition = self._transition
        if not transition:
            return None
        origin = transition["origin"]
        target = transition["target"]
        window = transition["window"]
        # 抓取范围取起点与终点的并集：两者都在屏幕内且无需变换缩放。
        left = min(origin.left, target.left)
        top = min(origin.top, target.top)
        right = max(origin.left + origin.width, target.left + target.width)
        bottom = max(origin.top + origin.height, target.top + target.height)
        # 限制在播放窗口内，避免抓到窗口之外的无关像素。
        left = max(left, window.left)
        top = max(top, window.top)
        right = min(right, window.left + window.width)
        bottom = min(bottom, window.top + window.height)
        if right - left <= 0 or bottom - top <= 0:
            return None
        region = Rect(left, top, right - left, bottom - top)
        data = self.grab_client(region)
        if data is None:
            return None
        return bgra_to_qimage(data, region.width, region.height)


def bgra_to_qimage(data: bytes, width: int, height: int):
    """把 ``XGetImage`` 返回的 BGRA 数据转成 ``QImage``。

    实现要点：``QImage`` 用外部缓冲区构造时不会拷贝数据，只保存指针。
    这里按行切出独立的 ``bytes`` 对象，逐行 ``drawImage`` 进目标图像；
    Qt 在绘制时会把像素拷贝到目标内部，因此不存在悬空指针风险。

    ``Format_RGB32`` 在小端机器上的内存布局正好是 BGRA，与 ``XGetImage``
    的 ZPixmap 输出完全一致，无需额外的通道交换。

    延迟导入 Qt：本模块的纯逻辑函数需要在没有图形环境时也能被测试。
    """

    from PyQt6.QtGui import QImage, QPainter

    rows = split_rows(data, width, height)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    for y, (line, stride) in enumerate(rows):
        row = QImage(line, width, 1, stride, QImage.Format.Format_RGB32)
        painter.drawImage(0, y, row)
    painter.end()
    return image
