# 故障排查

所有运行日志写入 `logs/launcher.log`，全中文。排查时先看这个文件。

```bash
tail -50 logs/launcher.log
```

## 快速诊断

```bash
# 环境自检
./scripts/install.sh --check

# 桌面入口接管状态
./run.sh entries --verify

# 跳过预检直接跑，看真实报错
./run.sh run --skip-preflight
```

## 常见问题

### 播放窗口尺寸不对（比如 2300×1294 而不是 1920×1080）

**原因**：Qt 按 `Xft.dpi` 自动做了高 DPI 缩放。`ScreenFrames` 是按视频原始
1920×1080 像素标定的，一旦缩放，Qt 坐标就不再等于 X11 像素，整条几何链路错位。

**检查**：

```bash
xrdb -query | grep Xft.dpi     # 例如 115 → 缩放 1.1979
```

**解决**：`run.sh` 和 `ensure_app()` 已经双处钉死：

```bash
QT_SCALE_FACTOR=1
QT_ENABLE_HIGHDPI_SCALING=0
QT_AUTO_SCREEN_SCALE_FACTOR=0
```

如果绕开 `run.sh` 直接调 Python，请自己补上这三个变量。

### 日志里出现 `embedder:x-error codes=[-1]` / `BadAccess`

**这是正常的**。它表示 `auto` 档在尝试 `composite` 时失败，随后降级到 `shm`：

```
embedder:capability notes=合成器已接管该窗口的重定向，无法抢占（X 返回 BadAccess）
embedder:select requested=auto mode=shm
```

mutter 自己持有 `RedirectAutomatic`，用户态无法再抢 `RedirectManual`。
只有当你**强制** `"EmbedMode": "composite"` 时，这条才是真故障。

### 卡在等待应用（`launcher:phase WAITING_APP`）

```bash
# 确认进程起来了
pgrep -af ChatGPT

# 确认窗口存在且 WM_CLASS 正确
xdotool search --class Chatgpt
xprop -id <窗口号> WM_CLASS
```

对照 `launcher.json`：

- `ProcessNames` / `ProcessPathContains` 是否匹配实际进程
- `TargetWMClass` 是否与实际 `WM_CLASS` 一致（区分大小写）

超过 `MaxWaitSeconds` 仍未就绪，会进 `FAILED_APP` 并直接拉起应用。

### 过渡卡住不动

看日志里有没有 `session:resume`：

```
launcher:phase TRANSITION
session:resume          ← 必须有这一行
session:aligned ok=True
```

没有 `session:resume` 说明播放没被恢复，播放位置会一直停在 `HoldAt`，
过渡永远推进不下去。

### 动画放完了但焦点没给到应用

看这两行：

```
session:handoff-activate ok=True
```

`ok=False` 表示窗口管理器（mutter）忽略了 `_NET_ACTIVE_WINDOW` 请求。
`_activate_confirmed()` 会重试到预算耗尽；仍失败通常是：

- 目标窗口当时仍带 `_NET_WM_STATE_HIDDEN`（被别的窗口盖住）
- 有其它程序在同时抢焦点

日志里出现 `session:handoff-late-activate` 是正常的兜底路径，不是错误。

### `session:handoff-relookup window=0x0`

嵌入层从未 attach（应用识别太慢），并且交接时也没现找到窗口。
此时播放窗口会直接消失，应用不会被拉到前台。检查 `TargetWMClass` 与进程匹配。

### 进程跑完后不退出

正常情况下日志末尾会有：

```
cli:done handed_off=True
```

Qt 只在"最后一个窗口关闭"时自动退出事件循环，而会话里还挂着定时器等非窗口
对象，所以必须由 `finished=app.quit` 显式退出。若卡住，确认入口传了这个回调。

### `Cannot load libcuda.so.1`

libmpv 的提示，软件渲染下必然出现，**可忽略**。

### `Gtk-WARNING Theme parsing error`

系统 GTK 主题的问题，与本程序无关，**可忽略**。

### `运行环境检查未通过：找不到 list libmpv`

```bash
sudo apt install libmpv2
ldconfig -p | grep libmpv     # 确认 libmpv.so.2 存在
```

### 报"配置文件不是合法 JSON"

```bash
python3 -m json.tool launcher.json > /dev/null
```

常见原因是手改时漏了逗号或用了中文引号。可以直接从模板重新拷一份：

```bash
cp config/launcher.linux.example.json launcher.json
```

### `配置校验失败：必须满足 HoldAt < TransitionStart`

时间轴顺序错了，见 [configuration.md](configuration.md) 的"时间轴"一节。

### Wayland 会话下完全不工作

本移植依赖 X11 的窗口几何读写，Wayland 下拿不到其它窗口的坐标。
在登录界面选择 **Ubuntu on Xorg** 会话后重新登录。

## 收集诊断信息

报问题时请附上：

```bash
echo "会话类型: $XDG_SESSION_TYPE"
echo "DISPLAY: $DISPLAY"
xrandr | grep -w connected
xrdb -query | grep -i dpi
ldconfig -p | grep libmpv
tail -100 logs/launcher.log
```
