"""旧版 Markdown 兼容：把 V1 生成的 ``*.md`` 转成可溯源的 TranscriptDocument。

约束：
1. 不要求用户重新下载或重新转写；
2. 不要求旧文档存在 transcript.json；
3. 以"段落"为稳定引用单元，按原文顺序编号（seg_0001…）；
4. 旧文档没有可靠时间信息 → ``time_precision=unknown``、时间字段为 null，
   笔记里用「原文第 N 段」定位，绝不编造视频时间；
5. **只读**：本模块打开旧文件只做读取，从不写回、移动或重命名。
"""

from __future__ import annotations

import hashlib
import os
import re

from transcript import (
    SOURCE_LEGACY_MARKDOWN,
    TIME_UNKNOWN,
    Segment,
    TranscriptDocument,
    assign_segment_ids,
)

# 旧文档里常见的标题行与元信息行，不属于正文
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_META_RE = re.compile(r"^\s*[-*]\s*(来源|语言|链接|时长|时间码)[：:]")
# 单段超过这个长度就按句子再切，避免整篇只有一个引用单元
_MAX_UNIT_CHARS = 400
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])")


class LegacyMarkdownError(ValueError):
    """旧 Markdown 无法读取或没有正文。"""


def _read_text(path: str) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except UnicodeDecodeError:
            continue
    raise LegacyMarkdownError(f"无法解码旧 Markdown（尝试过 utf-8 / gb18030）：{path}")


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(block)
    return digest.hexdigest()


def _split_long_block(block: str, max_chars: int = _MAX_UNIT_CHARS) -> list[str]:
    """把超长整块按句子切成若干稳定单元，保持原始顺序与文字不变。"""
    if len(block) <= max_chars:
        return [block]
    sentences = [part for part in _SENTENCE_SPLIT.split(block) if part.strip()]
    units: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + len(sentence) > max_chars:
            units.append(current.strip())
            current = sentence
        else:
            current += sentence
    if current.strip():
        units.append(current.strip())
    return units or [block]


def split_paragraphs(text: str) -> list[str]:
    """按空行切段；过长的整段再按句子切成稳定引用单元。

    V1 的转写文件常常整篇没有空行（多个切片用换行拼接），如果只按空行切，
    整篇会变成一个巨大的片段，既无法定位也无法作为证据。因此这里对超长块
    做二次切分，但**不删除、不改写任何正文**。
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    lines = []
    for line in normalized.split("\n"):
        if _HEADING_RE.match(line) or _META_RE.match(line):
            continue
        lines.append(line)

    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if line.strip():
            current.append(line.strip())
        elif current:
            blocks.append(" ".join(current).strip())
            current = []
    if current:
        blocks.append(" ".join(current).strip())

    units: list[str] = []
    for block in blocks:
        if not block:
            continue
        units.extend(_split_long_block(block))
    return [unit for unit in units if unit]


def load_legacy_markdown(path: str, *, title: str = "") -> TranscriptDocument:
    """读取旧 Markdown 并构造文档；原文件保持只读。"""
    absolute = os.path.abspath(path)
    if not os.path.isfile(absolute):
        raise LegacyMarkdownError(f"文件不存在：{absolute}")

    before_hash = sha256_of(absolute)
    text = _read_text(absolute)
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        raise LegacyMarkdownError("该 Markdown 没有可用正文（只有标题或空内容）。")

    segments = [
        Segment(id="", text=paragraph, start_ms=None, end_ms=None, time_precision=TIME_UNKNOWN)
        for paragraph in paragraphs
    ]
    assign_segment_ids(segments)

    after_hash = sha256_of(absolute)
    if before_hash != after_hash:
        # 理论上不会发生；真发生了必须报错，不能悄悄放过
        raise LegacyMarkdownError("读取过程中原文件发生变化，已中止以避免覆盖。")

    return TranscriptDocument(
        platform="legacy",
        video_id=os.path.splitext(os.path.basename(absolute))[0],
        title=title or os.path.splitext(os.path.basename(absolute))[0],
        url="",
        page=None,
        duration_ms=None,
        source_type=SOURCE_LEGACY_MARKDOWN,
        language="",
        segments=segments,
        provenance={
            "legacy_path": absolute,
            "legacy_sha256": before_hash,
            "paragraph_count": len(segments),
            "time_available": False,
        },
    )
