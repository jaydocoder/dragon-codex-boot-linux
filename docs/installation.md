# 安装与部署

## 前置条件

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

## 安装步骤

```bash
cd /home/neo/project/java/githubProject/dragon-codex-boot-linux

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
│   └── log.py                日志
├── scripts/
│   ├── install.sh            安装与环境自检
│   └── test.sh               测试
├── tests/                    pytest 用例
└── logs/launcher.log         运行日志
```
