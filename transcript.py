"""统一转写数据结构 TranscriptDocument（V2 数据契约）。

设计要点：
1. JSON 是溯源的事实来源，Markdown 只是展示结果；
2. 每个 segment 有稳定且唯一的 ID（``seg_0001`` 递增，创建后不再重排）；
3. 时间统一为毫秒整数；无时间信息时字段为 ``None``（JSON 中为 null）；
4. ``time_precision`` 只允许 ``exact`` / ``approximate`` / ``unknown``；
   ``exact`` 仅表示"直接采用字幕源给出的时间码"，不代表与实际语音毫秒级同步；
5. Whisper 的 45 秒切片只具备近似定位能力，只能标 ``approximate``；
6. 所有正式输出都走"临时文件 + 原子替换"，避免中断后留下看似成功的损坏文件。

本模块只依赖标准库，便于单元测试。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

SCHEMA_VERSION = "2.0"

# 文字来源类型：必须严格区分，不允许把自动字幕标成人工字幕
SOURCE_HUMAN_SUBTITLE = "human_subtitle"
SOURCE_AUTOMATIC_SUBTITLE = "automatic_subtitle"
SOURCE_UNKNOWN_SUBTITLE = "unknown_subtitle"
SOURCE_WHISPER = "whisper"
SOURCE_LEGACY_MARKDOWN = "legacy_markdown"

SOURCE_TYPES = (
    SOURCE_HUMAN_SUBTITLE,
    SOURCE_AUTOMATIC_SUBTITLE,
    SOURCE_UNKNOWN_SUBTITLE,
    SOURCE_WHISPER,
    SOURCE_LEGACY_MARKDOWN,
)

SOURCE_LABELS = {
    SOURCE_HUMAN_SUBTITLE: "人工字幕",
    SOURCE_AUTOMATIC_SUBTITLE: "自动字幕",
    SOURCE_UNKNOWN_SUBTITLE: "未知来源字幕",
    SOURCE_WHISPER: "Whisper 转写",
    SOURCE_LEGACY_MARKDOWN: "旧版 Markdown",
}

SOURCE_IS_SUBTITLE = (
    SOURCE_HUMAN_SUBTITLE,
    SOURCE_AUTOMATIC_SUBTITLE,
    SOURCE_UNKNOWN_SUBTITLE,
)

TIME_EXACT = "exact"
TIME_APPROXIMATE = "approximate"
TIME_UNKNOWN = "unknown"

TIME_PRECISIONS = (TIME_EXACT, TIME_APPROXIMATE, TIME_UNKNOWN)


class TranscriptError(ValueError):
    """转写数据本身不合法（结构、时间码、ID 等）。"""


class SubtitleNotFoundError(RuntimeError):
    """没有可用字幕。调用方据此决定回退 Whisper 还是报错。"""


def atomic_write_text(path: str, text: str, *, encoding: str = "utf-8") -> str:
    """把文本写入 ``path``：先写同目录临时文件，再原子替换。

    同目录是必须的 —— 跨盘 ``os.replace`` 会失败；同目录还能保证替换是原子的。
    中断时只会残留 ``.tmp-*`` 临时文件，不会出现"看似写成功"的半截正式文件。
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding=encoding,
        newline="\n",
        dir=directory,
        prefix=".tmp-",
        suffix=".part",
        delete=False,
    )
    temporary_path = handle.name
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        try:
            if os.path.exists(temporary_path):
                os.remove(temporary_path)
        except OSError:
            pass
        raise
    return path


def atomic_write_json(path: str, payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, text)


@dataclass
class Segment:
    """一条带时间位置的文本单元。"""

    id: str
    text: str
    start_ms: Optional[int] = None
    end_ms: Optional[int] = None
    time_precision: str = TIME_UNKNOWN

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "time_precision": self.time_precision,
            "text": self.text,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Segment":
        return cls(
            id=str(data.get("id") or ""),
            text=str(data.get("text") or ""),
            start_ms=data.get("start_ms"),
            end_ms=data.get("end_ms"),
            time_precision=str(data.get("time_precision") or TIME_UNKNOWN),
        )

    @property
    def has_time(self) -> bool:
        return self.start_ms is not None and self.end_ms is not None

    def time_label(self) -> Optional[str]:
        """返回 ``[mm:ss]`` 或 ``[mm:ss–mm:ss]``；无时间返回 None（不编造）。

        ``approximate`` 会加 ``≈`` 前缀，明确告诉读者这不是逐句精确时间。
        """
        if not self.has_time:
            return None
        prefix = "≈" if self.time_precision == TIME_APPROXIMATE else ""
        if self.end_ms - self.start_ms <= 1000:
            return f"{prefix}[{format_ms(self.start_ms)}]"
        return f"{prefix}[{format_ms(self.start_ms)}–{format_ms(self.end_ms)}]"


def format_ms(value: Optional[int]) -> str:
    """毫秒 → mm:ss（超过 1 小时用 h:mm:ss）。仅用于展示。"""
    if value is None:
        return "--:--"
    total_seconds = max(0, int(value) // 1000)
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@dataclass
class TranscriptDocument:
    """一个视频（或一个旧 Markdown 文件）的完整可溯源转写。"""

    platform: str = ""
    video_id: str = ""
    title: str = ""
    url: str = ""
    page: Optional[int] = None
    duration_ms: Optional[int] = None
    source_type: str = SOURCE_WHISPER
    language: str = ""
    segments: list[Segment] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    # 溯源补充信息（不改变契约语义）：字幕轨道、来源证据、回退原因等
    provenance: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        payload = {
            "schema_version": self.schema_version,
            "video": {
                "platform": self.platform,
                "video_id": self.video_id,
                "page": self.page,
                "title": self.title,
                "url": self.url,
                "duration_ms": self.duration_ms,
            },
            "transcript": {
                "source_type": self.source_type,
                "language": self.language,
                "segments": [segment.to_dict() for segment in self.segments],
            },
        }
        if self.provenance:
            payload["provenance"] = self.provenance
        return payload

    @classmethod
    def from_dict(cls, data: dict) -> "TranscriptDocument":
        if not isinstance(data, dict):
            raise TranscriptError("转写数据必须是 JSON 对象。")
        version = str(data.get("schema_version") or "")
        if version != SCHEMA_VERSION:
            raise TranscriptError(
                f"不支持的 schema_version：{version or '(缺失)'}，本程序只接受 {SCHEMA_VERSION}。"
            )
        video = data.get("video") or {}
        transcript = data.get("transcript") or {}
        segments = [
            Segment.from_dict(item) for item in (transcript.get("segments") or [])
        ]
        return cls(
            platform=str(video.get("platform") or ""),
            video_id=str(video.get("video_id") or ""),
            title=str(video.get("title") or ""),
            url=str(video.get("url") or ""),
            page=video.get("page"),
            duration_ms=video.get("duration_ms"),
            source_type=str(transcript.get("source_type") or SOURCE_WHISPER),
            language=str(transcript.get("language") or ""),
            segments=segments,
            schema_version=version,
            provenance=dict(data.get("provenance") or {}),
        )

    # ------------------------------------------------------------------ #
    # 校验
    # ------------------------------------------------------------------ #
    def validate(self) -> list[str]:
        """返回问题列表；空列表表示通过。不抛异常，便于逐条报告。"""
        problems: list[str] = []
        if self.source_type not in SOURCE_TYPES:
            problems.append(f"未知的 source_type：{self.source_type}")
        if not self.segments:
            problems.append("没有任何 segment（空转写）")

        seen: set[str] = set()
        for index, segment in enumerate(self.segments, start=1):
            where = f"第 {index} 条（{segment.id or '无 ID'}）"
            if not segment.id:
                problems.append(f"{where} 缺少 ID")
            elif segment.id in seen:
                problems.append(f"{where} ID 重复")
            else:
                seen.add(segment.id)

            if segment.time_precision not in TIME_PRECISIONS:
                problems.append(f"{where} time_precision 非法：{segment.time_precision}")

            if segment.time_precision == TIME_UNKNOWN:
                if segment.start_ms is not None or segment.end_ms is not None:
                    problems.append(f"{where} 精度为 unknown 但带有时间码")
            else:
                if segment.start_ms is None or segment.end_ms is None:
                    problems.append(f"{where} 精度为 {segment.time_precision} 但缺少时间码")
                else:
                    if not isinstance(segment.start_ms, int) or not isinstance(
                        segment.end_ms, int
                    ):
                        problems.append(f"{where} 时间码必须是毫秒整数")
                    elif segment.start_ms < 0:
                        problems.append(f"{where} 起始时间为负")
                    elif segment.end_ms < segment.start_ms:
                        problems.append(f"{where} 结束时间早于起始时间")

            if not segment.text.strip():
                problems.append(f"{where} 文本为空")
        return problems

    def ensure_valid(self) -> None:
        problems = self.validate()
        if problems:
            raise TranscriptError("转写数据校验失败：" + "；".join(problems[:5]))

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def write_json(self, path: str) -> str:
        self.ensure_valid()
        return atomic_write_json(path, self.to_dict())

    def write_markdown(self, path: str, *, heading: str = "转写结果（原始）") -> str:
        """输出可读 Markdown。原文本与时间码原样呈现，不做任何改写。"""
        self.ensure_valid()
        source_label = SOURCE_LABELS.get(self.source_type, self.source_type)
        lines = [f"# {heading}", ""]
        meta = [f"- 来源：{source_label}"]
        if self.language:
            meta.append(f"- 语言：{self.language}")
        if self.url:
            meta.append(f"- 链接：{self.url}")
        if self.duration_ms:
            meta.append(f"- 时长：{format_ms(self.duration_ms)}")
        if self.source_type in SOURCE_IS_SUBTITLE:
            meta.append("- 时间码：来自字幕源（exact 仅表示取自信字幕，不代表与语音毫秒级同步）")
        elif self.source_type == SOURCE_WHISPER:
            meta.append("- 时间码：按 45 秒切片估算，为近似位置（approximate）")
        lines.extend(meta)
        lines.append("")
        for segment in self.segments:
            label = segment.time_label()
            prefix = f"{label} " if label else ""
            lines.append(f"{prefix}{segment.text.strip()}")
        lines.append("")
        return atomic_write_text(path, "\n".join(lines))

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def segment_by_id(self, segment_id: str) -> Optional[Segment]:
        for segment in self.segments:
            if segment.id == segment_id:
                return segment
        return None

    def id_map(self) -> dict[str, Segment]:
        return {segment.id: segment for segment in self.segments}

    @property
    def plain_text(self) -> str:
        return "\n".join(segment.text.strip() for segment in self.segments if segment.text.strip())


def assign_segment_ids(segments: Iterable[Segment], *, start: int = 1) -> list[Segment]:
    """按当前顺序重新赋予稳定 ID（仅在构建阶段调用，之后不得重排）。"""
    result = []
    for offset, segment in enumerate(segments):
        segment.id = f"seg_{start + offset:04d}"
        result.append(segment)
    return result


@dataclass
class CoverageReport:
    """字幕完整度评估。

    只看文本长度是不够的：一段 2 小时的视频里只有开头 3 分钟的字幕，文本可能
    很长但覆盖极低。因此这里综合有效时间跨度、空白比例和异常时间码。
    """

    duration_ms: Optional[int]
    segment_count: int
    effective_span_ms: int
    covered_ms: int
    coverage_ratio: Optional[float]
    empty_ratio: float
    invalid_count: int
    overlap_count: int
    gap_count: int
    verdict: str
    reasons: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return self.verdict == "ok"

    def summary(self) -> str:
        ratio = "未知" if self.coverage_ratio is None else f"{self.coverage_ratio * 100:.1f}%"
        return (
            f"覆盖面 {ratio}；片段 {self.segment_count} 条；"
            f"空白占比 {self.empty_ratio * 100:.1f}%；异常 {self.invalid_count} 处；"
            f"重叠 {self.overlap_count} 处；结论 {self.verdict}"
        )


def assess_coverage(
    document: TranscriptDocument,
    *,
    min_ratio: float = 0.55,
    max_empty_ratio: float = 0.35,
    allow_unknown_duration: bool = True,
) -> CoverageReport:
    """评估字幕/转写是否"完整可用"。

    判定规则（任一不满足即不可用）：
    1. 有 segment；
    2. 无非法时间码；
    3. 空文本比例不超过阈值；
    4. 若有视频时长，覆盖比例不低于阈值；
    5. 若无视频时长，只有在时间精度不是 unknown 时才允许（否则无法判断）。
    """
    reasons: list[str] = []
    segments = document.segments
    invalid = 0
    empty = 0
    overlap = 0

    timed: list[Segment] = []
    for segment in segments:
        if not segment.text.strip():
            empty += 1
        if segment.has_time:
            if segment.start_ms is None or segment.end_ms is None:
                invalid += 1
                continue
            if segment.start_ms < 0 or segment.end_ms < segment.start_ms:
                invalid += 1
                continue
            timed.append(segment)
        elif segment.time_precision != TIME_UNKNOWN:
            # 声称有精度却没有时间码
            invalid += 1

    timed.sort(key=lambda item: (item.start_ms, item.end_ms))
    previous_end = None
    covered = 0
    gaps = 0
    for segment in timed:
        start, end = int(segment.start_ms), int(segment.end_ms)
        if previous_end is not None:
            if start < previous_end - 500:  # 允许轻微重叠（字幕滚动常见）
                overlap += 1
                start = max(start, previous_end)
            elif start - previous_end > 15_000:
                gaps += 1
        if end > start:
            covered += end - start
        previous_end = max(previous_end or 0, end)

    span = 0
    if timed:
        span = int(timed[-1].end_ms) - int(timed[0].start_ms)

    duration = document.duration_ms
    ratio = None
    if duration and duration > 0:
        ratio = min(1.0, covered / duration)

    empty_ratio = empty / len(segments) if segments else 1.0

    if not segments:
        reasons.append("没有任何字幕/转写片段")
    if invalid:
        reasons.append(f"存在 {invalid} 处非法时间码")
    if empty_ratio > max_empty_ratio:
        reasons.append(f"空文本占比 {empty_ratio * 100:.0f}% 过高")
    if ratio is not None:
        if ratio < min_ratio:
            reasons.append(f"时间覆盖仅 {ratio * 100:.0f}%，低于 {min_ratio * 100:.0f}%")
    elif duration is None:
        if not allow_unknown_duration:
            reasons.append("无法获取视频时长，不能确认字幕完整性")
        elif not timed:
            reasons.append("既无视频时长也无时间码，无法确认完整性")

    verdict = "ok" if not reasons else "incomplete"
    return CoverageReport(
        duration_ms=duration,
        segment_count=len(segments),
        effective_span_ms=span,
        covered_ms=covered,
        coverage_ratio=ratio,
        empty_ratio=empty_ratio,
        invalid_count=invalid,
        overlap_count=overlap,
        gap_count=gaps,
        verdict=verdict,
        reasons=reasons,
    )


def merge_adjacent(
    segments: list[Segment], *, max_gap_ms: int = 120, max_chars: int = 220
) -> list[Segment]:
    """把字幕滚动造成的碎片合并成可读句子。

    只合并时间上相邻（间隔 ≤ max_gap_ms）且合并不超长的片段，保留首段时间范围；
    不删除任何正文，也不改动文字顺序。
    """
    merged: list[Segment] = []
    for segment in segments:
        if not merged:
            merged.append(segment)
            continue
        previous = merged[-1]
        previous_text = previous.text.strip()
        current_text = segment.text.strip()
        # 完全重复（滚动字幕常见）直接跳过，避免无意义重复
        if current_text and current_text == previous_text:
            if previous.has_time and segment.has_time:
                previous.end_ms = max(int(previous.end_ms), int(segment.end_ms))
            continue
        can_merge = (
            previous.has_time
            and segment.has_time
            and int(segment.start_ms) - int(previous.end_ms) <= max_gap_ms
            and segment.time_precision == previous.time_precision
            and len(previous_text) + len(current_text) <= max_chars
            and not previous_text.endswith(("。", "！", "？", "!", "?", "."))
        )
        if can_merge:
            previous.text = f"{previous_text}{current_text}"
            previous.end_ms = max(int(previous.end_ms), int(segment.end_ms))
        else:
            merged.append(segment)
    return merged
