"""字幕发现、解析与选择（V2 功能 A）。

职责边界：
1. 发现：B站按 **目标分P的 cid** 查询字幕，YouTube/其他站点复用 yt-dlp 元数据；
2. 解析：SRT / VTT / B站字幕 JSON → 统一时间轴（毫秒整数）；
3. 选择：智能模式（人工字幕优先，否则回退 Whisper）、强制 Whisper、仅提取字幕；
4. 校验：空字幕、损坏、非法时间码、时间不连续/覆盖率过低 → 不产出"看似成功"的结果。

重要约束：
- yt-dlp 的 ``subtitles`` 视为平台普通字幕类别（不独立保证逐字人工制作）；
- 无法判断来源时标 ``unknown_subtitle``，绝不冒充人工字幕；
- B站 ``lan`` 以 ``ai-`` 开头或 ``type=1`` 的轨道标自动；仅 ``type=0`` 且语言码不是 AI 的才标 CC 类别；
- 本模块不下载音视频，也不调用 Whisper。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Iterable, Optional

from transcript import (
    SOURCE_AUTOMATIC_SUBTITLE,
    SOURCE_HUMAN_SUBTITLE,
    SOURCE_UNKNOWN_SUBTITLE,
    TIME_EXACT,
    TranscriptDocument,
    Segment,
    assign_segment_ids,
    assess_coverage,
)

MODE_SMART = "smart"
MODE_WHISPER_ONLY = "whisper_only"
MODE_SUBTITLE_ONLY = "subtitle_only"

MODE_LABELS = {
    MODE_SMART: "智能模式（人工字幕优先，无字幕转写）",
    MODE_WHISPER_ONLY: "强制 Whisper 转写",
    MODE_SUBTITLE_ONLY: "仅提取字幕（不转写）",
}

# 语言偏好：先中文（简/繁/通用），再退回原语言与英文
DEFAULT_LANGUAGE_PREFERENCE = ("zh-Hans", "zh-CN", "zh", "zh-TW", "zh-Hant", "en")

_UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_TAG_RE = re.compile(r"<[^>]+>")
_ASS_TAG_RE = re.compile(r"\{\\[^}]*\}")
_WS_RE = re.compile(r"[ \t\u00a0\u200b]+")


class SubtitleError(RuntimeError):
    """字幕获取/解析失败。"""


class SubtitleNotFoundError(SubtitleError):
    """确实没有可用字幕（调用方据此决定回退或报错）。"""


class SubtitleParseError(SubtitleError):
    """字幕内容存在但无法解析出可靠时间轴。"""


@dataclass
class SubtitleTrack:
    """一条可选字幕轨道的描述。"""

    language: str
    source_type: str
    ext: str = ""
    url: str = ""
    label: str = ""
    evidence: str = ""

    @property
    def is_human(self) -> bool:
        return self.source_type == SOURCE_HUMAN_SUBTITLE

    def describe(self) -> str:
        return f"{self.language or '未标语言'}({self.source_type},{self.ext or '未知格式'})"


@dataclass
class ParsedSubtitle:
    cues: list[tuple[int, int, str]]
    invalid_count: int = 0
    skipped_count: int = 0
    detected_format: str = "unknown"


@dataclass
class SubtitleSelection:
    """一次字幕选择的结果与原因，供 UI 展示"来源标识 / 回退原因"。"""

    document: Optional[TranscriptDocument] = None
    track: Optional[SubtitleTrack] = None
    reason: str = ""
    fallback_reason: str = ""
    attempted: list[str] = field(default_factory=list)
    # 即使没用上字幕，平台元信息（标题/时长）对后续 Whisper 流程仍然有用
    video_title: str = ""
    duration_ms: Optional[int] = None

    @property
    def used_subtitle(self) -> bool:
        return self.document is not None


# --------------------------------------------------------------------------- #
# 文本清洗
# --------------------------------------------------------------------------- #
def clean_text(raw: str) -> str:
    """去掉字幕格式标记，保留正文文字与原有顺序。"""
    if not raw:
        return ""
    text = raw.replace("\r", "")
    text = _ASS_TAG_RE.sub("", text)  # {\an8} 等 ASS 定位标记
    text = _TAG_RE.sub("", text)      # <c>、<00:00:00.000>、<i> 等
    text = html.unescape(text)
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = _WS_RE.sub(" ", text)
    return text.strip()


def _ms(value: float) -> int:
    return int(round(float(value) * 1000))


_TIMECODE_RE = re.compile(
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})[.,](?P<ms>\d{1,3})"
)
_SHORT_TIMECODE_RE = re.compile(r"(?P<m>\d{1,2}):(?P<s>\d{2})[.,](?P<ms>\d{1,3})")
_ARROW_RE = re.compile(r"\s*-+>\s*")


def parse_timecode(value: str) -> Optional[int]:
    """把 ``00:01:02,500`` / ``01:02.500`` 解析为毫秒；非法返回 None。"""
    text = (value or "").strip()
    match = _TIMECODE_RE.search(text)
    if match:
        hours = int(match.group("h"))
        minutes = int(match.group("m"))
        seconds = int(match.group("s"))
        fraction = match.group("ms").ljust(3, "0")
        if minutes > 59 or seconds > 59:
            return None
        return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(fraction)
    match = _SHORT_TIMECODE_RE.search(text)
    if match:
        minutes = int(match.group("m"))
        seconds = int(match.group("s"))
        fraction = match.group("ms").ljust(3, "0")
        if seconds > 59:
            return None
        return (minutes * 60 + seconds) * 1000 + int(fraction)
    return None


# --------------------------------------------------------------------------- #
# 解析器
# --------------------------------------------------------------------------- #
def _split_blocks(text: str) -> list[list[str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in normalized.split("\n"):
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _parse_timed_blocks(blocks: Iterable[list[str]], *, fmt: str) -> ParsedSubtitle:
    cues: list[tuple[int, int, str]] = []
    invalid = 0
    skipped = 0
    for block in blocks:
        timeline_index = None
        for index, line in enumerate(block):
            if "-->" in line:
                timeline_index = index
                break
        if timeline_index is None:
            # NOTE / STYLE / 元数据块：直接跳过，不算损坏
            skipped += 1
            continue
        timeline = block[timeline_index]
        parts = _ARROW_RE.split(timeline, maxsplit=1)
        if len(parts) != 2:
            invalid += 1
            continue
        start = parse_timecode(parts[0])
        # 结束时间后面可能带 align/position 等设置，parse_timecode 只取第一个时间码
        end = parse_timecode(parts[1])
        if start is None or end is None or end < start:
            invalid += 1
            continue
        body = " ".join(block[timeline_index + 1:]).strip()
        cleaned = clean_text(body)
        if not cleaned:
            skipped += 1
            continue
        cues.append((start, end, cleaned))
    return ParsedSubtitle(cues, invalid_count=invalid, skipped_count=skipped, detected_format=fmt)


def parse_srt(text: str) -> ParsedSubtitle:
    """解析 SRT。非法时间码计入 invalid_count，不静默当成成功。"""
    if not (text or "").strip():
        raise SubtitleParseError("字幕内容为空。")
    return _parse_timed_blocks(_split_blocks(text), fmt="srt")


def parse_vtt(text: str) -> ParsedSubtitle:
    """解析 WebVTT（含 NOTE / STYLE / cue id / 行内标签）。"""
    if not (text or "").strip():
        raise SubtitleParseError("字幕内容为空。")
    blocks = [
        block
        for block in _split_blocks(text)
        if not block[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION"))
    ]
    return _parse_timed_blocks(blocks, fmt="vtt")


def parse_bilibili_json(text: str) -> ParsedSubtitle:
    """解析 B站字幕 JSON：``{"body":[{"from":秒,"to":秒,"content":"..."}]}``。"""
    if not (text or "").strip():
        raise SubtitleParseError("字幕内容为空。")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise SubtitleParseError(f"B站字幕不是合法 JSON：{error}") from error

    body = payload.get("body") if isinstance(payload, dict) else payload
    if not isinstance(body, list):
        raise SubtitleParseError("B站字幕 JSON 缺少 body 列表。")

    cues: list[tuple[int, int, str]] = []
    invalid = 0
    skipped = 0
    for item in body:
        if not isinstance(item, dict):
            invalid += 1
            continue
        try:
            start = _ms(item.get("from"))
            end = _ms(item.get("to"))
        except (TypeError, ValueError):
            invalid += 1
            continue
        if start < 0 or end < start:
            invalid += 1
            continue
        cleaned = clean_text(str(item.get("content") or ""))
        if not cleaned:
            skipped += 1
            continue
        cues.append((start, end, cleaned))
    return ParsedSubtitle(cues, invalid_count=invalid, skipped_count=skipped, detected_format="bilibili_json")


def detect_format(text: str, ext: str = "") -> str:
    """按内容（而非只按扩展名）判断字幕格式。"""
    lowered = (ext or "").lower().lstrip(".")
    head = (text or "")[:400].lstrip("\ufeff \r\n\t")
    if head.startswith("WEBVTT") or lowered == "vtt":
        return "vtt"
    if head.startswith("{") or head.startswith("["):
        return "bilibili_json"
    if "-->" in head:
        return "srt" if lowered != "vtt" else "vtt"
    if lowered in ("srt", "vtt"):
        return lowered
    if lowered in ("json", "bcc"):
        return "bilibili_json"
    return "unknown"


def parse_subtitle(text: str, ext: str = "") -> ParsedSubtitle:
    """按格式解析；格式无法判断时报错而不是猜。"""
    fmt = detect_format(text, ext)
    if fmt == "srt":
        return parse_srt(text)
    if fmt == "vtt":
        return parse_vtt(text)
    if fmt == "bilibili_json":
        return parse_bilibili_json(text)
    raise SubtitleParseError("无法识别字幕格式（支持 SRT / VTT / B站 JSON）。")


# --------------------------------------------------------------------------- #
# 轨道发现
# --------------------------------------------------------------------------- #
def track_source_type_from_ytdlp(bucket: str, language: str = "") -> str:
    """yt-dlp 的 subtitles=人工，automatic_captions=自动；其余无法断言。"""
    if bucket == "subtitles":
        return SOURCE_HUMAN_SUBTITLE
    if bucket == "automatic_captions":
        return SOURCE_AUTOMATIC_SUBTITLE
    return SOURCE_UNKNOWN_SUBTITLE


def tracks_from_info(info: dict) -> list[SubtitleTrack]:
    """从 yt-dlp 的 info dict 构造候选轨道（纯函数，便于测试）。"""
    tracks: list[SubtitleTrack] = []
    for bucket, source_type, evidence in (
        ("subtitles", SOURCE_HUMAN_SUBTITLE, "yt-dlp 普通字幕类别（不独立保证逐字人工制作）"),
        ("automatic_captions", SOURCE_AUTOMATIC_SUBTITLE, "yt-dlp automatic_captions（平台语音识别）"),
    ):
        for language, formats in (info.get(bucket) or {}).items():
            if not formats:
                continue
            best = _pick_best_format(formats)
            if best is None:
                continue
            tracks.append(
                SubtitleTrack(
                    language=language,
                    source_type=source_type,
                    ext=best.get("ext", ""),
                    url=best.get("url", ""),
                    label=best.get("name", ""),
                    evidence=evidence,
                )
            )
    return tracks


def _pick_best_format(formats: list[dict]) -> Optional[dict]:
    """优先 vtt/srt（本项目直接支持），其次 json3/ttml 交给 yt-dlp 转换。"""
    priority = {"vtt": 0, "srt": 1, "json3": 2, "srv1": 3, "srv3": 4, "ttml": 5}
    candidates = [item for item in formats if isinstance(item, dict)]
    if not candidates:
        return None
    return sorted(candidates, key=lambda item: priority.get((item.get("ext") or "").lower(), 9))[0]


def _language_rank(language: str, preference: tuple[str, ...]) -> int:
    normalized = (language or "").replace("_", "-").lower()
    for index, wanted in enumerate(preference):
        wanted_lower = wanted.lower()
        if normalized == wanted_lower:
            return index
        # zh-CN 与 zh-cn 视为相同；zh-Hans 与 zh-hans-cn 之类的变体按前缀匹配
        if normalized.split("-")[0] == wanted_lower.split("-")[0] and "-" not in wanted_lower:
            return index + 10
    return 999


def order_tracks(
    tracks: Iterable[SubtitleTrack], preference: tuple[str, ...] = DEFAULT_LANGUAGE_PREFERENCE
) -> list[SubtitleTrack]:
    """排序：人工字幕优先 → 语言偏好 → 自动字幕 → 未知来源。"""
    source_rank = {
        SOURCE_HUMAN_SUBTITLE: 0,
        SOURCE_AUTOMATIC_SUBTITLE: 2,
        SOURCE_UNKNOWN_SUBTITLE: 3,
    }
    return sorted(
        tracks,
        key=lambda track: (
            source_rank.get(track.source_type, 4),
            _language_rank(track.language, preference),
            track.language,
        ),
    )


# --------------------------------------------------------------------------- #
# B站字幕（按 cid 精确获取，杜绝分P串台）
# --------------------------------------------------------------------------- #
def _requests_session():
    import requests

    session = requests.Session()
    # B站接口在系统代理下常返回 412，这里默认直连（与 utils 的 WBI 下载保持一致）
    session.trust_env = False
    cookie_file = (os.getenv("YTDLP_COOKIE_FILE") or "").strip()
    if cookie_file and os.path.exists(cookie_file):
        try:
            from http.cookiejar import MozillaCookieJar

            jar = MozillaCookieJar(cookie_file)
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        except Exception as error:  # noqa: BLE001 - 缺 cookie 不应中断流程
            print(f"读取 cookies 失败，将按匿名访问：{error}")
    return session


def _bilibili_headers() -> dict:
    return {
        "User-Agent": _UA_DESKTOP,
        "Referer": "https://www.bilibili.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def bilibili_page_meta(video_id: str, page: Optional[int]) -> dict:
    """取目标分P的 cid / 标题 / 时长；分P不存在时明确报错。"""
    session = _requests_session()
    response = session.get(
        "https://api.bilibili.com/x/player/pagelist",
        params={"bvid": video_id},
        headers=_bilibili_headers(),
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()
    pages = payload.get("data") or []
    if payload.get("code") != 0 or not pages:
        raise SubtitleError(
            f"获取分P信息失败：{payload.get('message') or '没有可用分P'}"
        )
    number = page or 1
    if number > len(pages):
        raise SubtitleError(
            f"该视频共 {len(pages)} 个分P，第 {number} P 不存在。"
        )
    target = pages[number - 1]
    return {
        "cid": target.get("cid"),
        "page": number,
        "title": target.get("part") or video_id,
        "duration_ms": int(target.get("duration") or 0) * 1000 or None,
        "total_pages": len(pages),
    }


def _bilibili_wbi_query(params: dict) -> str:
    """复用 utils 的 WBI 签名，避免重复实现。"""
    from utils import _get_wbi_mixin_key, _sign_wbi_params

    session = _requests_session()
    nav = session.get(
        "https://api.bilibili.com/x/web-interface/nav",
        headers=_bilibili_headers(),
        timeout=15,
    ).json()
    wbi_img = (nav.get("data") or {}).get("wbi_img")
    if not wbi_img:
        raise SubtitleError("无法获取 B站接口签名信息。")
    return _sign_wbi_params(params, _get_wbi_mixin_key(wbi_img))


def discover_bilibili(
    video_id: str, page: Optional[int] = None
) -> tuple[list[SubtitleTrack], dict]:
    """查询指定分P的字幕轨道，返回（轨道列表, 分P元信息）。"""
    meta = bilibili_page_meta(video_id, page)
    cid = meta.get("cid")
    if not cid:
        raise SubtitleError("目标分P缺少 cid，无法定位字幕。")

    session = _requests_session()
    # 只信任按目标 CID 签名查询的 WBI 接口；旧版无签名接口可能返回
    # 无法确认所属视频的数据，不能作为字幕正文的权威来源。
    try:
        query = _bilibili_wbi_query({"bvid": video_id, "cid": cid})
        response = session.get(
            f"https://api.bilibili.com/x/player/wbi/v2?{query}",
            headers=_bilibili_headers(),
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise SubtitleError(
                f"B站 WBI 字幕接口返回错误：{payload.get('code')}"
            )
        subtitles = ((payload.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
    except Exception as error:  # noqa: BLE001
        raise SubtitleError(f"无法通过目标 CID 获取可信的 B站字幕：{error}") from error

    tracks: list[SubtitleTrack] = []
    for item in subtitles:
        language = str(item.get("lan") or "")
        declared_type = item.get("type")
        # B站 type: 0=CC, 1=AI；不能以非 ai- 的语言码反推人工字幕。
        # ai_type 在不同接口/版本含义不一，单独不足以确认人工来源。
        if language.lower().startswith("ai-") or declared_type == 1:
            source_type = SOURCE_AUTOMATIC_SUBTITLE
        elif language and type(declared_type) is int and declared_type == 0:
            source_type = SOURCE_HUMAN_SUBTITLE
        else:
            source_type = SOURCE_UNKNOWN_SUBTITLE
        evidence = (
            f"B站字幕元数据（lan={language or '缺失'}, "
            f"type={declared_type!r}, ai_type={item.get('ai_type')!r}；"
            f"分类={source_type}）"
        )
        url = str(item.get("subtitle_url") or "")
        if url.startswith("//"):
            url = "https:" + url
        tracks.append(
            SubtitleTrack(
                language=language,
                source_type=source_type,
                ext="json",
                url=url,
                label=str(item.get("lan_doc") or ""),
                evidence=evidence,
            )
        )
    meta["tracks_count"] = len(tracks)
    return tracks, meta


def fetch_bilibili_subtitle(track: SubtitleTrack) -> ParsedSubtitle:
    if not track.url:
        raise SubtitleError("该字幕轨道没有下载地址。")
    session = _requests_session()
    response = session.get(track.url, headers=_bilibili_headers(), timeout=20)
    response.raise_for_status()
    return parse_bilibili_json(response.text)


# --------------------------------------------------------------------------- #
# yt-dlp 字幕（YouTube 等）
# --------------------------------------------------------------------------- #
def _ytdlp_proxy_argument() -> list[str]:
    proxy = (os.getenv("YTDLP_PROXY") or "").strip()
    return ["--proxy", proxy] if proxy else []


def _ytdlp_command() -> list[str]:
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]
    return [sys.executable, "-m", "yt_dlp"]


def discover_ytdlp(url: str) -> tuple[list[SubtitleTrack], dict]:
    """用 yt-dlp 元数据发现字幕轨道，并带回标题与时长。"""
    try:
        import yt_dlp
    except ImportError as error:  # pragma: no cover - 依赖缺失
        raise SubtitleError(f"未安装 yt-dlp，无法获取字幕：{error}") from error

    options = {
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 30,
    }
    proxy = (os.getenv("YTDLP_PROXY") or "").strip()
    if proxy:
        options["proxy"] = proxy
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except Exception as error:  # noqa: BLE001 - 统一转成可读错误
        raise SubtitleError(f"获取视频元数据失败：{error}") from error

    tracks = tracks_from_info(info or {})
    meta = {
        "title": (info or {}).get("title") or "",
        "duration_ms": int(((info or {}).get("duration") or 0) * 1000) or None,
        "tracks_count": len(tracks),
    }
    return tracks, meta


def fetch_ytdlp_subtitle(url: str, track: SubtitleTrack) -> ParsedSubtitle:
    """用 yt-dlp 把字幕取到临时目录并转成 VTT，再交给统一解析器。"""
    temporary_dir = tempfile.mkdtemp(prefix="b2t-sub-")
    try:
        arguments = [
            *_ytdlp_command(),
            "--skip-download",
            "--no-playlist",
            "--sub-langs", track.language,
            "--sub-format", "vtt/srt/best",
            "--convert-subs", "vtt",
            "-o", os.path.join(temporary_dir, "%(id)s.%(ext)s"),
            *_ytdlp_proxy_argument(),
        ]
        if track.source_type == SOURCE_AUTOMATIC_SUBTITLE:
            arguments.append("--write-auto-subs")
        else:
            arguments.append("--write-subs")
        arguments.append(url)

        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        files = [
            name
            for name in os.listdir(temporary_dir)
            if name.lower().endswith((".vtt", ".srt"))
        ]
        if not files:
            detail = (result.stderr or "").strip().splitlines()[-3:]
            raise SubtitleNotFoundError(
                "未取到字幕文件：" + (" / ".join(detail) if detail else "yt-dlp 未返回字幕")
            )
        path = os.path.join(temporary_dir, files[0])
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        return parse_subtitle(text, os.path.splitext(files[0])[1])
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 构建文档 + 选择策略
# --------------------------------------------------------------------------- #
def build_document(
    cues: list[tuple[int, int, str]],
    *,
    source_type: str,
    language: str,
    platform: str = "",
    video_id: str = "",
    title: str = "",
    url: str = "",
    page: Optional[int] = None,
    duration_ms: Optional[int] = None,
    precision: str = TIME_EXACT,
    provenance: Optional[dict] = None,
) -> TranscriptDocument:
    """把时间轴转化为 TranscriptDocument（ID 在此一次性生成）。"""
    segments = [
        Segment(
            id="",
            text=text,
            start_ms=start,
            end_ms=end,
            time_precision=precision,
        )
        for start, end, text in cues
    ]
    assign_segment_ids(segments)
    return TranscriptDocument(
        platform=platform,
        video_id=video_id,
        title=title,
        url=url,
        page=page,
        duration_ms=duration_ms,
        source_type=source_type,
        language=language,
        segments=segments,
        provenance=dict(provenance or {}),
    )


def select_track(
    tracks: list[SubtitleTrack],
    *,
    mode: str,
    preference: tuple[str, ...] = DEFAULT_LANGUAGE_PREFERENCE,
) -> tuple[Optional[SubtitleTrack], str]:
    """按模式挑一条轨道；返回（轨道, 选择理由）。"""
    ordered = order_tracks(tracks, preference)
    if not ordered:
        return None, "没有发现任何字幕轨道"

    if mode == MODE_SMART:
        for track in ordered:
            if track.is_human:
                return track, f"采用人工字幕 {track.language}（{track.evidence}）"
        automatic = [track for track in ordered if track.source_type == SOURCE_AUTOMATIC_SUBTITLE]
        if automatic:
            return None, "只有自动字幕，不视为可靠人工字幕，回退 Whisper"
        return None, "没有可确认为人工来源的字幕，回退 Whisper"

    if mode == MODE_SUBTITLE_ONLY:
        track = ordered[0]
        return track, f"仅提取字幕模式，选用 {track.language}（{track.source_type}）"

    # 强制 Whisper：不选任何字幕
    return None, "已选择强制 Whisper，忽略已有字幕"


def resolve_subtitles(
    ref,
    *,
    mode: str = MODE_SMART,
    preference: tuple[str, ...] = DEFAULT_LANGUAGE_PREFERENCE,
    min_coverage: float = 0.55,
) -> SubtitleSelection:
    """完整流程：发现 → 选择 → 获取 → 解析 → 校验。

    任何一步失败都返回带原因的 Selection（智能模式下由调用方回退 Whisper）；
    ``subtitle_only`` 模式下失败会抛异常，绝不偷偷转写。
    """
    selection = SubtitleSelection()

    if mode == MODE_WHISPER_ONLY:
        selection.fallback_reason = "已选择强制 Whisper，忽略已有字幕"
        selection.reason = selection.fallback_reason
        return selection

    try:
        if ref.platform == "bilibili":
            tracks, meta = discover_bilibili(ref.video_id, ref.page)
        else:
            tracks, meta = discover_ytdlp(ref.canonical_url)
    except Exception as error:  # noqa: BLE001 - 网络/接口问题都走回退或报错
        message = f"字幕发现失败：{error}"
        if mode == MODE_SUBTITLE_ONLY:
            raise SubtitleNotFoundError(message) from error
        selection.fallback_reason = message
        selection.reason = message
        return selection

    # 平台元信息对后续 Whisper 流程同样有用（标题、时长），先记录下来
    selection.video_title = str(meta.get("title") or "")
    selection.duration_ms = meta.get("duration_ms")

    track, reason = select_track(tracks, mode=mode, preference=preference)
    selection.reason = reason
    if track is None:
        selection.fallback_reason = reason
        if mode == MODE_SUBTITLE_ONLY:
            available = "、".join(item.describe() for item in tracks) or "无"
            raise SubtitleNotFoundError(
                f"没有可用的字幕（现有轨道：{available}）。仅提取字幕模式不会启动 Whisper。"
            )
        return selection

    selection.attempted.append(track.describe())
    try:
        if ref.platform == "bilibili":
            parsed = fetch_bilibili_subtitle(track)
        else:
            parsed = fetch_ytdlp_subtitle(ref.canonical_url, track)
    except Exception as error:  # noqa: BLE001
        message = f"字幕获取/解析失败：{error}"
        if mode == MODE_SUBTITLE_ONLY:
            raise SubtitleNotFoundError(message) from error
        selection.fallback_reason = message
        return selection

    if not parsed.cues:
        message = (
            f"字幕为空或全部非法（非法时间码 {parsed.invalid_count} 处）"
        )
        if mode == MODE_SUBTITLE_ONLY:
            raise SubtitleParseError(message)
        selection.fallback_reason = message
        return selection

    document = build_document(
        parsed.cues,
        source_type=track.source_type,
        language=track.language,
        platform=ref.platform,
        video_id=ref.video_id,
        title=meta.get("title") or ref.title_hint or ref.video_id,
        url=ref.canonical_url,
        page=ref.page,
        duration_ms=meta.get("duration_ms"),
        precision=TIME_EXACT,
        provenance={
            "track_language": track.language,
            "track_label": track.label,
            "track_evidence": track.evidence,
            "subtitle_format": parsed.detected_format,
            "invalid_cues": parsed.invalid_count,
            "skipped_cues": parsed.skipped_count,
            "cid": meta.get("cid"),
        },
    )

    coverage = assess_coverage(document, min_ratio=min_coverage)
    document.provenance["coverage"] = {
        "ratio": coverage.coverage_ratio,
        "verdict": coverage.verdict,
        "reasons": coverage.reasons,
    }
    if not coverage.usable:
        message = "字幕不完整：" + "；".join(coverage.reasons)
        if mode == MODE_SUBTITLE_ONLY:
            raise SubtitleParseError(message)
        print(f"{message}，回退 Whisper")
        selection.fallback_reason = message
        return selection

    selection.document = document
    selection.track = track
    return selection
