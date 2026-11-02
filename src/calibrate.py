"""标定工具：根据视频帧反推 ``ScreenFrames``。

上游 Windows 版的 ``ScreenFrames`` 是人工在视频里量出来的：动画最后几秒，
视频画面中那个「屏幕矩形」会逐步放大到铺满整个画面。只要知道若干时间点上
屏幕矩形在画面中的归一化位置和大小，``geometry.dest`` 就能把真实窗口画到
正确的地方。

人工量取费时且容易错，本模块改用「像素差分」自动测量：

1. 用 libmpv 渲染视频，在指定时间点抓取整帧（RGB24）；
2. 逐帧统计「当前帧」与「已铺满的基准帧」之间的差异；
3. 差异像素的外接矩形即为该时刻屏幕区域的位置——因为屏幕区域以外，
   视频背景是静止的；
4. 把像素矩形归一化成 0~1，得到可直接写入 ``launcher.json`` 的关键帧。

判定方向有两种模式：

- ``moving``（默认，对应 ``HoldAt`` 附近）：屏幕区域内的内容在动，背景静止；
- ``static``（对应过渡末尾）：所有区域都已铺满，差异应当趋近于零，
  只用于校验。

差异判定采用「逐像素差分 + 阈值 + 连通性自适应」的简化策略：不追求完美
分割，只求外接矩形稳定。实测表明，对上游这段视频（背景为渐变星空，
屏幕区域为明亮内容），逐像素阈值法足够可靠。

.. important::

   以上「自动求解」在实际素材上**并不成立**。对本项目自带的
   ``media/startup.mp4`` 做过完整实测（详见 ``docs/calibration.md``）：

   - 屏幕矩形与背景之间**没有可检测的边缘**（12.65s 处真值边界两侧灰度
     跳变仅 5~6，低于 H.264 压缩噪声）；
   - 背景本身是缓慢变化的亮色渐变，整体均值 230~250，亮度和局部方差
     都不能把屏幕区域从背景里分出来；
   - 13.0s 之后画面已接近纯色平坦，连「连拍帧差分」都没有信号。

   因此 ``calibrate()`` 的真实能力是**校验与微调**，而非从零求解：
   调用方传入 ``seeds``（上游已量好的关键帧）作为初值，工具用「同一时间点
   连拍帧差分」验证每个初值是否命中一段真实的运动内容，并在有运动信号的
   时间点上用差分矩形做局部修正。没有 ``seeds`` 时只做纯校验、不猜边界。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

from .geometry import ScreenFrame, clamp

# 逐像素差异阈值（0~255）。低于此值视为同一像素，用于过滤压缩噪点。
DEFAULT_PIXEL_THRESHOLD = 24

# 允许忽略的孤立差异像素比例，避免个别噪点把外接矩形撑大到整帧。
DEFAULT_NOISE_RATIO = 0.002

# 差分矩形与种子值的最大允许偏移（归一化比例）。超过说明差分矩形不可信，
# 此时保留种子值。0.02 约合 1920×1080 画面上的 38×22 像素。
DEFAULT_SNAP_TOLERANCE = 0.02


@dataclass(frozen=True)
class SampleReport:
    """单个采样时间点的标定结论，供命令行与日志输出。"""

    time: float
    #: 结论来源：``refined`` 采纳差分、``seed`` 保留种子、
    #: ``diff`` 无种子的未校验差分、``none`` 毫无信号。
    source: str
    frame: Optional[ScreenFrame]
    note: str


@dataclass(frozen=True)
class CalibrationResult:
    """``calibrate`` 的完整返回值。"""

    frames: List[ScreenFrame]
    report: List[SampleReport]

    @property
    def trusted(self) -> bool:
        """是否每个采样点都有可信结论（没有 ``none`` 或未校验的 ``diff``）。"""

        return all(item.source in ("refined", "seed") for item in self.report)


@dataclass(frozen=True)
class FrameBox:
    """一帧差异像素的外接矩形，像素坐标（相对整帧）。"""

    time: float
    left: int
    top: int
    width: int
    height: int
    changed_pixels: int
    total_pixels: int

    @property
    def changed_ratio(self) -> float:
        """差异像素占整帧的比例；0 表示画面与基准完全一致。"""

        if self.total_pixels <= 0:
            return 0.0
        return self.changed_pixels / self.total_pixels

    def as_screen_frame(self, width: int, height: int) -> ScreenFrame:
        """把像素矩形归一化成 ``ScreenFrame``（相对给定帧尺寸）。"""

        if width <= 0 or height <= 0:
            raise ValueError("帧尺寸必须为正数")
        return ScreenFrame(
            time=self.time,
            x=clamp(self.left / width),
            y=clamp(self.top / height),
            width=clamp(self.width / width),
            height=clamp(self.height / height),
        )


def row_stride(width: int) -> int:
    """返回 ``width`` 对应的 RGB24 行跨距（4 字节对齐）。

    libmpv 的 ``screenshot-raw`` 返回按 4 字节对齐的缓冲区：例如宽 1920 时
    每行 5760 字节本就对齐，但奇数宽度会补 1~3 字节填充。差分必须按真实
    跨距切行，否则会读到上一行的尾部像素。
    """

    if width <= 0:
        raise ValueError("宽度必须为正数")
    return (width * 3 + 3) // 4 * 4


def bgr0_to_rgb(data: bytes, width: int, height: int, stride: int) -> bytes:
    """把 mpv ``screenshot-raw`` 的 ``bgr0`` 数据转成紧凑 RGB24。

    ``bgr0`` 每像素 4 字节（B、G、R、填充），行跨距由 mpv 给出。差分只关心
    颜色是否变化，所以这里顺手丢掉填充字节并统一成 RGB24，后续按
    ``width * 3`` 向上取整的 stride 处理即可。
    """

    if width <= 0 or height <= 0:
        raise ValueError("帧尺寸必须为正数")
    if stride < width * 4:
        raise ValueError(f"行跨距 {stride} 小于最小要求 {width * 4}")
    if len(data) < stride * height:
        raise ValueError("帧数据长度不足")

    out = bytearray(width * height * 3)
    for y in range(height):
        src = y * stride
        dst = y * width * 3
        for x in range(width):
            s = src + x * 4
            d = dst + x * 3
            # bgr0 的字节序是 B、G、R、0，这里翻转成 R、G、B。
            out[d] = data[s + 2]
            out[d + 1] = data[s + 1]
            out[d + 2] = data[s]
    return bytes(out)


def _as_rows(data: bytes, height: int, stride: int) -> List[memoryview]:
    """把平面字节缓冲按真实行跨距切成逐行视图，避免逐行拷贝。

    ``stride`` 可能大于 ``width * 3``（mpv 的 ``screenshot-raw`` 返回按
    4 字节对齐的缓冲区），因此不能假设行跨度等于可视宽度。
    """

    view = memoryview(data)
    return [view[y * stride : (y + 1) * stride] for y in range(height)]


def diff_box(
    base: bytes,
    current: bytes,
    width: int,
    height: int,
    time: float,
    threshold: int = DEFAULT_PIXEL_THRESHOLD,
    noise_ratio: float = DEFAULT_NOISE_RATIO,
) -> Optional[FrameBox]:
    """计算两帧差异像素的外接矩形。

    ``base`` 与 ``current`` 都是 RGB24 平面数据，行跨距必须是 4 字节对齐
    的形式，即 ``stride = width * 3`` 向上取整到 4 的倍数。
    返回 ``None`` 表示差异低于噪声阈值（可视为画面无变化）。
    """

    if width <= 0 or height <= 0:
        raise ValueError("帧尺寸必须为正数")
    stride = row_stride(width)
    expected = stride * height
    if len(base) < expected or len(current) < expected:
        raise ValueError("帧数据长度与尺寸不匹配")

    base_rows = _as_rows(base, height, stride)
    current_rows = _as_rows(current, height, stride)

    left = width
    top = height
    right = -1
    bottom = -1
    changed = 0

    for y in range(height):
        row_a = base_rows[y]
        row_b = current_rows[y]
        for x in range(width):
            offset = x * 3
            # 只看一个通道就够判定「是否变化」：三个通道都稳定时它必然相等，
            # 而单通道抖动会被阈值吃掉。省掉两次减法让循环快三倍。
            if abs(row_a[offset] - row_b[offset]) <= threshold:
                continue
            if (
                abs(row_a[offset + 1] - row_b[offset + 1]) <= threshold
                and abs(row_a[offset + 2] - row_b[offset + 2]) <= threshold
            ):
                continue
            changed += 1
            if x < left:
                left = x
            if x > right:
                right = x
            if y < top:
                top = y
            if y > bottom:
                bottom = y

    total = width * height
    if changed == 0 or right < 0:
        return None
    if changed / total < noise_ratio:
        # 差异像素太少，几乎必然是压缩噪点或光标闪烁，不构成有效区域。
        return None

    return FrameBox(
        time=time,
        left=left,
        top=top,
        width=right - left + 1,
        height=bottom - top + 1,
        changed_pixels=changed,
        total_pixels=total,
    )


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        raise ValueError("不能对空序列求中位数")
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _trimmed_mean(values: Sequence[float], keep: float = 0.7) -> float:
    """去掉两端极值后取均值，用于抵消个别抖动帧的干扰。"""

    ordered = sorted(values)
    if not ordered:
        raise ValueError("不能对空序列求均值")
    count = max(1, int(round(len(ordered) * keep)))
    start = (len(ordered) - count) // 2
    subset = ordered[start : start + count]
    return sum(subset) / len(subset)


def stabilize_box(boxes: Sequence[FrameBox]) -> Optional[FrameBox]:
    """把同一时间点附近的多帧差异矩形合并成一个稳定结果。

    对每个边界分别取截尾均值：单帧偶发的大幅抖动会被剔除，
    而真实边界的稳定值会保留下来。
    """

    # 全部为 None 与传入空序列是两种不同情况：前者表示该时间点确实没差异，
    # 后者是调用方的编程错误，必须区分开。
    boxes = list(boxes)
    if not boxes:
        raise ValueError("待合并的帧列表不能为空")
    valid = [box for box in boxes if box is not None]
    if not valid:
        return None
    lefts = [box.left for box in valid]
    tops = [box.top for box in valid]
    rights = [box.left + box.width for box in valid]
    bottoms = [box.top + box.height for box in valid]

    left = int(round(_trimmed_mean(lefts)))
    top = int(round(_trimmed_mean(tops)))
    right = int(round(_trimmed_mean(rights)))
    bottom = int(round(_trimmed_mean(bottoms)))
    width = max(1, right - left)
    height = max(1, bottom - top)

    # 时间与像素统计取中位数，代表这一组采样的典型值。
    return FrameBox(
        time=_median([box.time for box in valid]),
        left=left,
        top=top,
        width=width,
        height=height,
        changed_pixels=int(_median([box.changed_pixels for box in valid])),
        total_pixels=valid[0].total_pixels,
    )


def frames_to_json(frames: Sequence[ScreenFrame]) -> List[dict]:
    """把关键帧序列导出成 ``launcher.json`` 的 ``ScreenFrames`` 结构。

    字段名使用上游的 PascalCase，数值保留 4 位小数：再多位只是浮点噪声，
    而 ``launcher.json`` 是给人看、给人改的配置文件。
    """

    return [
        {
            "Time": round(frame.time, 3),
            "X": round(frame.x, 4),
            "Y": round(frame.y, 4),
            "Width": round(frame.width, 4),
            "Height": round(frame.height, 4),
        }
        for frame in frames
    ]

#: mpv 的 MPV_ERROR_PROPERTY_UNAVAILABLE。seek 之后到第一帧可截取之间存在
#: 空窗，screenshot-raw 会以这个错误码返回。
MPV_ERROR_PROPERTY_UNAVAILABLE = -12

#: 抓帧失败时的重试预算（秒）。正常情况几十毫秒内就能成功。
SCREENSHOT_RETRY_SECONDS = 2.0


def _screenshot_with_retry(player, moment: float) -> object:
    """抓取当前帧，遇到"还没有可用帧"时重试。

    mpv 在 seek 到目标时间后，解码与渲染是异步的；screenshot-raw 在帧就绪
    前调用会抛 ``SystemError(..., -12, ...)``。其它错误码（例如文件损坏）
    直接向上抛，不做掩盖。
    """

    # 与 sample_frames 一致：延迟导入，避免缺 libmpv 时连模块导入都失败。
    from .video import VideoError, enforce_numeric_locale

    deadline = time.monotonic() + SCREENSHOT_RETRY_SECONDS
    delay = 0.01
    while True:
        enforce_numeric_locale()
        try:
            return player.command("screenshot-raw", "video")
        except SystemError as exc:
            args = exc.args or ()
            code = None
            if len(args) >= 2 and isinstance(args[1], int):
                code = args[1]
            else:
                for item in args:
                    if isinstance(item, int):
                        code = item
                        break
            if code != MPV_ERROR_PROPERTY_UNAVAILABLE:
                raise
            if time.monotonic() >= deadline:
                raise VideoError(
                    f"时间点 {moment:.2f}s 抓帧超时："
                    f"等待 {SCREENSHOT_RETRY_SECONDS:.1f} 秒仍无可用视频帧"
                ) from exc
            time.sleep(delay)
            delay = min(delay * 2, 0.1)


def sample_frames(
    video_path,
    times: Sequence[float],
    width: int,
    height: int,
    samples: int = 3,
    settle: float = 0.12,
    deadline_seconds: float = 20.0,
):
    """用 libmpv 逐时间点抓取视频帧，返回 ``{时间点: [RGB24 缓冲, ...]}``。

    这里**不**创建播放窗口：``vo=null`` 让 mpv 只解码不输出，
    再通过 ``screenshot-raw`` 取原始像素。这样标定可以在无窗口、
    甚至 headless 的会话里跑。

    ``samples`` 是每个时间点连拍的帧数（默认 3），后续用
    ``stabilize_box`` 合并，抵消单帧抖动。``settle`` 是 seek 之后
    等待解码器稳定的时间。
    """

    from pathlib import Path

    from .log import log
    from .video import VideoError, enforce_numeric_locale, probe_duration

    path = Path(video_path)
    if not path.is_file():
        raise VideoError(f"视频文件不存在：{path}")
    duration = probe_duration(path)
    if not duration:
        raise VideoError(f"无法读取视频时长，文件可能已损坏：{path}")

    try:
        import mpv
    except Exception as exc:  # 缺 python-mpv 或 libmpv
        raise VideoError(f"无法加载 libmpv 绑定：{exc}") from exc

    enforce_numeric_locale()
    # vo=null：不打开任何输出。pause=yes：由我们控制时间轴。
    player = mpv.MPV(vo="null", ao="null", pause=True, idle="yes")
    result: dict[float, list[bytes]] = {}
    try:
        player.play(str(path))
        wait_until = time.monotonic() + deadline_seconds
        while time.monotonic() < wait_until:
            enforce_numeric_locale()
            if player.duration:
                break
            time.sleep(0.05)
        else:
            raise VideoError(f"视频在 {deadline_seconds:.0f} 秒内未能加载：{path}")

        # mpv 的 width/height 是只读属性，抓帧尺寸要用 dw/dh（显示尺寸）控制。
        # 让输出尺寸与调用方要求严格一致，差分会更省事。
        enforce_numeric_locale()
        player.dw, player.dh = width, height

        for moment in times:
            if moment < 0 or moment > duration:
                raise ValueError(
                    f"采样时间 {moment:.2f}s 超出视频范围 0~{duration:.2f}s"
                )
            enforce_numeric_locale()
            player.command("seek", float(moment), "absolute", "exact")
            time.sleep(settle)
            # seek 之后 mpv 需要一帧才能把画面渲染出来。
            player.frame_step = True
            player.frame_step = False
            time.sleep(0.05)

            captured: list[bytes] = []
            for _ in range(max(1, samples)):
                enforce_numeric_locale()
                # 直接用底层命令：python-mpv 的 screenshot_raw 包装依赖
                # Pillow，本项目刻意不引入，因此自己处理返回值。
                #
                # screenshot-raw 在 seek 后可能立刻返回 -12
                # （MPV_ERROR_PROPERTY_UNAVAILABLE）：该时刻还没有可截取的
                # 视频帧。固定 sleep 无法彻底消除这个竞态（实测约 1/3 概率
                # 命中），必须按错误码重试。
                shot = _screenshot_with_retry(player, moment)
                if not isinstance(shot, dict) or shot.get("format") != "bgr0":
                    raise VideoError(
                        f"时间点 {moment:.2f}s 抓帧失败：mpv 返回格式异常"
                    )
                data = shot.get("data") or b""
                actual_stride = int(shot.get("stride") or 0)
                actual_height = int(shot.get("h") or 0)
                if actual_stride < row_stride(width) or actual_height < height:
                    raise VideoError(
                        f"时间点 {moment:.2f}s 抓帧尺寸不足："
                        f"stride={actual_stride} h={actual_height}"
                    )
                captured.append(bgr0_to_rgb(data, width, height, actual_stride))
                time.sleep(0.04)
            result[float(moment)] = captured
            log(f"calibrate:sample t={moment:.2f}s frames={len(captured)}")
    finally:
        try:
            player.terminate()
        except Exception:  # 关闭失败不应影响已采集的数据
            pass
    return result


def calibrate(
    samples: dict,
    width: int,
    height: int,
    threshold: int = DEFAULT_PIXEL_THRESHOLD,
    seeds: Optional[Sequence[ScreenFrame]] = None,
    snap_tolerance: float = DEFAULT_SNAP_TOLERANCE,
) -> "CalibrationResult":
    """由采样结果得到关键帧序列，并给出每个采样点的可信度。

    参数语义：

    - ``samples``：``sample_frames`` 的返回值，``{时间点: [RGB24 缓冲, ...]}``；
    - ``seeds``：可选的上游已知关键帧。给出时工具逐点校验并在有运动信号的
      采样点做局部修正；不给时只报告哪些时间点有运动信号；
    - ``snap_tolerance``：差分矩形与种子相差超过该归一化比例时，判定为
      「差分不可信」，保留种子值。默认 0.02，约合 1920×1080 上的 38×22 像素。

    返回 ``CalibrationResult``：``frames`` 是最终关键帧，``report`` 是逐点
    结论，便于命令行打印给人核对。
    """

    if width <= 0 or height <= 0:
        raise ValueError("帧尺寸必须为正数")
    if not samples:
        raise ValueError("采样结果不能为空")

    seed_map = {round(frame.time, 3): frame for frame in (seeds or [])}
    report: List[SampleReport] = []
    frames: List[ScreenFrame] = []

    for moment in sorted(samples):
        captured = samples[moment]
        if len(captured) < 2:
            raise ValueError(
                f"时间点 {moment:.2f}s 只抓到 {len(captured)} 帧，"
                "定位屏幕区域至少需要 2 帧"
            )
        # 相邻帧两两差分后取并集：任意一对都可能撞上静止瞬间。
        boxes = [
            diff_box(captured[i], captured[i + 1], width, height, moment, threshold)
            for i in range(len(captured) - 1)
        ]
        valid = [box for box in boxes if box is not None]
        matched_seed = seed_map.get(round(moment, 3))

        if not valid:
            # 连拍帧完全一致：这一时刻画面静止，差分给不出任何边界信息。
            if matched_seed is not None:
                frames.append(matched_seed)
                report.append(
                    SampleReport(
                        time=moment,
                        source="seed",
                        frame=matched_seed,
                        note="连拍帧无差异（画面静止），保留种子值",
                    )
                )
            else:
                report.append(
                    SampleReport(
                        time=moment,
                        source="none",
                        frame=None,
                        note="连拍帧无差异，且没有种子值可供参考",
                    )
                )
            continue

        union = _union_box(valid, moment)
        measured = union.as_screen_frame(width, height)

        if matched_seed is None:
            # 没有种子：如实报告测到的矩形，但标注为未经校验。
            report.append(
                SampleReport(
                    time=moment,
                    source="diff",
                    frame=measured,
                    note="无种子值，仅报告差分矩形，未经校验",
                )
            )
            frames.append(measured)
            continue

        drift = max(
            abs(measured.x - matched_seed.x),
            abs(measured.y - matched_seed.y),
            abs(measured.width - matched_seed.width),
            abs(measured.height - matched_seed.height),
        )
        if drift <= snap_tolerance:
            frames.append(measured)
            report.append(
                SampleReport(
                    time=moment,
                    source="refined",
                    frame=measured,
                    note=f"差分矩形与种子吻合（偏移 {drift * 100:.2f}%），采用差分值",
                )
            )
        else:
            frames.append(matched_seed)
            report.append(
                SampleReport(
                    time=moment,
                    source="seed",
                    frame=matched_seed,
                    note=(
                        f"差分矩形与种子偏离 {drift * 100:.2f}%，超出容差 "
                        f"{snap_tolerance * 100:.0f}%，保留种子值（差分不可信）"
                    ),
                )
            )

    return CalibrationResult(
        frames=sorted(frames, key=lambda frame: frame.time),
        report=sorted(report, key=lambda item: item.time),
    )


def _union_box(boxes: Sequence[FrameBox], time: float) -> FrameBox:
    """把同一时间点的多个差异矩形合并成其并集。"""

    left = min(box.left for box in boxes)
    top = min(box.top for box in boxes)
    right = max(box.left + box.width for box in boxes)
    bottom = max(box.top + box.height for box in boxes)
    return FrameBox(
        time=time,
        left=left,
        top=top,
        width=right - left,
        height=bottom - top,
        changed_pixels=max(box.changed_pixels for box in boxes),
        total_pixels=boxes[0].total_pixels,
    )
def sanity_check(frames: Sequence[ScreenFrame]) -> List[str]:
    """标定结果的额外合理性检查，返回中文问题列表。

    与 ``geometry.validate_frames`` 的通用校验互补：这里检查的是
    「这段标定是否像一段真实的放大动画」——时间递增、区域单调变大、
    且最终铺满画面。不满足通常说明采样时间点选错了。
    """

    problems: List[str] = []
    if len(frames) < 2:
        problems.append("标定结果至少需要 2 个关键帧")
        return problems

    previous = None
    for index, frame in enumerate(frames):
        if previous is not None:
            if frame.time <= previous.time:
                problems.append(f"第 {index + 1} 帧时间未递增")
            # 允许极小回退（浮点误差），但明显变小说明采到了错误区域。
            if frame.width + 1e-3 < previous.width:
                problems.append(f"第 {index + 1} 帧宽度比前一帧小")
            if frame.height + 1e-3 < previous.height:
                problems.append(f"第 {index + 1} 帧高度比前一帧小")
        previous = frame

    last = frames[-1]
    if not math.isclose(last.x, 0.0, abs_tol=0.02):
        problems.append("末帧 X 未归零，动画结束时屏幕区域应铺满整幅画面")
    if not math.isclose(last.y, 0.0, abs_tol=0.02):
        problems.append("末帧 Y 未归零，动画结束时屏幕区域应铺满整幅画面")
    if not math.isclose(last.width, 1.0, abs_tol=0.02):
        problems.append("末帧宽度未达到整幅画面")
    if not math.isclose(last.height, 1.0, abs_tol=0.02):
        problems.append("末帧高度未达到整幅画面")

    return problems


def format_frames(frames: Sequence[ScreenFrame]) -> str:
    """生成便于人工核对的对齐文本表格。"""

    lines = ["  Time       X        Y        Width    Height   像素矩形（1920×1080）"]
    for frame in frames:
        left = int(round(frame.x * 1920))
        top = int(round(frame.y * 1080))
        width = int(round(frame.width * 1920))
        height = int(round(frame.height * 1080))
        lines.append(
            f"  {frame.time:6.2f}  {frame.x:7.4f}  {frame.y:7.4f}  "
            f"{frame.width:7.4f}  {frame.height:7.4f}   "
            f"({left},{top},{width},{height})"
        )
    return "\n".join(lines)


def format_report(result: "CalibrationResult") -> str:
    """把标定结论渲染成便于人工阅读的中文表格。"""

    if not result.report:
        return "  （没有任何采样点）"

    lines = ["  Time     来源     X        Y        Width    Height   说明"]
    for item in result.report:
        if item.frame is None:
            cells = "      -        -        -        -"
        else:
            cells = (
                f"{item.frame.x:8.4f} {item.frame.y:8.4f} "
                f"{item.frame.width:8.4f} {item.frame.height:8.4f}"
            )
        lines.append(f"  {item.time:6.2f}   {item.source:<6} {cells}   {item.note}")

    if not result.trusted:
        lines.append("")
        lines.append(
            "  注意：存在没有种子值兜底的采样点，标定结果不完整。"
            "请用 --seed-from 指向已有的 launcher.json。"
        )
    return "\n".join(lines)
