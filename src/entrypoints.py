"""桌面入口（.desktop）管理。

上游 Windows 版通过改写开始菜单快捷方式实现「点图标即播放启动动画」。
Linux 的对应物是 XDG 桌面入口文件，本模块负责：

- 发现目标应用的 .desktop 入口（用户级优先于系统级）；
- 识别「已被第三方脚本包装」的入口，默认不动它们，
  避免把别人的启动器包装再包一层；
- 改动前先备份原始内容并记录 SHA-256，用于回滚与校验；
- 写回时保留用户自定义的其它字段（字段顺序、注释、未知键）。

设计取舍：只改 ``Exec=`` 一行，其余内容原样保留。上游 Windows 版
会重建整个快捷方式，Linux 上那样做会丢用户的本地化名字、图标覆盖
和自定义动作，代价过高。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional

from .config import Config
from .log import log

# XDG 搜索顺序：用户级覆盖系统级，因此用户级先出现。
XDG_DATA_DIRS_DEFAULT = "/usr/local/share:/usr/share"

# 备份后缀。统一放在同目录下，方便用户手工回滚。
BACKUP_SUFFIX = ".dcb-linux.bak"

# 我们写入 Exec 时添加的标记，用于幂等判断（已接管就跳过）。
# 必须与 _build_exec 实际写入的参数一致，否则「已接管」永远识别不出来。
MARKER = "--launch-command"

# 段头形状：[Desktop Entry]、[Desktop Action new-window] 等。
_SECTION_RE = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")

# 键值行：key=value，允许等号两侧有空白，保留原始缩进。
_KEY_RE = re.compile(r"^(?P<lead>\s*)(?P<key>[A-Za-z0-9-]+)(?P<eq>\s*=\s*)(?P<value>.*)$")

# 从已接管的 Exec 里还原出原始目标命令。
# 本工具写入时总是加双引号，但人工编辑或第三方脚本可能写成无引号形式，
# 两种都要能识别，否则「已接管」判断和重复接管防护都会失效。
_LAUNCH_COMMAND_RE = re.compile(
    r'--launch-command\s+(?:"(?P<quoted>[^"]*)"|(?P<bare>[^"\s]+))'
)

@dataclass(frozen=True)
class EntryField:
    """``.desktop`` 文件里的一条 ``key=value`` 记录。"""

    key: str
    value: str
    # 所在的组名，例如 ``Desktop Entry`` 或 ``Desktop Action new-window``。
    section: str


@dataclass
class DesktopEntry:
    """一个解析后的桌面入口。"""

    path: Path
    # 文件原始内容，保持行尾与编码不变，用于逐行改写与还原。
    raw: str
    # 顶层 ``[Desktop Entry]`` 组里的字段，键名大小写保持原样。
    fields: dict[str, str] = field(default_factory=dict)
    # 出现在 ``[Desktop Entry]`` 之后的动作组名。
    action_sections: List[str] = field(default_factory=list)

    # ---- 常用字段 ----
    @property
    def name(self) -> str:
        return self.fields.get("Name", "").strip()

    @property
    def exec_line(self) -> str:
        return self.fields.get("Exec", "").strip()

    @property
    def try_exec(self) -> str:
        return self.fields.get("TryExec", "").strip()

    @property
    def startup_wm_class(self) -> str:
        return self.fields.get("StartupWMClass", "").strip()

    @property
    def is_hidden(self) -> bool:
        """Hidden=true 的入口不会出现在菜单里，接管它没有意义。"""

        return self.fields.get("Hidden", "").strip().lower() == "true"

    @property
    def is_nodisplay(self) -> bool:
        """NoDisplay 多半是协议处理器，不应当被接管。"""

        return self.fields.get("NoDisplay", "").strip().lower() == "true"

    @property
    def is_managed(self) -> bool:
        """是否已被本工具接管（Exec 里带我们的标记）。"""

        return MARKER in self.exec_line

    def exec_arguments(self) -> str:
        """``Exec`` 去掉可执行文件后的参数部分，用于保留 %U/%F 等占位符。"""

        return _split_exec(self.exec_line)[1]

    def exec_program(self) -> str:
        """``Exec`` 的可执行文件部分（已去引号）。"""

        return _split_exec(self.exec_line)[0]


@dataclass
class EntryPlan:
    """对某个入口的接管计划。"""

    entry: DesktopEntry
    # 是否会被改写。
    action: str
    # 中文原因说明，用于命令行输出。
    reason: str

    # 动作取值：接管 / 跳过 / 已接管 / 只读
    ADOPT = "接管"
    SKIP = "跳过"
    ALREADY = "已接管"
    READONLY = "只读"


def _split_exec(exec_line: str) -> tuple[str, str]:
    """把 ``Exec`` 拆成（可执行文件, 其余参数）。

    首段可能带引号，需要按引号解析；其余部分原样返回，
    因为里面的 ``%U``、``--flag=value`` 等不应被触碰。
    """

    text = exec_line.strip()
    if not text:
        return "", ""
    if text[0] in "\"'":
        quote = text[0]
        end = text.find(quote, 1)
        if end < 0:
            return text[1:], ""
        return text[1:end], text[end + 1 :].strip()
    parts = text.split(None, 1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1].strip()


def parse_desktop_entry(path: Path) -> Optional[DesktopEntry]:
    """解析 ``.desktop`` 文件；无法读取或缺少 ``[Desktop Entry]`` 时返回 None。"""

    try:
        # newline="" 关闭通用换行转换：不保留它，CRLF 文件读进来就变成 LF，
        # 改写后再写回会悄无声息地改掉整个文件的换行风格。
        with path.open("r", encoding="utf-8", newline="") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        log(f"entrypoints:read-failed path={path} error={exc}")
        return None

    entry = DesktopEntry(path=path, raw=raw)
    current: Optional[str] = None
    for line in raw.splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match:
            current = section_match.group("name").strip()
            if current != "Desktop Entry":
                entry.action_sections.append(current)
            continue
        if current is None:
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key_match = _KEY_RE.match(line)
        if not key_match:
            continue
        if current != "Desktop Entry":
            continue
        # 同一组内重复键以最后一次为准，与 GLib 的解析行为一致。
        entry.fields[key_match.group("key")] = key_match.group("value")

    if "Exec" not in entry.fields:
        return None
    return entry


def xdg_application_dirs() -> List[Path]:
    """按 XDG 规范返回应用目录，用户级在前。"""

    dirs: List[Path] = []
    home_data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    dirs.append(Path(home_data) / "applications")

    data_dirs = os.environ.get("XDG_DATA_DIRS") or XDG_DATA_DIRS_DEFAULT
    for item in data_dirs.split(":"):
        item = item.strip()
        if not item:
            continue
        dirs.append(Path(item) / "applications")

    # 去重但保序，避免用户把同一个目录写进 XDG_DATA_DIRS 两次。
    seen: set[str] = set()
    unique: List[Path] = []
    for item in dirs:
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _iter_desktop_files(directory: Path) -> Iterable[Path]:
    try:
        children = sorted(directory.iterdir())
    except OSError:
        return []
    return [p for p in children if p.is_file() and p.suffix == ".desktop"]


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _resolve_try_exec(entry: DesktopEntry) -> Optional[Path]:
    """解析 ``TryExec`` 为绝对路径；未设置时返回 None。

    ``TryExec`` 通常是裸命令名，需要走 PATH 查找，而不是当成相对路径。
    """

    target = entry.try_exec
    if not target:
        return None
    if target.startswith("/"):
        return Path(target)
    found = shutil.which(target)
    return Path(found) if found else None


def program_exists(entry: DesktopEntry) -> bool:
    """入口指向的程序是否存在。用于排除残留的失效入口。"""

    program = entry.exec_program()
    if not program:
        return True
    # Exec 里可能带 env/setsid 之类的前缀，取第一个看起来像路径的段。
    if program.startswith("/"):
        return Path(program).exists()
    return shutil.which(program) is not None


def looks_wrapped(entry: DesktopEntry) -> bool:
    """判断入口是否已被第三方脚本包装。

    两类判据：
    1. ``Exec`` 指向用户自建目录下的可执行文件（升级官方包不会覆盖的位置）；
    2. ``Exec`` 的程序名与官方入口的程序名不同，说明被换过。

    这里只看形式特征，不猜测意图：命中任一条就按「已包装」处理，
    交给 ``IncludeWrappedEntrypoints`` 开关决定是否仍然接管。
    """

    exec_line = entry.exec_line
    if not exec_line or MARKER in exec_line:
        return False
    program = entry.exec_program()
    if not program:
        return False
    if program.startswith("/"):
        path = Path(program)
        # 官方入口几乎总在 /usr/bin、/usr/lib/<app>/ 或 /opt/<app>/；
        # 出现在用户目录下说明是某人写的包装脚本。
        user_roots = (
            Path.home() / ".local",
            Path.home() / "bin",
            Path.home() / ".cargo",
            Path.home() / "project",
        )
        for root in user_roots:
            try:
                path.relative_to(root)
                return True
            except ValueError:
                continue
    return False


def _normalize_exec_name(value: str) -> str:
    """把 ``Exec`` 归一化成可比较的程序名，用于跨入口比对。"""

    program = _split_exec(value)[0]
    if program.startswith("/"):
        # 用文件名比较：/usr/bin/chatgpt 与 chatgpt 应视为同一个程序。
        return Path(program).name
    return program
def _target_program(entry: DesktopEntry) -> str:
    """取入口「真正要启动的程序名」，穿透本工具写入的包装。

    已接管的入口 Exec 形如 ``<launcher> --launch-command "<原命令>"``，
    直接拿 Exec 首段比较会永远匹配不上目标程序，导致重复接管判断失效。
    """

    if entry.is_managed:
        match = _LAUNCH_COMMAND_RE.search(entry.exec_line)
        if match:
            # 带引号与无引号两种写法二选一命中。
            inner = match.group("quoted")
            if inner is None:
                inner = match.group("bare") or ""
            return _normalize_exec_name(inner)
    return _normalize_exec_name(entry.exec_line)


def _matches_target(entry: DesktopEntry, config: Config) -> bool:
    """判断入口是否指向配置里的目标应用。

    三条判据任一成立即可：
    1. Exec 里的程序名等于 ``app_command``（官方入口）；
    2. 已由本工具接管，且内部原始命令指向目标（便于二次运行时识别）；
    3. ``StartupWMClass`` 或 ``Name`` 命中配置的 ``target_wm_class``
       —— 用于识别被第三方脚本换过 Exec 的包装入口。
    """

    if _target_program(entry) == _normalize_exec_name(config.app_command):
        return True

    wm_class = config.target_wm_class.strip().lower()
    if wm_class:
        if entry.startup_wm_class.strip().lower() == wm_class:
            return True
        if entry.name.strip().lower() == wm_class:
            return True
    return False


def build_plan(config: Config, *, directories: Optional[List[Path]] = None) -> List[EntryPlan]:
    """列出所有候选入口及处理意见，不修改任何文件。"""

    dirs = directories if directories is not None else xdg_application_dirs()
    excluded = {name.strip() for name in config.exclude_entrypoints if name.strip()}

    plans: List[EntryPlan] = []
    # 同名入口（例如 chatgpt.desktop 同时存在于用户级与系统级）只处理最靠前的那个，
    # 这正是 XDG 的覆盖语义：后面的会被前面的遮蔽。
    seen_names: set[str] = set()

    for directory in dirs:
        for path in _iter_desktop_files(directory):
            entry = parse_desktop_entry(path)
            if entry is None:
                continue

            if not _matches_target(entry, config):
                continue

            if path.name in seen_names:
                plans.append(EntryPlan(entry, EntryPlan.SKIP, "已被同名入口遮蔽"))
                continue
            seen_names.add(path.name)

            if path.name in excluded:
                plans.append(EntryPlan(entry, EntryPlan.SKIP, "列在 ExcludeEntrypoints"))
                continue
            if entry.is_managed:
                plans.append(EntryPlan(entry, EntryPlan.ALREADY, "Exec 已含本工具标记"))
                continue
            if entry.is_hidden:
                plans.append(EntryPlan(entry, EntryPlan.SKIP, "Hidden=true"))
                continue
            if entry.is_nodisplay:
                plans.append(EntryPlan(entry, EntryPlan.SKIP, "NoDisplay=true"))
                continue
            if looks_wrapped(entry) and not config.include_wrapped_entrypoints:
                plans.append(EntryPlan(entry, EntryPlan.SKIP, "已被第三方脚本包装"))
                continue
            if not program_exists(entry):
                plans.append(EntryPlan(entry, EntryPlan.SKIP, "指向的程序不存在"))
                continue
            if not os.access(path, os.W_OK):
                plans.append(EntryPlan(entry, EntryPlan.READONLY, "文件不可写"))
                continue

            plans.append(EntryPlan(entry, EntryPlan.ADOPT, "可接管"))

    return plans


def _rewrite_exec(raw: str, new_value: str) -> str:
    """只替换 ``[Desktop Entry]`` 组内的 ``Exec=`` 行，其余原样保留。"""

    lines = raw.splitlines(keepends=True)
    out: List[str] = []
    section: Optional[str] = None
    replaced = False

    for line in lines:
        section_match = _SECTION_RE.match(line.rstrip("\r\n"))
        if section_match:
            section = section_match.group("name").strip()
            out.append(line)
            continue
        if section == "Desktop Entry" and not replaced:
            key_match = _KEY_RE.match(line.rstrip("\r\n"))
            if key_match and key_match.group("key") == "Exec":
                # 保留原行的行尾符，避免把 CRLF 文件改成 LF。
                ending = line[len(line.rstrip("\r\n")) :]
                lead = key_match.group("lead")
                out.append(f"{lead}Exec={new_value}{ending}")
                replaced = True
                continue
        out.append(line)

    if not replaced:
        # 理论上不会发生：parse 阶段已确认 Exec 存在。
        raise ValueError("未找到 Exec 行，拒绝改写")
    return "".join(out)


def backup_path(path: Path) -> Path:
    return path.with_name(path.name + BACKUP_SUFFIX)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_command_path(path: str) -> str:
    """按 Desktop Entry 规范为路径加引号（含空格时必须加）。"""

    if any(ch in path for ch in " \t\"'\\$`"):
        return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return path


def _quote_argument(value: str) -> str:
    """把整段命令包成单个参数，供桌面环境完整传递。"""

    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _build_exec(config: Config, entry: DesktopEntry) -> str:
    """构造新的 ``Exec``：本工具启动器 + 原入口的完整原始命令。

    把原命令整体作为参数透传，而不是让启动器自己再解析一次：
    入口里可能带 ``--profile-directory``、``%U`` 之类我们不该理解的东西，
    原样交回去最安全。
    """

    launcher = config.resolve_launcher_command()
    original = entry.exec_line
    if not original:
        raise ValueError("原入口没有 Exec，拒绝接管")
    return f"{_quote_command_path(launcher)} --launch-command {_quote_argument(original)}"


def adopt(config: Config, plan: EntryPlan) -> Path:
    """接管单个入口：先备份原始文件，再写入新的 ``Exec``。

    返回备份文件路径。若已存在备份则不覆盖——那份备份对应的是最原始的
    文件内容，重复接管时若用当前内容覆盖它，回滚就会回到半接管状态。
    """

    path = plan.entry.path
    backup = backup_path(path)
    if not backup.exists():
        shutil.copy2(path, backup)
        log(f"entrypoints:backup path={backup} sha256={sha256_of(backup)[:12]}")

    new_exec = _build_exec(config, plan.entry)
    new_raw = _rewrite_exec(plan.entry.raw, new_exec)

    # 原子替换：先写临时文件再 rename，避免中途崩溃留下半截文件。
    # 用 newline="" 原样写出，保住文件原本的换行风格。
    tmp = path.with_name(path.name + ".tmp-dcb-linux")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        handle.write(new_raw)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    log(f"entrypoints:adopt path={path} exec={new_exec}")
    return backup


def restore(config: Config, *, directories: Optional[List[Path]] = None) -> List[Path]:
    """把已接管的入口恢复原状，返回被恢复的文件列表。"""

    dirs = directories if directories is not None else xdg_application_dirs()
    restored: List[Path] = []
    for directory in dirs:
        for path in _iter_desktop_files(directory):
            backup = backup_path(path)
            if not backup.exists():
                continue
            shutil.copy2(backup, path)
            backup.unlink()
            log(f"entrypoints:restore path={path}")
            restored.append(path)
    return restored


def verify(config: Config, *, directories: Optional[List[Path]] = None) -> List[str]:
    """检查接管状态是否自洽，返回中文问题列表（空表示通过）。"""

    launcher = config.resolve_launcher_command()
    problems: List[str] = []
    for plan in build_plan(config, directories=directories):
        path = plan.entry.path
        backup = backup_path(path)
        if plan.action == EntryPlan.ALREADY:
            if not backup.exists():
                problems.append(f"{path.name} 已接管但缺少备份文件 {backup.name}")
            elif not plan.entry.exec_line.startswith(_quote_command_path(launcher)):
                problems.append(f"{path.name} 的 Exec 未指向本工具（{launcher}）")
        elif plan.action == EntryPlan.ADOPT and backup.exists():
            problems.append(f"{path.name} 存在遗留备份但尚未接管，建议先执行回滚")
    return problems
