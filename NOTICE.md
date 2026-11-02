# 来源与署名

## 本项目是什么

本项目是上游项目 **Dragon Codex Boot** 的 **Linux（Ubuntu / X11 / GNOME）平台移植版**。

它不是一个独立作品，而是对上游设计、配置模型与动画素材的直接复用，再加上针对
Linux 桌面环境的重新实现。

## 上游项目

| 项目 | 内容 |
| --- | --- |
| 名称 | Dragon Codex Boot |
| 仓库 | https://github.com/yushua0808-cpu/dragon-codex-boot |
| 作者 | yushua0808-cpu（Dragon Codex Boot maintainers） |
| 许可 | MIT License，见上游 `LICENSE` |
| 原平台 | Windows（WPF / DWM）与 macOS（Swift） |

**本项目全部原创设计的版权与功劳归属于上游作者。** 包括但不限于：

- 启动动画的创意、分镜与节奏设计
- `HoldAt` / `TransitionStart` / `TransitionEnd` 三阶段时间轴模型
- `ScreenFrames` 屏幕区域关键帧标定机制
- 把真实应用窗口对齐进动画画面的"窗口交接"交互理念
- PascalCase 命名的 `launcher.json` 配置格式
- 桌面入口接管的整体思路（Windows 快捷方式 → 本项目的 `.desktop` 改写）
- 随附的龙娘启动动画素材 `media/startup.mp4` 与 `media/MEDIA_NOTICE.md`

## 本移植版新增的内容

以下部分为本移植版针对 Linux 环境重新编写，版权归本移植版贡献者：

- `src/` 下全部 Python 模块（X11 窗口操作、XComposite 嵌入、libmpv 播放、
  几何对齐、会话状态机、入口接管等）
- `tests/` 下全部测试
- `scripts/install.sh`、`scripts/test.sh`、`run.sh`
- `docs/` 下 Linux 相关文档
- `config/launcher.linux.example.json`

这些新增内容同样以 MIT License 发布，见根目录 `LICENSE`。

## 许可合规说明

上游与本移植版均使用 **MIT License**，因此：

1. 允许修改、再分发与商用。
2. 必须保留原版权声明与许可声明，故根目录 `LICENSE` 沿用上游原文，
   未做任何替换或删改。
3. 本项目未使用上游作者名义做任何背书或推荐暗示。
4. 上游 Windows / macOS 实现代码未复制进本仓库——Linux 版是无共享代码的
   平台重写，仅复用设计约定与素材。

`media/startup.mp4` 与上游仓库中的文件逐字节一致（MD5
`5ba5da46cb13f1342e3c0d062efc8df0`），原样保留其 `media/MEDIA_NOTICE.md`，
媒体许可边界见该文件。

## 版权声明全文

`LICENSE` 采用标准 MIT 模板，版权行沿用上游原文：

```
Copyright (c) 2026 Dragon Codex Boot contributors
```

该行涵盖上游与本移植版两方贡献者。为求明确，此处分别列出：

- **上游原创设计与素材**：Copyright (c) 2026 Dragon Codex Boot contributors
  （Dragon Codex Boot maintainers，https://github.com/yushua0808-cpu/dragon-codex-boot）
- **Linux 移植版新增代码与文档**：Copyright (c) 2026 Dragon Codex Boot for Linux
  contributors（https://github.com/jaydocoder/dragon-codex-boot-linux）

两方均以 MIT License 授权，条款完全相同。`LICENSE` 未做任何删改，以同时满足
上游 MIT 许可证"保留原版权声明"的要求。

## 商标声明

OpenAI、Codex、ChatGPT 等名称仅用于说明本项目所支持的客户端。

**本项目是独立的社区工具，与 OpenAI 无隶属关系，也未获其授权、赞助或背书。**
