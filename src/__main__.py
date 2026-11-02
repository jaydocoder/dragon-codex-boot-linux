"""命令行入口：启动动画、标定、入口接管三条子命令。

用法示例::

    python -m src run                        # 播放启动动画并拉起 Codex
    python -m src run --launch-command "..." # 桌面入口透传的原始命令
    python -m src entries --plan             # 查看接管计划（只读）
    python -m src entries --adopt            # 接管入口
    python -m src entries --restore          # 回滚接管
    python -m src calibrate --seed-from ...  # 校验 screenFrames

退出码约定：

- ``0``：正常完成（动画播完并交接，或被用户跳过）；
- ``1``：可恢复的失败（视频打不开、应用一直起不来）；
- ``2``：配置或环境错误（配置文件缺失、X 服务器不可用）。
"""

from __future__ import annotations

import argparse
import shlex
import sys
from pathlib import Path
from typing import List, Optional

from . import __version__
from .config import ConfigError, load
from .log import log, setup as setup_log

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_ENV_ERROR = 2


# ----------------------------------------------------------------------
# 子命令参数
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dragon-codex-boot",
        description="Codex 启动动画启动器（Ubuntu / X11 版）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="播放启动动画并拉起 Codex")
    run.add_argument("--config", type=Path, help="配置文件路径，默认项目根目录的 launcher.json")
    run.add_argument(
        "--launch-command",
        default="",
        help="桌面入口透传的原始命令；给出时代替脚本里直接启动 Codex 的默认行为",
    )
    run.add_argument(
        "--skip-preflight",
        action="store_true",
        help="跳过 libmpv 预检（仅在已确认环境可用时使用）",
    )

    entries = sub.add_parser("entries", help="接管 / 恢复桌面入口")
    entries.add_argument("--config", type=Path, help="配置文件路径")
    group = entries.add_mutually_exclusive_group(required=True)
    group.add_argument("--plan", action="store_true", help="只读列出接管计划")
    group.add_argument("--adopt", action="store_true", help="接管选中的入口")
    group.add_argument("--restore", action="store_true", help="把入口恢复原状")
    group.add_argument("--verify", action="store_true", help="核对已接管的入口是否仍然有效")

    cal = sub.add_parser("calibrate", help="校验 screenFrames 标定值")
    cal.add_argument("--config", type=Path, help="配置文件路径")
    cal.add_argument("--seed-from", type=Path, help="从该配置文件读取种子几何逐点校验")
    cal.add_argument("--video", type=Path, help="覆盖配置里的视频路径")

    return parser


# ----------------------------------------------------------------------
# run
# ----------------------------------------------------------------------
def _resolve_launch_command(raw: str) -> Optional[List[str]]:
    """把 ``--launch-command`` 拆成参数列表。

    桌面入口里可能带 ``--profile-directory=Default``、``%U`` 之类的字段，
    这里只做 shell 分词，不理解其语义，整体交还给原程序。

    ``%U`` / ``%F`` 等字段码在直接启动时没有意义，需要剔除。
    """

    if not raw:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        log(f"cli:launch-command-unparsable err={exc}")
        return None
    # 剔除 Desktop Entry 的字段码。
    cleaned = [part for part in parts if not (len(part) == 2 and part[0] == "%")]
    return cleaned or None


def _apply_launch_command(config, raw: str) -> None:
    """把透传命令落到配置上。

    ``AppCommand`` 是脚本直接拉起 Codex 的默认命令（单个可执行文件）。桌面
    入口透传时以透传命令为准：它可能带 ``--profile-directory`` 等参数，
    因此整体写入 ``launch_command_parts``，由会话层原样 ``exec``。
    """

    parts = _resolve_launch_command(raw)
    if not parts:
        log("cli:launch-command-ignored")
        return
    config.app_command = parts[0]
    config.launch_command_parts = parts
    log(f"cli:launch-command args={len(parts)} program={parts[0]}")


def cmd_run(args: argparse.Namespace) -> int:
    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_ENV_ERROR

    _setup_logging(config)

    if args.launch_command:
        _apply_launch_command(config, args.launch_command)

    if not args.skip_preflight:
        from .video import run_mpv_preflight

        ok, detail = run_mpv_preflight()
        if not ok:
            print(f"运行环境检查未通过：{detail}", file=sys.stderr)
            return EXIT_ENV_ERROR

    # 用 ensure_app() 而不是直接构造 QApplication：它会先把 Qt 的高 DPI
    # 缩放钉成 1:1，必须早于 QApplication 构造生效。
    from .player import ensure_app
    from .session import BootSession

    app = ensure_app()
    # 会话进入终止阶段时显式退出事件循环：Qt 只在「最后一个窗口关闭」时自动
    # 退出，而这里还挂着定时器等非窗口对象，实测进程会一直空转不结束。
    session = BootSession(config, finished=app.quit)
    started = session.start()
    if not started.ok and started.reason.startswith("无法连接 X 服务器"):
        print(f"启动失败：{started.reason}", file=sys.stderr)
        return EXIT_ENV_ERROR

    # 会话在终止阶段会自己收尾；这里只负责跑事件循环。
    app.exec()
    session.shutdown()

    result = session.result()
    if result.ok:
        log(f"cli:done handed_off={result.handed_off}")
        return EXIT_OK
    print(f"启动未完成：{result.reason or '未知原因'}", file=sys.stderr)
    return EXIT_FAILURE


# ----------------------------------------------------------------------
# entries
# ----------------------------------------------------------------------
def cmd_entries(args: argparse.Namespace) -> int:
    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_ENV_ERROR

    _setup_logging(config)

    from . import entrypoints

    if args.plan:
        plans = entrypoints.build_plan(config)
        if not plans:
            print("没有找到可处理的桌面入口。")
            return EXIT_OK
        print(f"{'动作':<6} {'文件':<48} 原因")
        for plan in plans:
            print(f"{plan.action:<6} {str(plan.entry.path):<48} {plan.reason}")
        return EXIT_OK

    if args.adopt:
        adopted = 0
        for plan in entrypoints.build_plan(config):
            if plan.action != entrypoints.EntryPlan.ADOPT:
                continue
            backup = entrypoints.adopt(config, plan)
            print(f"已接管：{plan.entry.path}（备份 {backup.name}）")
            adopted += 1
        print(f"共接管 {adopted} 个入口。" if adopted else "没有需要接管的入口。")
        return EXIT_OK

    if args.restore:
        restored = entrypoints.restore(config)
        for path in restored:
            print(f"已恢复：{path}")
        print(f"共恢复 {len(restored)} 个入口。" if restored else "没有可恢复的入口。")
        return EXIT_OK

    if args.verify:
        problems = entrypoints.verify(config)
        for line in problems:
            print(line)
        if problems:
            return EXIT_FAILURE
        print("所有已接管的入口都有效。")
        return EXIT_OK

    return EXIT_ENV_ERROR


# ----------------------------------------------------------------------
# calibrate
# ----------------------------------------------------------------------
def cmd_calibrate(args: argparse.Namespace) -> int:
    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_ENV_ERROR

    _setup_logging(config)

    from .calibrate import calibrate, format_report, sample_frames
    from .video import VideoError

    seeds = config.screen_frames or None
    if args.seed_from is not None:
        try:
            seed_config = load(args.seed_from)
        except ConfigError as exc:
            print(f"种子配置错误：{exc}", file=sys.stderr)
            return EXIT_ENV_ERROR
        seeds = seed_config.screen_frames or None
    if not seeds:
        print(
            "没有可用的种子标定值：请先用 --seed-from 指定上游 screenFrames。\n"
            "本项目不做自动求解，原因见 docs/calibration.md。",
            file=sys.stderr,
        )
        return EXIT_ENV_ERROR

    video = args.video or config.resolve_video()
    times = [frame.time for frame in seeds]
    try:
        samples = sample_frames(
            video,
            times=times,
            width=config.player_width,
            height=config.player_height,
        )
    except VideoError as exc:
        print(f"采样失败：{exc}", file=sys.stderr)
        return EXIT_ENV_ERROR

    result = calibrate(
        samples,
        width=config.player_width,
        height=config.player_height,
        seeds=seeds,
    )
    print(format_report(result))
    # 未通过逐点校验时给出非零退出码，便于脚本判断。
    return EXIT_OK if result.trusted else EXIT_FAILURE


# ----------------------------------------------------------------------
# 入口
# ----------------------------------------------------------------------
def _setup_logging(config) -> None:
    """把日志接到 ``logs/launcher.log``。

    日志根目录按运行形态选择：源码形态为项目根，deb 安装形态为
    ``~/.local/state/dragon-codex-boot``（``/usr/lib`` 对普通用户不可写）。

    必须在使用任何 ``log()`` 之前调用：``log()`` 在未初始化时静默丢弃，
    排查启动问题时只会看到空日志。
    """

    from . import paths

    setup_log(paths.log_dir().parent)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_ENV_ERROR
    handlers = {
        "run": cmd_run,
        "entries": cmd_entries,
        "calibrate": cmd_calibrate,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
