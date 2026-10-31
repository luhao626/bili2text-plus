"""视频来源识别与链接解析。

设计目标：
1. 只依赖标准库，便于单元测试；
2. 用域名精确判定平台，而不是 "bilibili.com" in text 这类子串匹配；
3. 从中文分享文案里提取出真正的 URL；
4. 为每个视频（以及 B 站每个分 P）生成互不冲突的缓存键；
5. 生成 Windows 合法文件名。

本模块不做任何网络请求：短链重定向解析由 utils 负责。
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

PLATFORM_LABELS = {
    "bilibili": "B站",
    "douyin": "抖音",
    "youtube": "YouTube",
    "generic": "其他站点",
    "local": "本地文件",
}

# 本地文件兜底：抖音等平台在 2026-09 后封掉了命令行直连，
# 用户可在浏览器里正常下载视频后，直接把文件交给本工具转写。
LOCAL_VIDEO_EXTS = (
    ".mp4", ".m4v", ".mov", ".mkv", ".flv", ".webm", ".avi", ".ts", ".wmv",
)
LOCAL_AUDIO_EXTS = (
    ".mp3", ".m4a", ".wav", ".aac", ".flac", ".ogg", ".opus", ".wma",
)
LOCAL_MEDIA_EXTS = LOCAL_VIDEO_EXTS + LOCAL_AUDIO_EXTS

# 域名白名单：必须与主机名精确匹配，或作为子域出现
BILIBILI_HOSTS = ("bilibili.com", "b23.tv")
DOUYIN_HOSTS = ("douyin.com", "iesdouyin.com")
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")

# 分享文案里 URL 可能紧贴中文标点，这里只允许常见 URL 字符
_URL_IN_TEXT = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"(?:/video/)?av(\d+)", re.IGNORECASE)
_DOUYIN_VIDEO_RE = re.compile(r"/(?:share/)?video/(\d{6,})")
_DOUYIN_NOTE_RE = re.compile(r"/(?:share/)?note/(\d{6,})")
_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,}$")

_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class UnsupportedLinkError(ValueError):
    """链接无法识别或不支持。"""


@dataclass(frozen=True)
class VideoRef:
    """一次待处理视频的完整描述。"""

    platform: str
    video_id: str
    canonical_url: str
    cache_key: str
    page: Optional[int] = None
    title_hint: str = ""

    @property
    def platform_label(self) -> str:
        return PLATFORM_LABELS.get(self.platform, self.platform)

    @property
    def display(self) -> str:
        if self.page and self.page > 1:
            return f"{self.platform_label} · {self.video_id} 第 {self.page} P"
        return f"{self.platform_label} · {self.video_id}"


def _host_matches(host: str, allowed: tuple[str, ...]) -> bool:
    """精确匹配域名，允许 www./m./v. 等子域，禁止 xxxbilibili.com 之类的伪装。"""
    host = (host or "").lower().strip(".")
    if not host:
        return False
    for base in allowed:
        if host == base or host.endswith("." + base):
            return True
    return False


def extract_url(text: str) -> Optional[str]:
    """从任意文本（含中文分享文案）中提取第一个 http(s) 链接。"""
    if not text:
        return None
    match = _URL_IN_TEXT.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;:)]}\"'")


def is_short_link(url: str) -> bool:
    """抖音 v.douyin.com / B站 b23.tv 这类需要跟随跳转的短链。"""
    host = (urlparse(url).netloc or "").lower()
    return _host_matches(host, ("v.douyin.com", "b23.tv"))


def _parse_page(query: dict) -> Optional[int]:
    raw = (query.get("p") or [None])[0]
    if raw is None:
        return None
    try:
        page = int(raw)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _bilibili_ref(url: str) -> VideoRef:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    match = _BV_RE.search(parsed.path) or _BV_RE.search(url)
    av_match = None if match else _AV_RE.search(url)

    if match:
        bvid = match.group(1)
        video_id = bvid
    elif av_match:
        video_id = f"av{av_match.group(1)}"
    else:
        raise UnsupportedLinkError(
            "未能从链接中识别 B 站视频号（BV 号或 av 号），请检查链接是否完整。"
        )

    page = _parse_page(query)
    # 旧版本用 BV 号做缓存目录，这里保持兼容；分 P 单独加后缀隔离
    cache_key = video_id if not page or page == 1 else f"{video_id}_p{page}"
    canonical = f"https://www.bilibili.com/video/{video_id}"
    if page and page > 1:
        canonical = f"{canonical}?p={page}"
    return VideoRef(
        platform="bilibili",
        video_id=video_id,
        canonical_url=canonical,
        cache_key=cache_key,
        page=page,
        title_hint=video_id,
    )


def _douyin_ref(url: str) -> VideoRef:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    match = _DOUYIN_VIDEO_RE.search(path) or _DOUYIN_NOTE_RE.search(path)
    if match:
        video_id = match.group(1)
        return VideoRef(
            platform="douyin",
            video_id=video_id,
            canonical_url=url,
            cache_key=f"dy_{video_id}",
            title_hint=video_id,
        )

    # 短链：用路径片段做稳定缓存键，真正的视频 ID 由 utils 跟随跳转后补全
    if _host_matches(host, ("v.douyin.com", "v.iesdouyin.com")):
        slug = re.sub(r"[^A-Za-z0-9]+", "", path) or "short"
        return VideoRef(
            platform="douyin",
            video_id=f"short_{slug}",
            canonical_url=url,
            cache_key=f"dy_{slug}",
            title_hint="douyin_short",
        )

    # 其余抖音链接（详情页缺 ID、首页、话题页等）没有可下载的单条视频
    raise UnsupportedLinkError(
        "该抖音链接中没有单条视频 ID。请使用视频详情页链接"
        "（https://www.douyin.com/video/...）或 App 分享出的短链。"
    )


def _youtube_ref(url: str) -> VideoRef:
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    query = parse_qs(parsed.query)
    if _host_matches(host, ("youtu.be",)):
        video_id = parsed.path.strip("/").split("/")[0]
    else:
        video_id = (query.get("v") or [None])[0]
        if not video_id and parsed.path.startswith("/shorts/"):
            video_id = parsed.path.split("/shorts/")[-1].split("/")[0]
    if video_id and _YOUTUBE_ID_RE.match(video_id):
        return VideoRef(
            platform="youtube",
            video_id=video_id,
            canonical_url=url,
            cache_key=f"yt_{video_id}",
            title_hint=video_id,
        )
    return VideoRef(
        platform="youtube",
        video_id="unknown",
        canonical_url=url,
        cache_key="yt_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:12],
        title_hint="youtube",
    )


def _generic_ref(url: str) -> VideoRef:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    return VideoRef(
        platform="generic",
        video_id=digest,
        canonical_url=url,
        cache_key=f"generic_{digest}",
        title_hint="video",
    )


def strip_wrapping_quotes(text: str) -> str:
    """去掉资源管理器「复制为路径」时带上的外层引号。"""
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1].strip()
    return text


def looks_like_local_media(text: str) -> bool:
    """判断输入是不是本地媒体文件路径（而不是链接或 BV 号）。"""
    candidate = strip_wrapping_quotes(text)
    if not candidate or "://" in candidate:
        return False
    lowered = candidate.lower()
    if not lowered.endswith(LOCAL_MEDIA_EXTS):
        return False
    # 排除 "abc.mp4" 这类只是恰好以扩展名结尾的裸词：必须能定位到文件
    return os.path.exists(candidate) or ("/" in candidate or "\\" in candidate)


def _local_ref(path: str) -> VideoRef:
    absolute = os.path.abspath(os.path.expanduser(strip_wrapping_quotes(path)))
    if not os.path.exists(absolute):
        raise UnsupportedLinkError(f"本地文件不存在：{absolute}")
    if not os.path.isfile(absolute):
        raise UnsupportedLinkError(f"这是一个文件夹，请选择具体的视频/音频文件：{absolute}")

    name = os.path.basename(absolute)
    stem = os.path.splitext(name)[0]
    digest = hashlib.sha1(absolute.lower().encode("utf-8")).hexdigest()[:8]
    # 同名不同路径的文件不能共用缓存目录，因此用路径哈希做后缀
    cache_key = f"local_{safe_filename(stem, max_length=48)}_{digest}"
    return VideoRef(
        platform="local",
        video_id=name,
        canonical_url=absolute,
        cache_key=cache_key,
        title_hint=stem,
    )


def resolve_video_input(raw_input: str) -> VideoRef:
    """把用户输入（链接 / 分享文案 / 裸 BV 号）解析成 VideoRef。"""
    text = (raw_input or "").strip()
    if not text:
        raise UnsupportedLinkError("请输入视频链接、本地文件路径，或粘贴抖音分享文案。")

    # 本地文件（含「复制为路径」带上引号、拖拽进来的绝对路径）
    if looks_like_local_media(text):
        return _local_ref(text)

    url = extract_url(text)
    if url:
        host = (urlparse(url).netloc or "").lower()
        scheme_fixed = url
        if scheme_fixed.startswith("http://") and not _host_matches(host, ("localhost",)):
            scheme_fixed = "https://" + scheme_fixed[len("http://"):]
        if _host_matches(host, BILIBILI_HOSTS):
            if _host_matches(host, ("b23.tv",)):
                # 短链需要联网展开，交回 utils 处理
                slug = re.sub(r"[^A-Za-z0-9]+", "", urlparse(url).path) or "short"
                return VideoRef(
                    platform="bilibili",
                    video_id=f"short_{slug}",
                    canonical_url=scheme_fixed,
                    cache_key=f"b23_{slug}",
                    title_hint="bilibili_short",
                )
            return _bilibili_ref(scheme_fixed)
        if _host_matches(host, DOUYIN_HOSTS):
            return _douyin_ref(scheme_fixed)
        if _host_matches(host, YOUTUBE_HOSTS):
            return _youtube_ref(scheme_fixed)
        return _generic_ref(scheme_fixed)

    # 没有链接：允许直接输入 BV 号 / av 号
    bv_match = _BV_RE.search(text)
    if bv_match:
        return _bilibili_ref(f"https://www.bilibili.com/video/{bv_match.group(1)}")
    if text.upper().startswith("AV") and text[2:].isdigit():
        return _bilibili_ref(f"https://www.bilibili.com/video/{text.lower()}")
    if text.startswith("BV") or text.startswith("bv"):
        candidate = "BV" + text[2:]
        if _BV_RE.fullmatch(candidate):
            return _bilibili_ref(f"https://www.bilibili.com/video/{candidate}")

    raise UnsupportedLinkError(
        "没有识别到可用的视频链接。支持 B站、抖音、YouTube 链接，"
        "也可以直接输入 BV 号、粘贴抖音 App 的分享文案，"
        "或直接给出本地视频/音频文件路径。"
    )


def safe_filename(name: str, *, max_length: int = 80, fallback: str = "video") -> str:
    """转换为 Windows 合法文件名：去掉非法字符、控制字符和保留名。"""
    cleaned = _INVALID_FILENAME_CHARS.sub("_", str(name or ""))
    cleaned = "".join(ch for ch in cleaned if ord(ch) >= 32)
    cleaned = cleaned.strip(" .")
    cleaned = re.sub(r"_{2,}", "_", cleaned).rstrip("._")
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip(" .")
    if not cleaned:
        return fallback
    if cleaned.split(".")[0].upper() in _WINDOWS_RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned
