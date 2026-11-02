# Dragon Codex Boot for Linux

> 本仓库是 [yushua0808-cpu/dragon-codex-boot](https://github.com/yushua0808-cpu/dragon-codex-boot)
> 的 **Linux 移植版**。启动动画的创意与素材、三阶段时间轴模型、`ScreenFrames`
> 标定机制、窗口交接的交互理念，全部来自上游作者 **yushua0808-cpu**
> （Dragon Codex Boot maintainers）。原创设计版权归原作者所有。
> 详见 [NOTICE.md](NOTICE.md)。

把上游 [dragon-codex-boot](https://github.com/yushua0808-cpu/dragon-codex-boot)（Windows WPF + macOS Swift）
的启动动画启动器移植到 **Ubuntu / X11 / GNOME**。

上游 Windows 版以 WPF + DWM 实现，macOS 版以 Swift 实现；本移植版在 Linux 上用
PyQt6 + XComposite + libmpv 重新实现同一套交互，**不复制上游代码**，仅复用设计约定与素材。

本移植只依赖 **PyQt6 + libmpv + python-xlib**，不引入 numpy / Pillow。

## 特性

- **三档嵌入降级**：`composite` → `shm` → `fade`，运行时实测能力后自动选档
- **精确几何对齐**：按 `ScreenFrames` 标定把真实客户区对齐到动画里的屏幕区域
- **桌面入口接管**：只改 `.desktop` 的 `Exec=` 一行，其余原样保留，可一键还原
- **隐私边界与上游一致**：不读窗口标题、不截图落盘
- **中文日志**：全链路中文日志，便于排查

## 环境要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Ubuntu | 24.04 及以上 | 需要 X11（GNOME 默认会话即可） |
| Python | 3.10+ | 使用项目内 `.venv` |
| PyQt6 | 6.12.0 | 走 pip 安装 |
| libmpv | 0.37+ | 系统包 `libmpv2` |
| 合成器 | mutter | 需要 `_NET_WM_CM_S0` selection owner |

> **仅支持 X11**。Wayland 会话下 X11 拦截窗口几何的能力受限，请切换到 Xorg 会话。

## 安装

### 方式一：deb 包（推荐）

从 [Releases](https://github.com/jaydocoder/dragon-codex-boot-linux/releases) 下载 `.deb`，双击用软件中心安装，或：

```bash
sudo apt-get install -y ./dragon-codex-boot_0.1.0_all.deb
```

每次发布都附带 `SHA256SUMS`，下载后可核对：

```bash
sha256sum -c SHA256SUMS
```

装完后**应用菜单里直接出现「ChatGPT（启动动画）」，双击即可**播放动画并拉起 ChatGPT。

依赖由 apt 自动解决（`python3-pyqt6`、`python3-xlib`、`python3-mpv`、`libmpv2`、`x11-utils`），
不自带 Python 运行时，也不再往家目录装 `.venv`。

首次运行时会在 `~/.config/dragon-codex-boot/launcher.json` 生成配置，
日志写到 `~/.local/state/dragon-codex-boot/logs/`，动画素材在 `/usr/share/dragon-codex-boot/media/`。

想让**官方「ChatGPT」图标**也走启动动画（而不是新增的那个图标）：

```bash
dragon-codex-boot entries --plan      # 先预览会改哪些文件
dragon-codex-boot entries --adopt     # 确认后接管（只改 Exec= 一行）
dragon-codex-boot entries --restore   # 一键还原
```

> 安装脚本**不会**自动接管官方图标：`chatgpt.desktop` 属于 `chatgpt` deb 包，
> 官方升级会覆盖；机器上也可能已有别的启动器包装。是否接管交给你决定。
> 卸载本包时会自动 `entries --restore`，不留死链。

卸载：

```bash
sudo apt-get remove dragon-codex-boot
```

### 方式二：源码运行

```bash
git clone https://github.com/jaydocoder/dragon-codex-boot-linux.git
cd dragon-codex-boot-linux

# 依赖自检（不会改动系统）
./scripts/install.sh --check

# 创建 .venv 并安装 Python 依赖
./scripts/install.sh
```

`install.sh` 会：

1. 检查 Python 版本与 `libmpv.so`，并确认有可用的 X11 `DISPLAY`
2. 创建（或复用）`.venv` 并安装 `requirements.txt`
3. 若 `launcher.json` 不存在，从 `config/launcher.linux.example.json` 生成一份
4. 做一次模块导入自检

源码方式下用 `./run.sh` 调用；deb 方式下命令是 `dragon-codex-boot`。

## 使用

```bash
# 播放启动动画并拉起应用
./run.sh run

# 由桌面入口调用时透传原始命令
./run.sh run --launch-command "/usr/lib/chatgpt/ChatGPT %U"

# 跳过环境预检（排查问题时用）
./run.sh run --skip-preflight
```

动画播放期间按 `Esc` 或点右上角按钮可跳过，直接进入应用。

## 接管桌面入口

```bash
# 先看会改哪些文件（不改动任何内容）
./run.sh entries --plan

# 接管匹配到的入口
./run.sh entries --adopt

# 检查当前接管状态
./run.sh entries --verify

# 一键还原（从 .dcb-linux.bak 恢复）
./run.sh entries --restore
```

接管时只修改 `[Desktop Entry]` 组内的 `Exec=` 一行，并把原命令整体写成
`--launch-command "<原命令>"` 透传，其余字段、顺序、注释原样保留。

> 默认**不接管** `chatgpt.desktop`（配置里 `ExcludeEntrypoints`），
> 因为它已被其它启动器包装，重复接管会导致链路打架。

## 配置

配置为 `launcher.json`，字段名与上游 PascalCase 完全兼容。

关键字段：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `Video` | `media/startup.mp4` | 启动动画，相对路径基于配置目录 |
| `AppCommand` | `/usr/bin/chatgpt` | 目标应用可执行文件 |
| `ProcessNames` | `["chatgpt","ChatGPT"]` | 用于在 `/proc` 中定位进程 |
| `TargetWMClass` | `Chatgpt` | 主窗口 `WM_CLASS` 提示 |
| `HoldAt` | `11.3` | 播到此处暂停，等应用窗口就绪 |
| `TransitionStart` | `12.7` | 过渡开始时刻（秒） |
| `TransitionEnd` | `13.65` | 过渡结束时刻（秒） |
| `MaxWaitSeconds` | `60` | 等待应用窗口的上限 |
| `EmbedMode` | `auto` | `auto` / `composite` / `shm` / `fade` |
| `EmbedFps` | `30` | 帧驱动频率 |
| `ScreenFrames` | 见示例 | 屏幕区域标定帧 |

详见 [docs/configuration.md](docs/configuration.md)。

## 标定

`ScreenFrames` 决定"动画里的屏幕区域"在什么时刻处于什么位置。默认值已在
1920×1080 下校准，更换分辨率或动画后需要重新标定：

```bash
./run.sh calibrate --seed-from media/startup.mp4
```

标定是**种子校验工具**：没有种子时它绝不猜边界，只报告 `diff` / `none` 并把
结果标记为不可信。详见 [docs/calibration.md](docs/calibration.md)。

## 文档

- [安装与部署](docs/installation.md)
- [配置项说明](docs/configuration.md)
- [标定流程](docs/calibration.md)
- [故障排查](docs/troubleshooting.md)

## 测试

```bash
./scripts/test.sh          # 全量
./scripts/test.sh -q       # 静默
./scripts/test.sh tests/test_geometry.py -v
```

测试在 `QT_QPA_PLATFORM=offscreen` 下运行，不需要真实 X 服务器。

## 发布流程

`.github/workflows/build-deb.yml` 自动完成构建与发布，无需本地打包：

| 触发 | 行为 |
| --- | --- |
| 推送 `v*` 标签 | 跑测试 → 构建 deb → 在容器内实测安装 → 创建 GitHub Release 并附上 deb 与 `SHA256SUMS` |
| 手动触发（Actions 页面） | 只跑测试与构建，产物作为 artifact 保留 30 天，不发布 |

构建前的三道关：

1. **单元测试**：258 项全部通过才继续
2. **包结构断言**：逐项检查 `copyright`、`NOTICE.md`、desktop 文件、素材、代码是否都在包里，并校验包内版本号与 tag 一致
3. **容器实测安装**：在干净的 `ubuntu:24.04` 里 `apt-get install` 一遍，验证依赖可解析、`--version` 能跑、desktop 文件合法、卸载干净

发一个版本：

```bash
# 同步 VERSION 与 tag，避免 tag 与包内版本不一致
echo "0.2.0-linux" > VERSION
git commit -am "release: 0.2.0"
git tag v0.2.0
git push origin main --tags
```

本地想提前验证容器安装，跑同一份脚本：

```bash
./scripts/verify-deb-in-container.sh
```

## 与上游的差异

| 方面 | 上游（Windows/macOS） | 本移植（Linux/X11） |
| --- | --- | --- |
| 窗口嵌入 | `SetParent` / `NSView` | XComposite 重定向，降级为 shm 采样 |
| 窗口查找 | `EnumWindows` | `/proc` + EWMH `WM_CLASS` |
| 几何对齐 | `SetWindowPos` | `ConfigureRequest` + `_NET_FRAME_EXTENTS` |
| 前台激活 | `SetForegroundWindow` | `_NET_ACTIVE_WINDOW` + 结果确认重试 |
| 入口接管 | 快捷方式 | `.desktop` 的 `Exec=` 单行改写 |

## 致谢

感谢 **[yushua0808-cpu](https://github.com/yushua0808-cpu)** 创作了上游
[Dragon Codex Boot](https://github.com/yushua0808-cpu/dragon-codex-boot)，
并选择以 MIT 许可开源。启动动画本身、三阶段时间轴的设计思路、`ScreenFrames`
标定模型以及"动画窗口变成真应用"的交接体验，都是原作者的工作成果。
没有上游这份设计与素材，本移植版无从谈起。

## 许可

本项目沿用上游的 **MIT License**，见 [LICENSE](LICENSE)。

- 来源与署名归属：[NOTICE.md](NOTICE.md)
- 第三方依赖与商标：[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
- 动画素材许可边界：[media/MEDIA_NOTICE.md](media/MEDIA_NOTICE.md)

**本项目是独立社区工具，与 OpenAI 无隶属关系，未获其授权或背书。**
