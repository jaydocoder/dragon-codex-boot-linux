"""播放窗口：无边框、置顶、固定几何的 PyQt6 窗口。

对上游 ``BootWindow`` 的等价实现：

- ``WindowStyle=None`` + ``Topmost=True`` → ``FramelessWindowHint`` +
  ``WindowStaysOnTopHint`` + ``X11BypassWindowManagerHint`` 关闭窗口管理器接管；
- ``FixedBoundsHook`` 拦截 ``WM_WINDOWPOSCHANGING`` → 这里的
  ``setFixedGeometry`` + ``setFixedSize``；
- 状态文字与「跳过 Esc」按钮沿用上游配色 ``rgb(115,91,144)`` / ``rgb(118,97,135)``。
"""

from __future__ import annotations

import os
from typing import Optional

from PyQt6.QtCore import QEvent, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QKeyEvent, QPainter
from PyQt6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QPushButton,
    QWidget,
)

from .geometry import Rect
from .log import log

# 上游配色常量（与 Windows 版保持一致，避免两平台视觉差异）。
BACKGROUND_COLOR = QColor(250, 247, 253)
STATUS_COLOR = QColor(115, 91, 144)
SKIP_TEXT_COLOR = QColor(118, 97, 135)
TEXT_WAITING = "Codex正在启动…  Esc跳过"
TEXT_VIDEO_FAILED = "视频无法播放，正在转入Codex"
TEXT_APP_FAILED = "无法自动打开Codex，按Esc退出"


def ensure_app() -> QApplication:
    """取得（必要时创建）QApplication 实例。

    创建前先把 Qt 的高 DPI 缩放钉成 1:1：``screenFrames`` 是按视频原始
    1920×1080 像素标定的，而 Qt6 在 X11 下会按 ``Xft.dpi`` 自动放大
    （实测 115 dpi → 缩放 1.2 → 窗口变成 2300×1294）。一旦缩放，
    Qt 坐标就不再等于 X11 像素，整条几何链路全部错位。

    环境变量必须在 ``QApplication`` 构造之前设置才生效，因此放在这里
    而不是调用方。
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "xcb")
    os.environ["QT_SCALE_FACTOR"] = "1"
    os.environ["QT_ENABLE_HIGHDPI_SCALING"] = "0"
    os.environ["QT_AUTO_SCREEN_SCALE_FACTOR"] = "0"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


class PlayerWindow(QWidget):
    """动画播放窗口。"""

    # 用户主动跳过（Esc 或点按钮）。
    skipRequested = pyqtSignal()

    def __init__(self, geometry: Rect, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._fixed_geometry = geometry
        self._status_text = ""
        self._status_visible = False
        # composite 档由播放窗口自己合成真实画面；其余档位保持 ``None``，
        # 画面由 mpv 直接渲染进本窗口的 X11 表面。
        self._overlay = None

        self.setWindowTitle("Dragon Codex Boot")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
            | Qt.WindowType.NoDropShadowWindowHint
        )
        # 窗口整体透明由渲染层控制；背景色仍按上游设置，避免出现黑底闪烁。
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setFixedSize(geometry.width, geometry.height)

        self._skip_button = QPushButton("跳过  Esc", self)
        self._skip_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._skip_button.setFont(QFont("Sans", 10))
        self._skip_button.setStyleSheet(
            "QPushButton {"
            "  background: white;"
            f"  color: rgb({SKIP_TEXT_COLOR.red()},{SKIP_TEXT_COLOR.green()},{SKIP_TEXT_COLOR.blue()});"
            "  border: 1px solid rgba(118,97,135,60);"
            "  border-radius: 4px;"
            "  padding: 5px 11px;"
            "}"
            "QPushButton:hover { background: white; }"
        )
        self._skip_button.move(geometry.width - 16 - 96, 16)
        self._skip_button.setFixedSize(96, 30)
        self._skip_button.clicked.connect(self.skipRequested.emit)
        self._skip_effect = QGraphicsOpacityEffect(self._skip_button)
        self._skip_button.setGraphicsEffect(self._skip_effect)
        self._update_skip_opacity(0.45)
        self._skip_button.installEventFilter(self)

    # ------------------------------------------------------------------
    # 状态文字
    # ------------------------------------------------------------------
    def show_status(self, text: str) -> None:
        if self._status_text != text or not self._status_visible:
            self._status_text = text
            self._status_visible = True
            self.update()

    def hide_status(self) -> None:
        if self._status_visible:
            self._status_visible = False
            self._status_text = ""
            self.update()

    # ------------------------------------------------------------------
    # 几何固定（对应上游 FixedBoundsHook）
    # ------------------------------------------------------------------
    def setFixedGeometry(self, rect: Rect) -> None:  # noqa: N802 - 对齐 Qt 命名风格
        """强制窗口保持指定位置与尺寸。"""
        self._fixed_geometry = rect
        self.setFixedSize(rect.width, rect.height)
        if self.isVisible():
            self.setGeometry(rect.left, rect.top, rect.width, rect.height)
        # 跳过按钮始终贴在右上角。
        self._skip_button.move(rect.width - 16 - 96, 16)

    def fixed_geometry(self) -> Rect:
        return self._fixed_geometry

    def set_overlay(self, frame) -> None:
        """设置由窗口自行合成的画面（``QImage`` 或 ``None``）。

        仅 composite 档使用：此时应用窗口已被 XComposite 重定向，mpv 无法
        把它画到屏幕上，需要由播放窗口逐帧抓取并合成。传入 ``None`` 表示
        交回 mpv 原生渲染。
        """
        self._overlay = frame
        if self._overlay is not None:
            self.update()

    def clear_overlay(self) -> None:
        """撤销自合成画面，恢复 mpv 原生渲染。"""
        self.set_overlay(None)

    def has_overlay(self) -> bool:
        """当前是否处于自合成模式。"""
        return self._overlay is not None

    def show_centered(self) -> None:
        """按固定几何显示窗口并抢到最前。"""
        rect = self._fixed_geometry
        self.setGeometry(rect.left, rect.top, rect.width, rect.height)
        self.show()
        self.raise_()
        self.activateWindow()

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        # 任何外部移动都立即纠正回固定几何（对应上游 FixedBoundsHook）。
        rect = self._fixed_geometry
        if self.isVisible() and (self.x() != rect.left or self.y() != rect.top):
            QTimer.singleShot(0, self._restore_geometry)

    def _restore_geometry(self) -> None:
        rect = self._fixed_geometry
        self.setGeometry(rect.left, rect.top, rect.width, rect.height)

    def _update_skip_opacity(self, opacity: float) -> None:
        """设置「跳过」按钮的不透明度（对应上游 0.45 → 1.0 悬停效果）。"""
        effect = self._skip_effect
        effect.setOpacity(opacity)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self._skip_button:
            if event.type() == QEvent.Type.Enter:
                self._update_skip_opacity(1.0)
            elif event.type() == QEvent.Type.Leave:
                self._update_skip_opacity(0.45)
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), BACKGROUND_COLOR)
        if self._overlay is not None:
            # 自合成档：真实画面已覆盖播放窗口范围内的全部像素，
            # 直接铺满即可。
            painter.drawImage(0, 0, self._overlay)
        if self._status_visible and self._status_text:
            painter.setPen(STATUS_COLOR)
            font = QFont("Sans", 12)
            painter.setFont(font)
            flags = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom
            painter.drawText(self.rect().adjusted(20, 20, -20, -38), flags, self._status_text)
        painter.end()


    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            log("skip-requested-by-escape")
            self.skipRequested.emit()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: N802
        # 上游在 ``Closed`` 里停止定时器与播放；这里由状态机统一处理。
        super().closeEvent(event)
