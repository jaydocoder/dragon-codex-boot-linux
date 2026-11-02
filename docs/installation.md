# 安装与部署

## deb 包安装（推荐）

### 安装

从 [Releases](https://github.com/jaydocoder/dragon-codex-boot-linux/releases) 下载 `.deb` 后：

```bash
# 软件中心双击安装，或命令行：
sudo apt-get install -y ./dragon-codex-boot_0.1.0_all.deb
```

装完应用菜单里出现 **ChatGPT（启动动画）**，双击即用。依赖由 apt 自动安装，
无需手动建 `.venv`。

### 包内布局

| 路径 | 内容 |
| --- | --- |
| `/usr/bin/dragon-codex-boot` | 命令入口 |
| `/usr/lib/dragon-codex-boot/src/` | Python 源码 |
| `/usr/share/dragon-codex-boot/media/startup.mp4` | 启动动画 |
| `/usr/share/dragon-codex-boot/launcher.example.json` | 配置模板 |
| `/usr/share/applications/dragon-codex-boot.desktop` | 应用菜单项 |
| `/usr/share/doc/dragon-codex-boot/` | 版权、署名与文档 |

### 用户级文件（不属于包管理）

| 路径 | 内容 |
| --- | --- |
| `~/.config/dragon-codex-boot/launcher.json` | 用户配置，首次运行时生成 |
| `~/.local/state/dragon-codex-boot/logs/` | 运行日志 |

卸载时这两个目录**不会被删除**（配置属于你自己的数据）。需要清理时手动删除即可。

### 为什么不自动接管官方「ChatGPT」图标

`/usr/share/applications/chatgpt.desktop` 属于 `chatgpt` deb 包，官方升级会覆盖；
机器上也可能已有第三方启动器包装（例如 `chatgpt-codexhost-launcher`）。
自动改写会在用户不知情的情况下改掉别人的东西，因此 `postinst` 只打印提示。

需要时显式执行：

```bash
dragon-codex-boot entries --plan      # 预览
dragon-codex-boot entries --adopt     # 接管
dragon-codex-boot entries --restore   # 还原
```

`apt-get remove` 时 `prerm` 会枚举 `/home/*` 与 `/root` 下的真实用户，
分别以该用户身份执行 `entries --restore`，不会在家目录里留下 root 拥有的文件。
`apt-get upgrade` 升级本包时**不**还原，避免双击图标时中间出现一次没有动画的窗口。

### 为什么不自带 Python 运行时

曾评估过 PyInstaller / 内置 venv 方案，实测 `.venv` 约 303 MB，其中 273 MB 是
PyQt6 私有的 Qt 库副本（含 libavcodec 等）。这会带来三个问题：

1. 包体积从 22 MB 涨到 300 MB 以上；
2. 包里那份 Qt 会和系统 `libmpv2` 的 ABI 产生冲突风险；
3. 那份 Qt 永不更新，安全修复收不到。

而本项目实际缺的两个 Python 包（`python3-xlib`、`python3-mpv`）都是**纯 Python**
（`Depends: python3:any`），apt 直接可装。因此采用标准 Debian 做法：依赖交给 apt。

### 从源码自行打包

```bash
./packaging/build-deb.sh              # 产物在 dist/
./packaging/build-deb.sh --install    # 构建后直接安装
```

## 前置条件（源码方式）

| 组件 | 检查命令 | 期望 |
| --- | --- | --- |
| X11 会话 | `echo $XDG_SESSION_TYPE` | `x11` |
| DISPLAY | `echo $DISPLAY` | `:0` 之类 |
| 合成器 | `xprop -root _NET_SUPPORTING_WM_CHECK` | 有输出 |
| libmpv | `ldconfig -p \| grep libmpv` | 有 `libmpv.so.2` |
| Python | `python3 --version` | 3.10+ |

> **Wayland 不受支持**：本移植依赖读取和改写 X11 窗口的绝对几何，
> Wayland 下客户端拿不到其它窗口的坐标。请在登录界面选择
> "Ubuntu on Xorg" 会话。

> 下面几节是**源码方式**。用 deb 包安装的话可以跳过，直接看
> [deb 包安装](#deb-包安装推荐)。

## 源码方式安装步骤

```bash
git clone https://github.com/jaydocoder/dragon-codex-boot-linux.git
cd dragon-codex-boot-linux

# 1. 环境自检（只读，不改动任何东西）
./scripts/install.sh --check

# 2. 创建 .venv 并装依赖
./scripts/install.sh
```

`--check` 只做只读检查，输出类似：

```
== 检查系统依赖 ==
  libmpv：已安装
  X11：可用（:0）
检查完成（未安装任何内容）。
```

它校验 Python ≥ 3.10、`libmpv.so`，并在有 `DISPLAY` 时确认 X11 可用；
缺 `libmpv` 会直接报错并提示 `sudo apt install libmpv2`。

## 为什么用项目内 `.venv`

Ubuntu 24.04 的系统 Python 受 PEP 668 保护，禁止直接 `pip install`。
项目内 `.venv` 既绕开这个限制，又不会污染系统环境。

`run.sh` 会自动优先使用 `.venv/bin/python`，找不到时才退回系统 Python
（此时会因为缺依赖而报错，请先跑 `install.sh`）。

## 部署到桌面

### 方式一：手动建快捷方式

```bash
cat > ~/.local/share/applications/dragon-codex-boot.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Dragon Codex Boot
Exec=$(pwd)/run.sh run
Terminal=false
Categories=Utility;
EOF
```

### 方式二：接管现有入口

如果系统里已有 ChatGPT 的桌面入口，可以让本工具接管它：

```bash
./run.sh entries --plan     # 先看会改什么
./run.sh entries --adopt    # 确认后再接管
```

接管只改 `Exec=` 一行，原命令作为 `--launch-command` 透传。
随时可以 `./run.sh entries --restore` 一键还原。

## 升级

```bash
git pull
./scripts/install.sh          # 幂等，已存在的 .venv 会增量更新依赖
```

配置文件 `launcher.json` 不会被覆盖，新增字段会按默认值补齐。

## 卸载

### deb 方式

```bash
sudo apt-get remove dragon-codex-boot
```

`prerm` 会自动还原被接管的桌面入口。用户配置与日志保留在
`~/.config/dragon-codex-boot/` 与 `~/.local/state/dragon-codex-boot/`，
需要彻底清理时手动删除。

### 源码方式

```bash
./run.sh entries --restore        # 先还原接管的入口
rm -rf .venv logs                 # 清理本地状态
rm -f ~/.local/share/applications/dragon-codex-boot.desktop
```

## 目录结构

```
dragon-codex-boot-linux/
├── run.sh                    启动脚本（钉死 Qt 1:1 缩放）
├── launcher.json             用户配置
├── config/
│   └── launcher.linux.example.json   配置模板
├── media/
│   └── startup.mp4           启动动画
├── src/
│   ├── __main__.py           CLI 入口
│   ├── config.py             配置加载
│   ├── geometry.py           几何换算与 screenFrames 插值
│   ├── x11.py                X11 窗口交互
│   ├── watcher.py            应用窗口探测
│   ├── video.py              libmpv 封装
│   ├── player.py             播放窗口
│   ├── embedder.py           三档嵌入
│   ├── launcher.py           状态机
│   ├── session.py            运行层串联
│   ├── entrypoints.py        .desktop 接管
│   ├── calibrate.py          标定校验
│   ├── paths.py              XDG 路径（源码/安装两种形态）
│   └── log.py                日志
├── scripts/
│   ├── install.sh            安装与环境自检
│   └── test.sh               测试
├── packaging/                deb 打包
│   ├── build-deb.sh          构建脚本
│   ├── debian/               control.in / postinst / prerm / postrm
│   ├── usr-bin-dragon-codex-boot    /usr/bin 入口
│   └── dragon-codex-boot.desktop    应用菜单项
├── dist/                     构建产物（不入库）
├── tests/                    pytest 用例
└── logs/launcher.log         运行日志
```

另有 `src/paths.py`：区分源码形态与安装形态，按 XDG 规范定位配置、
状态目录与动画素材，使同一份代码在 `.venv` 源码运行和 `/usr/lib` 安装运行下都能工作。
