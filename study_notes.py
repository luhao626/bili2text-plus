"""可溯源 AI 智能笔记（V2 功能 B）。

固定流程（AI 只提供结构，证据一律由程序回填）：
1. 把原文与 segment ID 提交给模型，要求返回严格 JSON（章节 + 核心知识点 + 引用 ID）；
2. 程序校验 JSON 结构与引用关系，**丢弃**指向不存在 ID 的条目；
3. 按 ID 从 TranscriptDocument 取回原文与时间范围（不采用 AI 编造的引文）；
4. 渲染 ``study_notes.md``，正文只允许「章节结构」「核心知识点」两个栏目；
5. 长文档按原文顺序分块提取，再合并去重并再次校验引用。

诚实性约束：程序能验证"引用 ID 存在""引文与原文一致"，但无法验证"语义是否真的
支持该结论"，因此不对语义核验作出保证；引文与原文对不上的条目会被明确标记待核实。
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from transcript import TIME_APPROXIMATE, Segment, TranscriptDocument, format_ms

SECTION_CHAPTERS = "## 02 · 章节结构"
SECTION_POINTS = "## 03 · 核心知识点"

MAX_QUOTE_CHARS = 220
DEFAULT_CHUNK_TOKENS = 6000

SYSTEM_PROMPT = (
    "你是一名严谨的中文学习笔记整理助手。"
    "你只能依据给定的原文片段工作，禁止补充任何原文没有的事实、数据、推荐或外部知识。"
    "你只输出 JSON，不要输出解释、Markdown 代码围栏或多余文字。"
)

USER_TEMPLATE = """下面是一个视频转写的原文片段。每个片段以 [segment_id] 开头。

请完成两件事，并严格按 JSON 输出：

1. chapters：按内容自然划分章节，每个章节给出简洁准确的名称，以及覆盖的 segment_id 列表。
2. key_points：提炼原文实际支持的核心知识点，每个知识点包含：
   - title：知识点标题（简洁）
   - explanation：一两句解释，只能用原文提供的信息
   - segment_ids：支撑该知识点的 segment_id 列表
   - quote：从原文中**原样摘录**的一句或几句支撑文字，不得改写

要求：
- 只能引用下面出现的 segment_id，不得编造 ID；
- 不得输出章节以外的概述、术语表、复习题、思维导图等内容；
- 如果某个知识点找不到原文支撑，就不要输出它；
- 章节数量按内容决定：内容包含多个明显不同的主题时，必须拆成多个章节；
  不要用单个章节覆盖整段内容，也不要机械按时间平均切分。

输出格式：
{{"chapters": [{{"title": "章节名", "segment_ids": ["seg_0001"]}}],
 "key_points": [{{"title": "知识点", "explanation": "解释", "segment_ids": ["seg_0001"], "quote": "原文摘录"}}]}}

原文片段：
{content}
"""

REPAIR_TEMPLATE = """你上一次的输出不是合法 JSON 或不符合约定结构（错误：{error}）。

请只输出修正后的 JSON 对象，字段为 chapters 与 key_points，不要输出任何其他内容。
只允许引用这些 segment_id：{allowed}

上一次的输出：
{previous}
"""


CHAPTER_ONLY_TEMPLATE = """下面是一个视频转写的完整原文，每个片段以 [segment_id] 开头。

上一次整理的章节结构把整段内容归成了一个章节。请在**不改变内容判断**的前提下，
按主题的自然变化重新划分章节（通常 3–6 个），每个章节覆盖一段连续的原文。

只输出 JSON：{{"chapters": [{{"title": "章节名", "segment_ids": ["seg_0001"]}}]}}
只能引用下面出现的 segment_id；不要输出知识点了。

原文：
{content}
"""


class StudyNotesError(RuntimeError):
    """笔记生成失败（不含"部分成功"的情况）。"""


@dataclass
class Chapter:
    title: str
    segment_ids: list[str] = field(default_factory=list)


@dataclass
class KeyPoint:
    title: str
    explanation: str
    segment_ids: list[str] = field(default_factory=list)
    # 由程序回填：证据文本与时间都来自源数据
    evidence: list[tuple[str, str]] = field(default_factory=list)  # [(时间标签, 原文)]
    needs_review: bool = False
    review_reason: str = ""


@dataclass
class StudyNotes:
    title: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    key_points: list[KeyPoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dropped_refs: int = 0
    complete: bool = True
    model: str = ""
    semantic_verification: str = "not_performed"

    @property
    def partial(self) -> bool:
        return not self.complete


# --------------------------------------------------------------------------- #
# 文本与分块
# --------------------------------------------------------------------------- #
def estimate_tokens(text: str) -> int:
    chinese = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    other = len(text) - chinese
    return int(chinese / 1.5 + other / 4)


def chunk_segments(
    segments: list[Segment], *, max_tokens: int = DEFAULT_CHUNK_TOKENS
) -> list[list[Segment]]:
    """按原文顺序分块，块内保留原始 segment ID，不改变任何时间位置。"""
    chunks: list[list[Segment]] = []
    current: list[Segment] = []
    current_tokens = 0
    for segment in segments:
        tokens = estimate_tokens(segment.text) + 8  # 预留 [seg_xxxx] 标记开销
        if current and current_tokens + tokens > max_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(segment)
        current_tokens += tokens
        # 单条超长片段也要能单独成块
        if current_tokens >= max_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
    if current:
        chunks.append(current)
    return chunks


def format_chunk(chunk: list[Segment]) -> str:
    return "\n".join(f"[{segment.id}] {segment.text.strip()}" for segment in chunk)


# --------------------------------------------------------------------------- #
# 模型调用
# --------------------------------------------------------------------------- #
def _strip_code_fence(text: str) -> str:
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


def _extract_json(text: str) -> dict:
    cleaned = _strip_code_fence(text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end <= start:
            raise StudyNotesError("AI 返回内容中没有 JSON 对象。")
        payload = json.loads(cleaned[start : end + 1])
    if not isinstance(payload, dict):
        raise StudyNotesError("AI 返回的 JSON 顶层不是对象。")
    return payload


def call_model(
    client,
    settings,
    prompt: str,
    *,
    max_tokens: int = 4000,
    attempts: int = 3,
) -> str:
    """调用 OpenAI 兼容接口；429/5xx 有限退避重试，其余错误交给调用方。"""
    from llm_config import classify_error, request_kwargs

    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.chat.completions.create(
                model=settings.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
                **request_kwargs(settings),
            )
        except Exception as error:  # noqa: BLE001
            last_error = error
            status = getattr(error, "status_code", None)
            if status in (429, 500, 502, 503, 504) and attempt < attempts:
                wait = 8 * attempt
                print(f"笔记接口被限流（HTTP {status}），{wait}s 后重试（{attempt}/{attempts}）...")
                time.sleep(wait)
                continue
            category, friendly = classify_error(error)
            raise StudyNotesError(f"AI 调用失败（{category}）：{friendly}") from error
        if not response.choices:
            raise StudyNotesError("AI 接口返回空结果。")
        content = response.choices[0].message.content or ""
        content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL)
        if not content.strip():
            raise StudyNotesError("AI 返回空内容（可能开启了思考模式或输出被截断）。")
        return content
    raise StudyNotesError(f"AI 调用失败：{last_error}")


# --------------------------------------------------------------------------- #
# 结构校验
# --------------------------------------------------------------------------- #
def _normalize_text(text: str) -> str:
    return re.sub(r"[\s，。、；：！？,.;:!?「」『』\"'（）()]+", "", text or "").lower()


def quote_matches_source(quote: str, source: str) -> bool:
    """严格检查规范化后的引文连续出现在原文中；字符重叠不等于逐字证据。"""
    normalized_quote = _normalize_text(quote)
    return bool(normalized_quote) and normalized_quote in _normalize_text(source)


def _quote_evidence_ids(
    ids: list[str], id_map: dict[str, Segment], quote: str
) -> list[str]:
    """优先展示实际包含逐字引文的连续引用，避免只展示无关的前三段。"""
    if quote:
        for width in range(1, min(3, len(ids)) + 1):
            for start in range(len(ids) - width + 1):
                selected = ids[start : start + width]
                source = " ".join(id_map[segment_id].text for segment_id in selected)
                if quote_matches_source(quote, source):
                    return selected
    return ids[:3]


def _missing_claim_markers(claim: str, source: str) -> list[str]:
    """识别来源片段中未出现的阿拉伯数字、英文专名/技术缩写。

    仅供保守标记待核实，不作正向语义验证；中文错误推断仍可能存在。
    """
    claim = unicodedata.normalize("NFKC", claim)
    source = unicodedata.normalize("NFKC", source)
    numbers = re.findall(r"(?<!\d)\d+(?:\.\d+)?%?", claim)
    english = re.findall(r"(?<![A-Za-z])[A-Za-z][A-Za-z0-9_.+-]*", claim)
    missing: list[str] = []
    for token in numbers:
        if not re.search(rf"(?<!\d){re.escape(token)}(?!\d)", source):
            if token not in missing:
                missing.append(token)
    for token in english:
        if len(token) < 2:
            continue
        if not re.search(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", source, re.I):
            if token not in missing:
                missing.append(token)
    return missing


def validate_payload(
    payload: dict,
    id_map: dict[str, Segment],
    *,
    strict_keys: bool = True,
    allowed_ids: Optional[set[str]] = None,
) -> tuple[list[Chapter], list[KeyPoint], list[str], int]:
    """把 AI 的 JSON 转成结构对象；丢弃无效引用并记录数量。"""
    warnings: list[str] = []
    dropped = 0

    if strict_keys:
        unknown = [
            key
            for key in payload
            if key not in ("chapters", "key_points", "keypoints", "knowledge_points")
        ]
        if unknown:
            warnings.append(f"已忽略 AI 返回的额外栏目：{'、'.join(unknown)}")

    raw_chapters = payload.get("chapters") or []
    raw_points = (
        payload.get("key_points")
        or payload.get("keypoints")
        or payload.get("knowledge_points")
        or []
    )
    if not isinstance(raw_chapters, list):
        raise StudyNotesError("chapters 字段不是数组。")
    if not isinstance(raw_points, list):
        raise StudyNotesError("key_points 字段不是数组。")

    chapters: list[Chapter] = []
    for item in raw_chapters:
        if not isinstance(item, dict):
            dropped += 1
            continue
        title = str(item.get("title") or "").strip()
        ids = [str(value) for value in (item.get("segment_ids") or [])]
        valid_ids, invalid = _partition_ids(ids, id_map, allowed_ids)
        dropped += invalid
        if not title:
            dropped += 1
            continue
        if not valid_ids:
            # 没有有效引用的章节无法溯源，丢弃而不是保留一个空壳
            dropped += 1
            continue
        chapters.append(Chapter(title=title, segment_ids=valid_ids))

    points: list[KeyPoint] = []
    for item in raw_points:
        if not isinstance(item, dict):
            dropped += 1
            continue
        title = str(item.get("title") or "").strip()
        explanation = str(item.get("explanation") or "").strip()
        ids = [str(value) for value in (item.get("segment_ids") or [])]
        valid_ids, invalid = _partition_ids(ids, id_map, allowed_ids)
        dropped += invalid
        if not title or not valid_ids:
            dropped += 1
            continue
        quote = str(item.get("quote") or "").strip()
        evidence: list[tuple[str, str]] = []
        source_text = " ".join(id_map[segment_id].text for segment_id in valid_ids)
        reasons: list[str] = []
        if not quote:
            reasons.append("AI 未提供逐字引文")
        elif not quote_matches_source(quote, source_text):
            reasons.append("AI 引文与原文不一致")
        evidence_ids = _quote_evidence_ids(valid_ids, id_map, quote)
        for segment_id in evidence_ids:
            segment = id_map[segment_id]
            label = segment.time_label() or _paragraph_label(id_map, segment_id)
            text = _excerpt_near_quote(segment.text.strip(), quote)
            evidence.append((label, text))
        missing = _missing_claim_markers(f"{title} {explanation}", source_text)
        if missing:
            reasons.append("数字或英文术语未在引用原文中出现：" + "、".join(missing[:8]))
        needs_review = bool(reasons)
        review_reason = "；".join(reasons)
        points.append(
            KeyPoint(
                title=title,
                explanation=explanation,
                segment_ids=valid_ids,
                evidence=evidence,
                needs_review=needs_review,
                review_reason=review_reason,
            )
        )

    return chapters, points, warnings, dropped


def _partition_ids(
    ids: Iterable[str],
    id_map: dict[str, Segment],
    allowed_ids: Optional[set[str]] = None,
) -> tuple[list[str], int]:
    valid: list[str] = []
    invalid = 0
    for segment_id in ids:
        if segment_id in id_map and (allowed_ids is None or segment_id in allowed_ids):
            if segment_id not in valid:
                valid.append(segment_id)
        else:
            invalid += 1
    return valid, invalid


def _paragraph_label(id_map: dict[str, Segment], segment_id: str) -> str:
    """无时间信息时用「原文第 N 段」定位，绝不编造时间。"""
    ordered = list(id_map.keys())
    try:
        index = ordered.index(segment_id) + 1
    except ValueError:
        return "原文片段"
    return f"原文第 {index} 段"


def _truncate(text: str, limit: int = MAX_QUOTE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _excerpt_near_quote(text: str, quote: str, limit: int = MAX_QUOTE_CHARS) -> str:
    """长片段展示逐字引文附近的原文，而不总是截取片段开头。"""
    if len(text) <= limit or not quote:
        return _truncate(text, limit)
    source_chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(text):
        normalized = _normalize_text(char)
        if normalized:
            source_chars.extend(normalized)
            offsets.extend([index] * len(normalized))
    normalized_quote = _normalize_text(quote)
    found = "".join(source_chars).find(normalized_quote) if normalized_quote else -1
    if found < 0:
        return _truncate(text, limit)
    start = max(0, offsets[found] - 20)
    end = min(len(text), start + limit)
    excerpt = text[start:end].rstrip()
    return ("…" if start > 0 else "") + excerpt + ("…" if end < len(text) else "")


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #
def merge_results(
    results: list[tuple[list[Chapter], list[KeyPoint]]],
) -> tuple[list[Chapter], list[KeyPoint]]:
    """合并分块结果：按标题去重，保留最先出现的位置，引用 ID 合并去重。"""
    chapter_index: dict[str, Chapter] = {}
    chapter_order: list[str] = []
    for chapters, _ in results:
        for chapter in chapters:
            key = _normalize_text(chapter.title)
            if key in chapter_index:
                existing = chapter_index[key]
                for segment_id in chapter.segment_ids:
                    if segment_id not in existing.segment_ids:
                        existing.segment_ids.append(segment_id)
            else:
                chapter_index[key] = Chapter(chapter.title, list(chapter.segment_ids))
                chapter_order.append(key)

    point_index: dict[str, KeyPoint] = {}
    point_order: list[str] = []
    for _, points in results:
        for point in points:
            key = _normalize_text(point.title)
            if key in point_index:
                existing = point_index[key]
                for segment_id in point.segment_ids:
                    if segment_id not in existing.segment_ids:
                        existing.segment_ids.append(segment_id)
                for item in point.evidence:
                    if item not in existing.evidence and len(existing.evidence) < 3:
                        existing.evidence.append(item)
                if point.needs_review:
                    existing.needs_review = True
                    if point.review_reason and point.review_reason not in existing.review_reason:
                        existing.review_reason = (
                            existing.review_reason + "；" + point.review_reason
                            if existing.review_reason else point.review_reason
                        )
                continue
            point_index[key] = point
            point_order.append(key)

    chapters = [chapter_index[key] for key in chapter_order]
    points = [point_index[key] for key in point_order]
    return chapters, points


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #
def render_markdown(notes: StudyNotes, document: TranscriptDocument) -> str:
    """只输出「章节结构」「核心知识点」两个正文栏目。"""
    heading = notes.title.strip() or document.title or "学习笔记"
    lines = [f"# 学习笔记：{heading}", ""]
    if notes.partial:
        lines.append("> 本笔记只覆盖了部分原文，未覆盖内容未纳入。")
        lines.append("")
    lines.append("> 溯源说明：引文来自原始转写；知识点解释未经独立语义核验，请结合原文判断。")
    lines.append("")

    lines.append(SECTION_CHAPTERS)
    lines.append("")
    if not notes.chapters:
        lines.append("- （原文未形成可识别的章节）")
    for chapter in notes.chapters:
        label = _chapter_label(document, chapter)
        if label:
            lines.append(f"- {label}{chapter.title}")
        else:
            lines.append(f"- {chapter.title}")
    lines.append("")

    lines.append(SECTION_POINTS)
    lines.append("")
    if not notes.key_points:
        lines.append("### 知识点：无可提炼内容")
        lines.append("")
        lines.append("原文中没有找到可被直接支持的知识点。")
        lines.append("")
    for point in notes.key_points:
        title = point.title
        if point.needs_review:
            title = f"{title}（待核实）"
        lines.append(f"### 知识点：{title}")
        lines.append("")
        if point.explanation:
            lines.append(point.explanation)
            lines.append("")
        if point.needs_review:
            lines.append(f"> 待核实原因：{point.review_reason or '证据支持性尚未确定'}")
            lines.append("")
        lines.append("**原文证据**")
        lines.append("")
        for label, text in point.evidence:
            lines.append(label)
            lines.append(f"「{text}」")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _chapter_label(document: TranscriptDocument, chapter: Chapter) -> str:
    """章节只标**起始**位置：一个章节可以跨很多片段，标区间会误导成章节很短。

    近似时间带 ``≈``；无时间信息时用「原文第 N 段」。
    """
    for segment_id in chapter.segment_ids[:1]:
        segment = document.segment_by_id(segment_id)
        if segment is None:
            continue
        if segment.has_time:
            prefix = "≈" if segment.time_precision == TIME_APPROXIMATE else ""
            return f"{prefix}[{format_ms(segment.start_ms)}] "
        return _paragraph_label(document.id_map(), segment_id) + "｜"
    return ""


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_prompt(chunk: list[Segment]) -> str:
    return USER_TEMPLATE.format(content=format_chunk(chunk))


def generate_study_notes(
    document: TranscriptDocument,
    client,
    settings,
    *,
    title: str = "",
    max_chunk_tokens: int = DEFAULT_CHUNK_TOKENS,
    progress: Optional[Callable[[str], None]] = None,
    stop_event=None,
) -> StudyNotes:
    """生成笔记；任何分块失败都不会产出"完整成功"的假象。"""
    report = progress or (lambda _message: None)
    if client is None:
        raise StudyNotesError(
            "未配置 AI 密钥，无法生成智能笔记。请先配置 LLM_API_KEY（或界面中的 API Key）。"
        )
    if not document.segments:
        raise StudyNotesError("原始转写为空，无法生成笔记。")

    document.ensure_valid()
    id_map = document.id_map()
    chunks = chunk_segments(document.segments, max_tokens=max_chunk_tokens)
    report(f"原文共 {len(document.segments)} 段，分 {len(chunks)} 块提交给 AI")

    results: list[tuple[list[Chapter], list[KeyPoint]]] = []
    notes = StudyNotes(title=title, model=getattr(settings, "model", ""))
    for index, chunk in enumerate(chunks, start=1):
        if stop_event is not None and stop_event.is_set():
            notes.complete = False
            notes.warnings.append(f"任务被停止，第 {index}/{len(chunks)} 块未处理")
            break
        report(f"正在处理第 {index}/{len(chunks)} 块（{len(chunk)} 段）")
        allowed_ids = [segment.id for segment in chunk]
        try:
            payload = _request_chunk(client, settings, chunk, allowed_ids)
            chapters, points, warnings, dropped = validate_payload(
                payload, id_map, allowed_ids=set(allowed_ids)
            )
        except StudyNotesError as error:
            notes.complete = False
            notes.warnings.append(f"第 {index} 块失败：{error}")
            print(f"第 {index} 块笔记生成失败：{error}")
            continue
        notes.warnings.extend(warnings)
        notes.dropped_refs += dropped
        results.append((chapters, points))

    if not results:
        raise StudyNotesError(
            "所有分块都未能生成有效笔记：" + "；".join(notes.warnings[:3])
        )

    chapters, points = merge_results(results)
    if len(chapters) < 2 and len(document.segments) > 40 and len(chunks) == 1:
        # 单个章节覆盖整段长内容通常是模型偷懒；只追加一次"仅重排章节"的请求
        report("章节结构过于笼统，请求一次细化（不改变知识点）")
        refined = _request_chapter_split(client, settings, document)
        if len(refined) > len(chapters):
            chapters = refined
    notes.chapters = chapters
    notes.key_points = points
    if notes.dropped_refs:
        notes.warnings.append(f"共丢弃 {notes.dropped_refs} 处无效引用")
    return notes


def _request_chapter_split(
    client, settings, document: TranscriptDocument
) -> list[Chapter]:
    """只重排章节的一次补充请求；失败不影响已生成的知识点。"""
    prompt = CHAPTER_ONLY_TEMPLATE.format(content=format_chunk(document.segments))
    try:
        content = call_model(client, settings, prompt, max_tokens=2000)
        payload = _extract_json(content)
    except Exception as error:  # noqa: BLE001 - 细化失败就保留原结果
        print(f"章节细化失败，保留原章节结构：{error}")
        return []
    chapters, _points, _warnings, _dropped = validate_payload(
        {"chapters": payload.get("chapters") or [], "key_points": []}, document.id_map()
    )
    return chapters


def _request_chunk(client, settings, chunk: list[Segment], allowed_ids: list[str]) -> dict:
    """请求一个分块；JSON 非法时做有限次纠错重试。"""
    prompt = build_prompt(chunk)
    content = call_model(
        client,
        settings,
        prompt,
        max_tokens=max(2000, min(8000, estimate_tokens(format_chunk(chunk)) * 2)),
    )
    last_error = None
    for attempt in range(1, 3):
        try:
            payload = _extract_json(content)
            if not isinstance(payload.get("chapters"), list) and not any(
                key in payload for key in ("key_points", "keypoints", "knowledge_points")
            ):
                raise StudyNotesError("JSON 缺少 chapters 与 key_points 字段。")
            return payload
        except (StudyNotesError, json.JSONDecodeError) as error:
            last_error = error
            if attempt >= 2:
                break
            print(f"AI 输出 JSON 不合法（{error}），请求一次修正...")
            repair_prompt = REPAIR_TEMPLATE.format(
                error=error, allowed="、".join(allowed_ids[:80]), previous=content[:4000]
            )
            content = call_model(client, settings, repair_prompt, max_tokens=3000)
    raise StudyNotesError(f"AI 返回的 JSON 无法解析：{last_error}")


def write_study_notes(notes: StudyNotes, document: TranscriptDocument, path: str) -> str:
    from transcript import atomic_write_text

    return atomic_write_text(path, render_markdown(notes, document))
