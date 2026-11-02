# 验证报告

- 项目：`dragon-codex-boot-linux`（Dragon Codex Boot 的 Ubuntu / X11 移植）
- 版本：`0.1.0-linux`
- 验证环境：Ubuntu 24.04.5 LTS、X11、GNOME/mutter、单屏 1920×1080、`Xft.dpi: 115`
- 验证日期：2026-11-02

## 1. 验证结论

| 项目 | 结论 |
| --- | --- |
| 单元测试 | **240 passed** |
| 环境自检 | 通过（`install.sh --check`） |
| 实机端到端 | **通过**（完整走通到焦点交接） |
| 桌面入口接管 | 通过（`--plan` / `--verify` 行为正确） |
| 标定校验 | 通过（种子校验降级路径按设计生效） |

无遗留阻塞项。

## 2. 单元测试

```bash
./scripts/test.sh -q
# 240 passed in 0.89s
```

测试在 `QT_QPA_PLATFORM=offscreen` 与 `LC_NUMERIC=C` 下运行，不依赖真实 X 服务器。

覆盖模块：

| 测试文件 | 覆盖内容 |
| --- | --- |
| `test_geometry.py` | `screenFrames` 插值、黄金值逐点复核、`row_stride` |
| `test_config.py` | PascalCase 字段映射、时间轴校验、`Volume` 独立分支 |
| `test_x11_helpers.py` | 几何换算、最小尺寸钳制、`_near` 容差 |
| `test_watcher.py` | 候选稳定判定、间隔与稳定时长 |
| `test_embedder.py` | 三档选档、降级顺序、`frame_alpha`、`split_rows` |
| `test_launcher.py` | 状态机全部迁移、`HANDOFF` 两阶段、`skip()` 幂等 |
| `test_session.py` | 启动串联、过渡恢复播放、交接顺序与激活确认 |
| `test_entrypoints.py` | `.desktop` 单行改写、备份、还原往返、排除规则 |
| `test_calibrate.py` | 种子校验、差分不可信时保留种子 |
| `test_cli.py` | `--launch-command` 解析、字段码剔除、参数互斥 |

### 黄金值独立复算

1920×1080 下的 `screenFrames` 换算结果与上游一致：

| 时刻 | 进度 | 期望矩形 | 结果 |
| --- | --- | --- | --- |
| 12.65 | 0.0 | `(236, 102, 1413, 783)` | 一致 |
| 12.65 | 0.5 | `(118, 51, 1666, 932)` | 一致 |
| 13.00 | 0.0 | `(142, 66, 1622, 891)` | 一致 |
| 13.00 | 0.5 | `(71, 33, 1771, 986)` | 一致 |
| 13.50 | 任意 | `(0, 0, 1920, 1080)` | 一致 |

## 3. 实机端到端

命令：`./run.sh run`，日志 `logs/launcher.log`。

```
session:player-rect 0,0 1920x1080 screen=1920x1080
embedder:select requested=auto mode=shm
launcher:phase WAITING_APP
media-opened duration=14.14s size=1920x1080
session:app-spawned cmd=/usr/bin/chatgpt
probe: 记录候选窗口 0x9800004
probe: 窗口 0x9800004 已稳定
launcher:app-ready client=0,28 1920x1052
embedder:x-error codes=[-1]
embedder:capability notes=合成器已接管该窗口的重定向，无法抢占（X 返回 BadAccess）
embedder:select requested=auto mode=shm
launcher:phase HOLDING at=11.333
session:hold-at 11.30s
launcher:phase TRANSITION
session:resume
session:aligned ok=True
launcher:phase HANDOFF
session:handoff-activate ok=True
launcher:phase DONE
session:handoff
cli:done handed_off=True
```

验证点：

- 完整状态迁移 `WAITING_APP → HOLDING → TRANSITION → HANDOFF → DONE`
- `session:resume` 出现，过渡正常推进（此前缺陷会导致卡死在 `HoldAt`）
- `session:aligned ok=True`，客户区对齐成功
- `session:handoff-activate ok=True`，激活经 `_NET_ACTIVE_WINDOW` 确认落地
- 进程正常退出并输出 `cli:done`
- `xdotool getactivewindow getwindowname` → `ChatGPT`
- 事后复查 `_NET_ACTIVE_WINDOW` 仍指向目标窗口，焦点持久保持

### XComposite 档实测结论

```
embedder:capability notes=合成器已接管该窗口的重定向，无法抢占（X 返回 BadAccess）
```

mutter 持有 `RedirectAutomatic`，用户态无法再抢 `RedirectManual`。
因此 `composite` 档在本环境下不可用，`auto` 正确降级到 `shm`。
这与设计预期一致，不是缺陷。

## 4. 联调中修复的缺陷

以下问题均在实机联调中暴露并修复，且已补回归测试：

| 缺陷 | 根因 | 修复 |
| --- | --- | --- |
| 过渡卡死不动 | 状态机发 `resume=True`，运行层从未执行 | `_apply_tick()` 补 `self._player.resume()` |
| 进度倒退导致走不完 | 暂停时 `time-pos` 退回关键帧 | 新增 `progress_position()` 取多个来源的最大值 |
| 交接后焦点丢失 | 播放窗口存活时目标窗口带 `_NET_WM_STATE_HIDDEN` | `HANDOFF` 拆两阶段，先关窗口再等可见再激活 |
| 交接时找错窗口 | `get_wm_class()` 返回元组导致匹配异常 | 改 `_wm_classes()`，strong/weak 双候选池 |
| 播放窗口尺寸放大 | `Xft.dpi: 115` 触发 Qt 高 DPI 缩放 | `run.sh` 与 `ensure_app()` 双处钉死 1:1 |
| 空闲画面覆盖底层窗口 | mpv `idle="yes"` 纯色画面 | 加 `keepaspect_window="yes"` 保留末帧 |
| 进程跑完不退出 | Qt 仅在最后窗口关闭时自动退出，会话还挂着定时器 | `BootSession(finished=app.quit)` 显式退出 |
| 激活请求丢失 | `activate()` 只 `flush`，进程随即断开 X 连接 | 改 `sync()`，并新增 `_activate_confirmed()` 复核重试 |
| 交接时无窗口可交 | 嵌入层从未 attach 时直接放弃 | 新增 `find_app_window()` 现找兜底 |
| 日志全空 | 入口未初始化日志 | `run`/`entries`/`calibrate` 均调用 `_setup_logging()` |
| 配置音量失效 | `launcher_command` 分支缩进吞掉 `volume` 赋值 | 独立 `elif key == "volume":` 分支 |
| 安装检查误报 | `set -o pipefail` 下 `grep -q` 触发 SIGPIPE | 改 `grep 'libmpv\.so' >/dev/null` |

## 5. 桌面入口接管

```
$ ./run.sh entries --plan
动作     文件                                               原因
跳过     /home/neo/.local/share/applications/chatgpt.desktop 列在 ExcludeEntrypoints
跳过     /usr/share/applications/chatgpt.desktop          已被同名入口遮蔽

$ ./run.sh entries --verify
所有已接管的入口都有效。
```

默认排除规则生效：`chatgpt.desktop` 未被接管（已被其它启动器包装）。
接管/还原往返由 `test_entrypoints.py::TestAdoptRestoreRoundTrip` 覆盖，
并验证 `[Desktop Entry]` 其余字段、顺序、注释原样保留。

## 6. 标定校验

```
$ ./run.sh calibrate
  Time     来源     X        Y        Width    Height   说明
   12.65   seed     0.1230   0.0940   0.7360   0.7250   差分矩形与种子偏离 25.62%，超出容差 2%，保留种子值（差分不可信）
   13.00   seed     0.0740   0.0610   0.8450   0.8250   连拍帧无差异（画面静止），保留种子值
   13.50   seed     0.0000   0.0000   1.0000   1.0000   连拍帧无差异（画面静止），保留种子值
   13.65   seed     0.0000   0.0000   1.0000   1.0000   连拍帧无差异（画面静止），保留种子值
```

标定工具在差分不可信时保留种子值而不猜边界，`trusted=False`，
符合"降级为种子校验工具"的设计。

## 7. 已知限制

1. **仅支持 X11**：Wayland 下无法读取其它窗口坐标，需切换到 Xorg 会话。
2. **`composite` 档在 mutter 下不可用**：由合成器策略决定，自动降级到 `shm`。
3. **默认不接管 `chatgpt.desktop`**：避免与既有启动器链路冲突，可改配置放开。
4. **开发环境焦点竞争**：联调时若其它程序同时抢焦点，会覆盖交接结果；
   本程序会复核并重试，但无法阻止第三方抢占。

## 8. 复现步骤

```bash
cd /home/neo/project/java/githubProject/dragon-codex-boot-linux

./scripts/install.sh --check   # 环境自检
./scripts/test.sh -q           # 单元测试，期望 240 passed
./run.sh entries --plan        # 接管计划（只读）
./run.sh calibrate             # 标定校验
./run.sh run                   # 实机端到端
tail -20 logs/launcher.log     # 期望以 cli:done handed_off=True 结束
```

## 11. 开源发布

### 11.1 发布信息

| 项 | 值 |
| --- | --- |
| 仓库 | https://github.com/jaydocoder/dragon-codex-boot-linux |
| 可见性 | Public |
| 默认分支 | `main` |
| 许可 | MIT（GitHub 已正确识别 `license.key = mit`） |
| 文件数 | 43 |
| 话题 | codex, chatgpt, linux, x11, pyqt6, libmpv, ubuntu, splash-screen, startup-animation, mit-license |

### 11.2 版权与署名合规校验

| 校验项 | 结果 |
| --- | --- |
| `LICENSE` 与上游逐字节一致 | 通过（blob 哈希均为 `8eb435753c6195a3bad81244522b72f56843aca5`） |
| `NOTICE.md` 列明上游作者与仓库 | 通过 |
| `NOTICE.md` 区分上游原创与本移植版新增 | 通过 |
| 媒体素材署名与许可边界 | 通过（`startup.mp4` MD5 与上游一致，`MEDIA_NOTICE.md` 原样保留） |
| 商标免责声明 | 通过（README / NOTICE / THIRD_PARTY 三处） |
| 文档内部链接 | 通过（无悬空链接） |
| 敏感信息扫描 | 通过（无 API key / token / 密码） |
| 构建产物未入库 | 通过（`.venv/`、`logs/*.log`、`launcher.json`、`__pycache__/` 均被忽略） |
| 可执行位保留 | 通过（`run.sh`、`scripts/*.sh` 均为 100755） |

### 11.3 发布前修正的问题

| 问题 | 处理 |
| --- | --- |
| `LICENSE` 版权行未体现上游作者 | 保留上游原文行，署名细节移入 `NOTICE.md` |
| `THIRD_PARTY_NOTICES.md` 残留 Windows / .NET / DWM 描述 | 改为 Linux 实际依赖（PyQt6 / libmpv / python-xlib / X11） |
| README 克隆地址为 `<this-repo>` 占位符 | 改为真实仓库地址 |
| README 缺少原作者署名 | 顶部加署名块，末尾加"致谢"章节 |
| `LICENSE` 追加版权行导致 GitHub 识别为 Other | 恢复标准 MIT 模板，署名移入 `NOTICE.md`，识别恢复为 MIT |
| 缺少 `.gitattributes` | 新增，声明换行符与二进制类型 |
| 缺少 `NOTICE.md` | 新增，完整说明来源与署名 |

### 11.4 发布后状态

- 全量测试：240 passed
- 本地 `HEAD` 与 `origin/main` 一致（`72b84da`）
- 工作区干净，无未提交改动
