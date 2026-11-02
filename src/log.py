"""日志模块。

对齐上游行为：启动过程写入 ``logs/launcher.log``，不记录窗口标题，
也不截图，避免把用户内容落到磁盘上。
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Optional

_LOGGER: Optional["LauncherLog"] = None


class LauncherLog:
    """极简追加式日志，失败时静默（不能因为写日志失败中断启动动画）。"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.path = base_dir / "logs" / "launcher.log"

    def write(self, message: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{stamp} {message}\n")
        except OSError:
            # 上游同样吞掉写入异常：日志不可用不应影响动画与交接。
            pass

    def tail(self, count: int = 40) -> str:
        """读取末尾若干行，供 ``--selftest`` 与故障排查使用。"""
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-count:])


def setup(base_dir: Path) -> LauncherLog:
    """初始化全局日志实例。"""
    global _LOGGER
    _LOGGER = LauncherLog(base_dir)
    return _LOGGER


def log(message: str) -> None:
    """写入全局日志；未初始化时静默丢弃。"""
    if _LOGGER is not None:
        _LOGGER.write(message)
