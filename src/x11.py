"""X11 窗口交互层。

本模块对应上游 C# ``Native`` 类在 Linux/X11 下的等价实现，负责：

1. 通过 ``/proc`` 与 EWMH 属性找到目标客户端主窗口（等价 ``FindApp``）；
2. 读取客户区几何、窗口外框与边框内缩量（等价 ``ClientBounds`` +
   ``_NET_FRAME_EXTENTS``）；
3. 把客户端客户区对齐到指定矩形（等价 ``AlignClient``）；
4. 恢复最小化窗口、激活并移交焦点（等价 ``ShowWindow`` + ``SetForegroundWindow``）。

所有查询都只读取窗口几何与进程信息，不读取窗口标题、不截图，
这一点与上游的隐私边界保持一致。
"""

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

from Xlib import X, Xatom, display as xdisplay, error as xerror
from Xlib.protocol import event as xevent

from .geometry import Rect, window_for_client

# 上游采用的窗口尺寸门槛：过小的窗口视为辅助窗或弹窗，不作为主窗口。
MIN_CLIENT_WIDTH = 450
MIN_CLIENT_HEIGHT = 280


class X11Error(Exception):
    """X11 交互失败时抛出，消息为中文。"""


@dataclass(frozen=True)
class WindowGeometry:
    """客户端窗口几何信息。"""

    window: int
    # 窗口外框在屏幕上的位置与尺寸（含窗口管理器装饰）。
    frame: Rect
    # 客户区在屏幕上的位置与尺寸（内容区）。
    client: Rect
    # 窗口管理器附加的边框内缩量：client - frame 的偏移。
    inset_left: int
    inset_top: int


def _client_message(display, window, message_type: str, data: Sequence[int]) -> None:
    """发送 EWMH 客户端消息（用于激活窗口）。"""
    atom = display.intern_atom(message_type)
    payload = list(data) + [0] * (5 - len(data))
    event = xevent.ClientMessage(
        window=window,
        client_type=atom,
        data=(32, payload),
    )
    root = display.screen().root
    root.send_event(
        event,
        event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
    )
    display.flush()


class X11WindowManager:
    """X11 查询与操作集合，供播放器与状态机调用。"""

    def __init__(self, display_name: Optional[str] = None) -> None:
        try:
            self.display = xdisplay.Display(display_name)
        except Exception as exc:  # python-xlib 抛出多种连接异常
            raise X11Error(
                f"无法连接 X 服务器（DISPLAY={display_name or os.environ.get('DISPLAY')}）：{exc}"
            ) from exc
        self.root = self.display.screen().root
        self._atoms = {
            name: self.display.intern_atom(name)
            for name in (
                "_NET_CLIENT_LIST",
                "_NET_CLIENT_LIST_STACKING",
                "_NET_ACTIVE_WINDOW",
                "_NET_WM_PID",
                "_NET_WM_STATE",
                "_NET_WM_STATE_HIDDEN",
                "_NET_WM_STATE_SKIP_TASKBAR",
                "_NET_WM_WINDOW_TYPE",
                "_NET_WM_WINDOW_TYPE_NORMAL",
                "_NET_FRAME_EXTENTS",
                "_NET_WM_WINDOW_OPACITY",
            )
        }

    # ------------------------------------------------------------------
    # 基础属性读取
    # ------------------------------------------------------------------
    def close(self) -> None:
        try:
            self.display.close()
        except Exception:
            pass

    def atom(self, name: str) -> int:
        if name not in self._atoms:
            self._atoms[name] = self.display.intern_atom(name)
        return self._atoms[name]

    def _property(self, window, name: str):
        try:
            return window.get_full_property(self.atom(name), X.AnyPropertyType)
        except (xerror.BadWindow, xerror.BadAtom, xerror.BadMatch):
            return None

    def screen_geometry(self) -> Rect:
        """返回主屏幕几何（用于居中播放窗口）。"""
        screen = self.display.screen()
        return Rect(0, 0, screen.width_in_pixels, screen.height_in_pixels)

    def has_compositor(self) -> bool:
        """检测合成器是否在运行。

        判定依据是 ``_NET_WM_CM_S0`` 的 selection owner 是否存在，
        这是 EWMH 规范定义的方式；``xprop -root`` 并不会显示它。
        """
        try:
            owner = self.display.get_selection_owner(
                self.display.intern_atom("_NET_WM_CM_S0")
            )
        except Exception:
            return False
        return bool(owner)

    # ------------------------------------------------------------------
    # 进程与窗口查找
    # ------------------------------------------------------------------
    @staticmethod
    def _read_process_info(pid: int) -> tuple[Optional[str], str]:
        """读取进程可执行文件路径与命令行。"""
        exe: Optional[str] = None
        try:
            exe = os.readlink(f"/proc/{pid}/exe")
        except OSError:
            exe = None
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            cmdline = ""
        return exe, cmdline

    def candidate_pids(
        self, process_names: Iterable[str], path_contains: str
    ) -> List[int]:
        """按进程名与路径过滤出候选 PID 集合（等价上游进程筛选）。"""
        names = {item.strip().lower() for item in process_names if item.strip()}
        result: List[int] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            exe, cmdline = self._read_process_info(pid)
            # 进程名匹配：可执行文件名，或命令行首个 token 的 basename。
            matched_name = False
            if exe:
                base = os.path.basename(exe).lower()
                if base in names:
                    matched_name = True
            if not matched_name and cmdline:
                first = cmdline.split(" ", 1)[0]
                if os.path.basename(first).lower() in names:
                    matched_name = True
            if not matched_name:
                continue
            # 路径过滤：用于区分同名进程（上游 ProcessPathContains 语义）。
            haystack = f"{exe or ''} {cmdline}"
            if path_contains and path_contains.lower() not in haystack.lower():
                continue
            result.append(pid)
        return result

    def _window_pid(self, window) -> Optional[int]:
        prop = self._property(window, "_NET_WM_PID")
        if prop is None or not prop.value:
            return None
        try:
            # 部分实现返回 bytes，部分返回 int 列表。
            value = prop.value
            if isinstance(value, bytes):
                return int.from_bytes(value[:4], "little")
            return int(value[0])
        except (IndexError, TypeError, ValueError):
            return None

    def _is_hidden(self, window) -> bool:
        prop = self._property(window, "_NET_WM_STATE")
        if prop is None or not prop.value:
            return False
        hidden = self.atom("_NET_WM_STATE_HIDDEN")
        return hidden in prop.value

    def client_list(self) -> List:
        """返回 EWMH 客户端窗口列表。"""
        prop = self._property(self.root, "_NET_CLIENT_LIST")
        if prop is None or not prop.value:
            return []
        windows = []
        for wid in prop.value:
            try:
                windows.append(self.display.create_resource_object("window", wid))
            except Exception:
                continue
        return windows

    def geometry_of(self, window) -> WindowGeometry:
        """读取窗口外框与客户区几何（等价上游 ``ClientBounds``）。"""
        try:
            geom = window.get_geometry()
            translate = window.translate_coords(self.root, 0, 0)
        except (xerror.BadWindow, xerror.BadDrawable) as exc:
            raise X11Error(f"窗口几何读取失败：{exc}") from exc

        # translate_coords(root, 0, 0) 给出窗口原点在根坐标系中的位置。
        left = -translate.x
        top = -translate.y
        client = Rect(left, top, geom.width, geom.height)

        # 边框内缩量：EWMH 的 _NET_FRAME_EXTENTS = [left, right, top, bottom]。
        inset_left = inset_top = 0
        extents = self._property(window, "_NET_FRAME_EXTENTS")
        if extents is not None and extents.value and len(extents.value) >= 4:
            inset_left = int(extents.value[0])
            inset_top = int(extents.value[2])
        else:
            # 回退：用父窗口几何推算装饰尺寸。
            try:
                parent = window.query_tree().parent
                if parent is not None and parent != self.root:
                    pgeom = parent.get_geometry()
                    pointer = parent.translate_coords(self.root, 0, 0)
                    pleft, ptop = -pointer.x, -pointer.y
                    inset_left = max(0, left - pleft)
                    inset_top = max(0, top - ptop)
            except Exception:
                pass

        frame = Rect(
            client.left - inset_left,
            client.top - inset_top,
            client.width + inset_left * 2,
            client.height + inset_top * 2,
        )
        return WindowGeometry(
            window=window.id,
            frame=frame,
            client=client,
            inset_left=inset_left,
            inset_top=inset_top,
        )

    def find_app_window(
        self,
        process_names: Iterable[str],
        path_contains: str,
        wm_class_hint: str = "",
    ) -> Optional[int]:
        """查找目标客户端主窗口 XID（等价上游 ``FindApp``）。

        过滤链与上游一致：属于候选进程 → 可见 → 不是瞬态弹窗 →
        未最小化 → 客户区尺寸达到门槛 → 取最前面的合格窗口。
        """
        pids = set(self.candidate_pids(process_names, path_contains))
        if not pids:
            return None

        hint = (wm_class_hint or "").strip().lower()
        strong: List[int] = []
        weak: List[int] = []
        for window in self.client_list():
            pid = self._window_pid(window)
            if pid is None or pid not in pids:
                continue
            try:
                attributes = window.get_attributes()
            except (xerror.BadWindow, xerror.BadDrawable):
                continue
            # 注意：被最小化的窗口 map_state 会变成 IsUnmapped 并带上
            # _NET_WM_STATE_HIDDEN。上游对最小化窗口的处理是「先恢复再使用」，
            # 而不是排除，因此这里不因最小化直接丢弃候选窗口。
            if attributes.map_state != X.IsViewable and not self._is_hidden(window):
                continue
            # 瞬态窗口（对话框、tooltip）不作为主窗口。
            try:
                if window.get_wm_transient_for():
                    continue
            except (xerror.BadWindow, xerror.BadDrawable):
                continue
            try:
                geom = self.geometry_of(window)
            except X11Error:
                continue
            if (
                geom.client.width < MIN_CLIENT_WIDTH
                or geom.client.height < MIN_CLIENT_HEIGHT
            ):
                continue
            # WM_CLASS 匹配是软条件：命中进入强候选，未命中仍保留为弱候选。
            # Electron 应用常有多个同进程窗口，类名能帮我们挑出主窗口。
            if hint and hint in self._wm_classes(window):
                strong.append(window)
            else:
                weak.append(window)

        candidates = strong or weak
        if not candidates:
            return None

        # 用 stacking 列表的逆序（最上层优先）挑选，等价上游「最前面的合格窗口」。
        stacking_prop = self._property(self.root, "_NET_CLIENT_LIST_STACKING")
        order: List[int] = []
        if stacking_prop is not None and stacking_prop.value:
            order = list(stacking_prop.value)
        candidate_ids = {window.id: window for window in candidates}
        for wid in reversed(order):
            if wid in candidate_ids:
                return wid
        return candidates[0].id

    def _wm_classes(self, window) -> set[str]:
        """读取 WM_CLASS 的 instance/class 两个字段，统一转成小写集合。

        python-xlib 的 ``get_wm_class()`` 返回 ``(instance, class)`` 元组，
        两者都可能为 ``None``；这里统一过滤掉空值。
        """

        try:
            raw = window.get_wm_class() or ()
        except (xerror.BadWindow, xerror.BadDrawable):
            return set()
        if isinstance(raw, str):
            raw = (raw,)
        return {str(item).lower() for item in raw if item}

    def window(self, window_id: int):
        """由 XID 取得窗口资源对象。"""
        return self.display.create_resource_object("window", window_id)

    def is_minimized(self, window_id: int) -> bool:
        try:
            return self._is_hidden(self.window(window_id))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # 窗口操作
    # ------------------------------------------------------------------
    def restore_window(self, window_id: int) -> bool:
        """恢复最小化窗口（等价上游 ``ShowWindow(h, SW_RESTORE)``）。"""
        window = self.window(window_id)
        try:
            current = window.get_wm_normal_hints()
            was_maximized = False
            state = self._property(window, "_NET_WM_STATE")
            if state is not None and state.value:
                maximized_atoms = {
                    self.atom("_NET_WM_STATE_MAXIMIZED_VERT"),
                    self.atom("_NET_WM_STATE_MAXIMIZED_HORZ"),
                }
                was_maximized = bool(maximized_atoms & set(state.value))
            del current
            if was_maximized:
                # 先取消最大化，后续对齐才能稳定生效。
                for atom_name, action in (
                    ("_NET_WM_STATE_MAXIMIZED_VERT", 0),
                    ("_NET_WM_STATE_MAXIMIZED_HORZ", 0),
                ):
                    _client_message(
                        self.display,
                        window,
                        "_NET_WM_STATE",
                        [action, self.atom(atom_name), 0, 1],
                    )
            window.map()
            self.display.flush()
            return True
        except (xerror.BadWindow, xerror.BadDrawable):
            return False

    def activate(self, window_id: int) -> bool:
        """激活窗口并移交焦点（等价上游 ``SetForegroundWindow``）。"""
        window = self.window(window_id)
        try:
            _client_message(
                self.display,
                window,
                "_NET_ACTIVE_WINDOW",
                [1, X.CurrentTime, 0],
            )
            try:
                window.set_input_focus(X.RevertToParent, X.CurrentTime)
                window.configure(stack_mode=X.Above)
            except (xerror.BadWindow, xerror.BadMatch):
                pass
            # 必须 sync 而不是 flush：本进程紧接着就会退出并断开 X 连接，
            # 只 flush 的话请求可能还躺在缓冲里，mutter 根本收不到，
            # 用户看到的就是「动画放完了，焦点却没交过去」。
            self.display.sync()
            return True
        except (xerror.BadWindow, xerror.BadDrawable):
            return False

    def active_window(self) -> int:
        """返回 ``_NET_ACTIVE_WINDOW`` 记录的当前活动窗口号（无则 0）。"""
        prop = self._property(self.root, "_NET_ACTIVE_WINDOW")
        if prop is None or not prop.value:
            return 0
        return int(prop.value[0])

    def client_size_limits(self, window_id: int) -> tuple[int, int]:
        """读取窗口管理器的客户区最小尺寸约束（来自 WM_NORMAL_HINTS）。

        实测 ChatGPT 客户端（Electron）声明了 ``min_height=722``，
        小于该高度的对齐请求会被窗口管理器直接钳制。
        """
        window = self.window(window_id)
        try:
            hints = window.get_wm_normal_hints()
            min_width = int(getattr(hints, "min_width", 0) or 0)
            min_height = int(getattr(hints, "min_height", 0) or 0)
        except Exception:
            return 0, 0
        return max(0, min_width), max(0, min_height)

    @staticmethod
    def _near(actual: int, expected: int, tolerance: int = 2) -> bool:
        return abs(actual - expected) <= tolerance

    def clear_size_constraints(self, window_id: int) -> bool:
        """清除会强制窗口尺寸的 EWMH 状态：全屏、最大化。

        实测在本机 GNOME/mutter 下，ChatGPT 客户端会带
        ``_NET_WM_STATE_FULLSCREEN`` 与 ``_NET_WM_STATE_MAXIMIZED_*``，
        此时任何 ``ConfigureRequest`` 的尺寸都会被窗口管理器覆盖，
        必须先撤销这些状态，对齐才能生效。
        """
        window = self.window(window_id)
        state = self._property(window, "_NET_WM_STATE")
        if state is None or not state.value:
            return True

        forced = {
            self.atom("_NET_WM_STATE_FULLSCREEN"),
            self.atom("_NET_WM_STATE_MAXIMIZED_VERT"),
            self.atom("_NET_WM_STATE_MAXIMIZED_HORZ"),
        }
        to_clear = [atom_id for atom_id in state.value if atom_id in forced]
        if not to_clear:
            return True

        try:
            for atom_id in to_clear:
                # action=0 表示 remove，第二个属性位填 1 表示垂直方向优先。
                _client_message(
                    self.display, window, "_NET_WM_STATE", [0, atom_id, 0, 1]
                )
            self.display.flush()
            return True
        except (xerror.BadWindow, xerror.BadMatch):
            return False

    def align_client(self, window_id: int, desired: Rect, attempts: int = 3) -> bool:
        """把客户区对齐到目标矩形（等价上游 ``AlignClient``）。

        上游注释强调：Electron/Chromium 窗口在还原后边框内缩量会变化，
        因此每轮都重新测量、重新换算，最后校验误差 ≤2px。

        Linux 侧的额外情况：窗口管理器可能对尺寸做钳制（例如 ChatGPT
        客户端声明 ``min_height=722``）。此时严格按目标值校验会误判为失败，
        所以先读取 ``WM_NORMAL_HINTS`` 求出「窗口管理器允许的期望尺寸」，
        实际尺寸等于钳制后的期望值即视为成功；请求值本身超出约束时
        退化为「尺寸已到边界且位置正确」。
        """
        window = self.window(window_id)
        if self.is_minimized(window_id):
            self.restore_window(window_id)
        # 先撤掉全屏/最大化，否则窗口管理器会覆盖我们请求的尺寸。
        if self.clear_size_constraints(window_id):
            self.display.sync()
            time.sleep(0.15)

        min_width, min_height = self.client_size_limits(window_id)
        # 窗口管理器允许的最接近目标尺寸的可达值。
        expected_width = max(desired.width, min_width)
        expected_height = max(desired.height, min_height)
        # 目标值本身超出约束时，只能退化为边界校验。
        clamped_by_request = (
            expected_width != desired.width or expected_height != desired.height
        )

        for _ in range(max(1, attempts)):
            try:
                geom = self.geometry_of(window)
            except X11Error:
                return False
            if geom.client.width <= 0 or geom.client.height <= 0:
                return False
            outer = window_for_client(geom.frame, geom.client, desired)
            try:
                window.configure(
                    x=outer.left,
                    y=outer.top,
                    width=max(1, outer.width),
                    height=max(1, outer.height),
                )
                self.display.flush()
                self.display.sync()
            except (xerror.BadWindow, xerror.BadValue, xerror.BadMatch):
                return False
            # 给窗口管理器一点时间落地尺寸变更，下一轮才有意义。
            time.sleep(0.05)

        try:
            actual = self.geometry_of(window).client
        except X11Error:
            return False
        if not (
            self._near(actual.left, desired.left)
            and self._near(actual.top, desired.top)
        ):
            return False
        # 宽度/高度的可接受实际值：期望值，或（目标小于下限时）下限本身。
        allowed_widths = {desired.width, max(desired.width, min_width)}
        allowed_heights = {desired.height, max(desired.height, min_height)}
        width_ok = any(self._near(actual.width, value) for value in allowed_widths)
        height_ok = any(self._near(actual.height, value) for value in allowed_heights)
        # 目标值超出约束时窗口管理器只能钳制到边界，此时要求实际值不小于可达上限。
        if clamped_by_request:
            width_ok = width_ok or self._near(actual.width, min_width)
            height_ok = height_ok or self._near(actual.height, min_height)
        return width_ok and height_ok

    # ------------------------------------------------------------------
    # 播放窗口专用
    # ------------------------------------------------------------------
    def set_override_redirect(self, window_id: int) -> None:
        window = self.window(window_id)
        try:
            window.change_attributes(override_redirect=1)
            self.display.flush()
        except (xerror.BadWindow, xerror.BadMatch):
            pass

    def set_opacity(self, window_id: int, opacity: float) -> bool:
        """设置窗口整体不透明度（0~1），用于交叉淡出回退方案。"""
        window = self.window(window_id)
        raw = max(0, min(0xFFFFFFFF, int(round(opacity * 0xFFFFFFFF))))
        try:
            window.change_property(
                self.atom("_NET_WM_WINDOW_OPACITY"),
                Xatom.CARDINAL,
                32,
                [raw],
            )
            self.display.flush()
            return True
        except (xerror.BadWindow, xerror.BadMatch, xerror.BadValue):
            return False

    def move_resize(self, window_id: int, rect: Rect) -> bool:
        window = self.window(window_id)
        try:
            window.configure(
                x=rect.left,
                y=rect.top,
                width=max(1, rect.width),
                height=max(1, rect.height),
            )
            self.display.flush()
            return True
        except (xerror.BadWindow, xerror.BadValue, xerror.BadMatch):
            return False
