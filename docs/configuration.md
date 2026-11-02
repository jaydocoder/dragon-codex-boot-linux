# 配置项说明

配置文件名 `launcher.json`，默认放在项目根目录，可用 `--config` 指定其它路径。
字段名与上游 Windows / macOS 版**完全一致**（PascalCase），便于共用配置。

## 字段表

| 字段 | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `DisplayName` | string | `Dragon Codex Boot` | 界面显示名 |
| `Video` | string | `media/startup.mp4` | 启动动画路径，相对路径基于配置目录 |
| `AppCommand` | string | `/usr/bin/chatgpt` | 目标应用可执行文件 |
| `ProcessNames` | string[] | `["chatgpt","ChatGPT"]` | 在 `/proc` 中匹配的进程名 |
| `ProcessPathContains` | string | `/chatgpt/` | 进程可执行路径必须包含的子串 |
| `TargetWMClass` | string | `Chatgpt` | 主窗口 `WM_CLASS` 提示 |
| `HoldAt` | float | `11.3` | 播放到此时刻暂停，等待应用窗口 |
| `TransitionStart` | float | `12.7` | 过渡开始时刻（秒） |
| `TransitionEnd` | float | `13.65` | 过渡结束时刻（秒） |
| `MaxWaitSeconds` | float | `60` | 等待应用窗口的最长时间 |
| `Volume` | float | `1.0` | 音量，`0.0`–`1.0` |
| `PlayerWidth` | int | `1920` | 播放窗口宽度 |
| `PlayerHeight` | int | `1080` | 播放窗口高度 |
| `MatchClientToPlayer` | bool | `true` | 是否把客户区对齐到动画屏幕区域 |
| `EmbedMode` | string | `auto` | `auto` / `composite` / `shm` / `fade` |
| `EmbedFps` | int | `30` | 帧驱动频率 |
| `AutoReplaceEntrypoints` | bool | `false` | 是否自动接管桌面入口 |
| `IncludeWrappedEntrypoints` | bool | `false` | 是否接管已被其它启动器包装的入口 |
| `ExcludeEntrypoints` | string[] | `["chatgpt.desktop"]` | 永不接管的入口文件名 |
| `ScreenFrames` | object[] | 见下 | 屏幕区域标定帧 |

## 时间轴

时间轴三段的关系必须满足：

```
0 ────────── HoldAt ────── TransitionStart ──── TransitionEnd ──── 视频结束
   播放动画     暂停等应用      过渡进行           对齐完成、交接
```

配置加载时强制校验 `0 < HoldAt < TransitionStart < TransitionEnd`，
违反任一条都会抛 `ConfigError` 并列出全部问题，不会带着错误时间轴运行。

- `TransitionStart` 到 `TransitionEnd` 之间应落在动画"镜头拉近"的区间
- `TransitionEnd` 一般等于或略早于视频结束

## 嵌入模式

| 模式 | 原理 | 要求 |
| --- | --- | --- |
| `composite` | XComposite `RedirectManual` + 逐帧合成 | 合成器不占用该窗口 |
| `shm` | 读回客户区像素 + `QImage` 叠加 | 无（默认可用） |
| `fade` | 直接对齐窗口，靠播放窗口淡出遮罩 | 无 |

`auto` 的顺序是 `composite` → `shm` → `fade`，逐档实测能力后选择。

> **实测结论（Ubuntu 24.04 + mutter）**：`RedirectManual` 会返回 `BadAccess`，
> 因为 mutter 自己已持有 `RedirectAutomatic`。因此 `composite` 档在本环境下
> **不可用**，`auto` 会稳定降级到 `shm`。这不是缺陷，是预期行为。

## ScreenFrames

每一个采样帧描述"动画里的屏幕区域"在某一时刻占画面的比例：

```json
{ "Time": 12.65, "X": 0.123, "Y": 0.094, "Width": 0.736, "Height": 0.725 }
```

- 全部为**相对画面尺寸**的比例（0–1）
- 实际像素 = 播放窗口尺寸 × 比例
- 相邻帧之间线性插值，`Time` 必须递增
- 首帧之前取首帧，末帧之后取末帧

默认值在 1920×1080 下标定，换算出的黄金值可逐点复核：

| 时刻 | 进度 | 客户区像素矩形（1920×1080） |
| --- | --- | --- |
| 12.65 | 0.0 | `(236, 102, 1413, 783)` |
| 12.65 | 0.5 | `(118, 51, 1666, 932)` |
| 13.00 | 0.0 | `(142, 66, 1622, 891)` |
| 13.00 | 0.5 | `(71, 33, 1771, 986)` |
| 13.50 | 任意 | `(0, 0, 1920, 1080)` |

换分辨率或换动画后必须重新标定，见 [calibration.md](calibration.md)。

## 桌面入口相关

`ExcludeEntrypoints` 里的文件名按 basename 精确匹配。默认排除 `chatgpt.desktop`，
因为它已被 `chatgpt-codexhost-launcher` 包装；再接管一次会让两条调用链互相打架。

`IncludeWrappedEntrypoints=false` 表示：`Exec=` 里已经带 `--launch-command`
（或类似包装痕迹）的入口一律跳过。

## 完整示例

见 `config/launcher.linux.example.json`。
