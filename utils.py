import os
import re
import shutil
import subprocess
import sys
import glob  # 新增导入
import hashlib
import time
import requests
from urllib.parse import parse_qs, urlencode, urlparse

from platforms import (
    UnsupportedLinkError,
    is_short_link,
    resolve_video_input,
    safe_filename,
)


# Bilibili uses this permutation to derive the WBI signing key.  Keeping the
# fallback on the signed playback API avoids the public webpage endpoint, which
# is frequently rejected with HTTP 412 on automated/network-proxied requests.
_WBI_MIXIN_KEY_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


def _get_wbi_mixin_key(wbi_img):
    """Build the temporary WBI key returned by Bilibili's navigation API."""
    img_key = os.path.splitext(os.path.basename(urlparse(wbi_img["img_url"]).path))[0]
    sub_key = os.path.splitext(os.path.basename(urlparse(wbi_img["sub_url"]).path))[0]
    source_key = img_key + sub_key
    return "".join(source_key[index] for index in _WBI_MIXIN_KEY_TABLE)[:32]


def _sign_wbi_params(params, mixin_key):
    """Return URL-encoded, signed query parameters for a WBI endpoint."""
    signed_params = dict(params)
    signed_params["wts"] = int(time.time())
    signed_params = {
        key: re.sub(r"[!\\'()*]", "", str(value))
        for key, value in sorted(signed_params.items())
    }
    query = urlencode(signed_params)
    return f"{query}&w_rid={hashlib.md5((query + mixin_key).encode()).hexdigest()}"


def _get_with_retries(session, url, *, attempts=3, **kwargs):
    """Retry transient proxy/CDN failures without hiding the final error."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as error:
            last_error = error
            if attempt < attempts:
                print(f"连接失败，正在重试（{attempt}/{attempts - 1}）...")
                time.sleep(attempt)
    raise last_error


def _download_media_from_urls(session, media_urls, headers, temporary_path, expected_size):
    """Try the playback CDNs, resuming a partial response on the next CDN."""
    downloaded_size = 0
    last_error = None
    for index, media_url in enumerate(media_urls, start=1):
        request_headers = dict(headers)
        if downloaded_size:
            request_headers["Range"] = f"bytes={downloaded_size}-"
        print(f"连接视频线路 {index}/{len(media_urls)}，已下载 {downloaded_size / 1048576:.1f} MB...")
        try:
            response = _get_with_retries(
                session,
                media_url,
                attempts=1,
                headers=request_headers,
                stream=True,
                timeout=(15, 20),
            )
            with response:
                if downloaded_size:
                    content_range = response.headers.get("Content-Range", "")
                    if response.status_code == 200:
                        # This CDN ignored Range and sent the whole file.
                        print("该线路不支持断点续传，从头下载...")
                        downloaded_size = 0
                    elif response.status_code != 206 or not content_range.startswith(
                        f"bytes {downloaded_size}-"
                    ):
                        raise RuntimeError(f"备用线路返回了无效的续传范围: {content_range}")
                mode = "ab" if downloaded_size else "wb"
                last_report_size = downloaded_size
                last_report_time = time.monotonic()
                with open(temporary_path, mode) as media_file:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        media_file.write(chunk)
                        downloaded_size += len(chunk)
                        now = time.monotonic()
                        if (downloaded_size - last_report_size >= 5 * 1048576
                                or now - last_report_time >= 15):
                            progress = (
                                f"{downloaded_size / 1048576:.1f}/{expected_size / 1048576:.1f} MB"
                                if expected_size else f"{downloaded_size / 1048576:.1f} MB"
                            )
                            print(f"视频下载进度: {progress}")
                            last_report_size = downloaded_size
                            last_report_time = now
            if expected_size and downloaded_size != expected_size:
                raise RuntimeError(
                    f"视频未完整下载（{downloaded_size}/{expected_size} 字节）"
                )
            return
        except (requests.RequestException, OSError, RuntimeError) as error:
            last_error = error
            if expected_size and downloaded_size == expected_size:
                # Some servers close the connection abruptly after sending
                # the final byte; the size check still confirms completion.
                return
            if index < len(media_urls):
                print(f"视频线路 {index} 中断（已下载 {downloaded_size / 1048576:.1f} MB），切换备用线路...")
    raise RuntimeError(f"所有B站视频线路均下载失败: {last_error}")


def _download_bilibili_via_wbi(file_id, output_dir, *, direct=True, page=None):
    """Download one specific Bilibili page through the signed playback endpoint.

    yt-dlp remains the preferred path.  This fallback is deliberately limited
    to a single video page because the rest of the application expects one
    media file for a submitted link.  ``page`` keeps ``?p=`` selections intact
    instead of silently falling back to the first page of a multi-part video.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/140.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    session = requests.Session()
    # This host's configured proxy returns 412 for Bilibili and can interrupt
    # CDN transfers.  requests otherwise picks that proxy up automatically.
    session.trust_env = not direct
    print("WBI接口使用直连网络..." if direct else "WBI接口尝试系统代理...")

    print("正在获取B站视频分P信息...")
    page_response = _get_with_retries(
        session,
        "https://api.bilibili.com/x/player/pagelist",
        params={"bvid": file_id},
        headers=headers,
        timeout=15,
    )
    page_response.raise_for_status()
    page_data = page_response.json()
    pages = page_data.get("data") or []
    if page_data.get("code") != 0 or not pages:
        raise RuntimeError(f"获取视频分P信息失败: {page_data.get('message', '无可下载分P')}")

    page_number = page or 1
    if page_number > len(pages):
        raise RuntimeError(
            f"该视频共 {len(pages)} 个分P，第 {page_number} P 不存在，请检查链接中的 p 参数。"
        )
    target_page = pages[page_number - 1]
    if page_number > 1:
        print(f"目标分P：第 {page_number} P（cid={target_page.get('cid')}）")

    print("正在获取B站播放接口签名...")
    nav_response = _get_with_retries(
        session,
        "https://api.bilibili.com/x/web-interface/nav",
        headers=headers,
        timeout=15,
    )
    nav_response.raise_for_status()
    wbi_img = (nav_response.json().get("data") or {}).get("wbi_img")
    if not wbi_img:
        raise RuntimeError("无法获取B站播放接口签名信息")

    mixin_key = _get_wbi_mixin_key(wbi_img)
    print("正在获取视频播放地址...")
    query = _sign_wbi_params(
        {
            "bvid": file_id,
            "cid": target_page["cid"],
            "qn": 64,  # 720P; broadly available without requiring an account
            "fnval": 0,  # request one directly playable MP4 instead of DASH tracks
            "fourk": 0,
        },
        mixin_key,
    )
    play_response = _get_with_retries(
        session,
        f"https://api.bilibili.com/x/player/wbi/playurl?{query}",
        headers=headers,
        timeout=15,
    )
    play_response.raise_for_status()
    play_data = play_response.json()
    durls = (play_data.get("data") or {}).get("durl") or []
    if play_data.get("code") != 0 or not durls:
        raise RuntimeError(f"获取视频播放地址失败: {play_data.get('message', '未返回MP4地址')}")
    if len(durls) != 1:
        raise RuntimeError("该视频返回多个媒体分段，暂不支持兜底直连下载")

    title = target_page.get("part") or file_id
    if page_number > 1:
        title = f"{title}_P{page_number}"
    safe_title = safe_filename(title, fallback=file_id)
    output_path = os.path.join(output_dir, f"{safe_title}.mp4")
    temporary_path = f"{output_path}.part"
    expected_size = durls[0].get("size")
    media_urls = [durls[0]["url"], *durls[0].get("backup_url", [])]
    if expected_size:
        print(f"视频大小约 {expected_size / 1048576:.1f} MB，开始下载...")
    _download_media_from_urls(session, media_urls, headers, temporary_path, expected_size)
    os.replace(temporary_path, output_path)

    print(f"WBI直链下载成功: {output_path}")
    return file_id

def ensure_folders_exist(output_dir):
    if not os.path.exists("bilibili_video"):
        os.makedirs("bilibili_video")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
    if not os.path.exists("outputs"):
        os.makedirs("outputs")


def _has_complete_video(output_dir):
    """Reuse an existing video only after FFmpeg decodes it without errors."""
    for extension in ("mp4", "mkv", "flv", "webm"):
        for path in sorted(glob.glob(os.path.join(output_dir, f"*.{extension}"))):
            print(f"检查已有视频是否完整: {path}")
            try:
                result = subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            except FileNotFoundError:
                print("未找到FFmpeg，无法校验已有视频，将重新下载。")
                return False
            if result.returncode == 0 and not result.stderr.strip():
                print(f"复用已下载的视频: {path}")
                return True
            print(f"已有视频不完整，将重新下载: {path}")
    return False

_UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_YTDLP_COMMAND = None


def _ytdlp_command():
    """优先用 PATH 里的 yt-dlp；找不到时退回当前解释器的 yt_dlp 模块。"""
    global _YTDLP_COMMAND
    if _YTDLP_COMMAND:
        return _YTDLP_COMMAND
    executable = shutil.which("yt-dlp")
    if executable:
        _YTDLP_COMMAND = [executable]
    else:
        _YTDLP_COMMAND = [sys.executable, "-m", "yt_dlp"]
    return _YTDLP_COMMAND


def _expand_short_link(url):
    """跟随 302 跳转拿到真实视频页地址；失败返回 None，交由 yt-dlp 自行处理。"""
    last_error = None
    for direct in (True, False):
        session = requests.Session()
        session.trust_env = not direct
        try:
            response = session.get(
                url,
                allow_redirects=True,
                timeout=15,
                headers={"User-Agent": _UA_DESKTOP, "Accept-Language": "zh-CN,zh;q=0.9"},
            )
            response.raise_for_status()
            return response.url or None
        except requests.RequestException as error:
            last_error = error
        finally:
            session.close()
    print(f"短链接解析失败，将直接交给下载器处理: {last_error}")
    return None


def _explain_download_failure(raw_output):
    """把 yt-dlp 的英文错误翻译成用户能看懂的中文提示。"""
    text = (raw_output or "").lower()
    if "unsupported url" in text or "no suitable extractor" in text:
        return "暂不支持该站点，请确认链接是否来自 B站 / 抖音 / YouTube。"
    if "private" in text:
        return "该视频为私密或仅好友可见，无法下载。"
    if "404" in text or "not found" in text or "does not exist" in text:
        return "视频不存在、已被删除，或链接已失效。"
    if "403" in text or "forbidden" in text or "login" in text or "cookies" in text:
        return "平台拒绝了访问（可能需要登录或存在访问限制），本工具不会绕过权限。"
    if "timed out" in text or "timeout" in text or "connection" in text:
        return "网络连接超时，请检查网络后重试。"
    if "http error 412" in text:
        return "平台风控拦截（HTTP 412），请稍后重试。"
    return "下载失败，请检查链接是否有效、网络是否正常。"


def _ytdlp_cookie_arguments():
    """可选：使用本机浏览器已有的登录会话下载。

    抖音等平台会要求 "fresh cookies"，即使视频是公开的。这里只是把用户
    自己浏览器里本来就能正常观看的会话交给 yt-dlp，不涉及任何绕过登录、
    付费或隐私限制的行为；不配置时完全不启用。

    环境变量：
        YTDLP_COOKIES_FROM_BROWSER=chrome|edge|firefox|brave
        YTDLP_COOKIE_FILE=<Netscape 格式 cookies.txt 路径>
    """
    cookie_file = (os.getenv("YTDLP_COOKIE_FILE") or "").strip()
    if cookie_file and os.path.exists(cookie_file):
        return ["--cookies", cookie_file]
    browser = (os.getenv("YTDLP_COOKIES_FROM_BROWSER") or "").strip()
    if browser:
        return ["--cookies-from-browser", browser]
    return []


def _download_with_ytdlp(ref, output_dir):
    """调用 yt-dlp 下载单个视频；成功返回 True。"""
    output_template = os.path.join(output_dir, "%(title)s.%(ext)s")
    arguments = [
        "-o", output_template,
        "--no-playlist",
        "--no-mtime",
        "--newline",
        "--progress-delta", "1",
    ]

    if ref.platform == "bilibili":
        arguments += [
            "--proxy", "",  # 直连，避免代理导致的 412 / SSL 错误
            "--socket-timeout", "20",
            "--referer", "https://www.bilibili.com",
            "--add-header", f"User-Agent: {_UA_DESKTOP}",
        ]
    elif ref.platform == "youtube":
        arguments += [
            "--add-header", f"User-Agent: {_UA_DESKTOP}",
            "--add-header", "Accept-Language: en-US,en;q=0.5",
            "--referer", "https://www.youtube.com/",
            "--extractor-args", "youtube:player_client=android",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--merge-output-format", "mp4",
        ]
    elif ref.platform == "douyin":
        arguments += [
            "--socket-timeout", "20",
            "--referer", "https://www.douyin.com/",
            "--add-header", f"User-Agent: {_UA_DESKTOP}",
            "--add-header", "Accept-Language: zh-CN,zh;q=0.9",
            "-f", "mp4/best",
            "--merge-output-format", "mp4",
        ]
        # 抖音通常要求 fresh cookies；仅在用户显式配置了浏览器/文件时启用
        arguments += _ytdlp_cookie_arguments()

    arguments.append(ref.canonical_url)

    print(f"使用 yt-dlp 下载: {ref.canonical_url}")
    output_lines = []
    try:
        process = subprocess.Popen(
            [*_ytdlp_command(), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except FileNotFoundError:
        print("错误: 未找到 yt-dlp，请先执行 pip install yt-dlp")
        return False

    with process:
        for line in iter(process.stdout.readline, ""):
            line = line.rstrip("\n")
            if not line:
                continue
            print(line)
            output_lines.append(line)
            if len(output_lines) > 60:
                output_lines.pop(0)
        process.stdout.close()
        returncode = process.wait()

    if returncode == 0:
        for extension in ("mp4", "mkv", "flv", "webm", "m4a"):
            if glob.glob(os.path.join(output_dir, f"*.{extension}")):
                print(f"视频已保存到目录: {output_dir}")
                for extra in glob.glob(os.path.join(output_dir, "*.xml")):
                    try:
                        os.remove(extra)
                    except OSError:
                        pass
                return True
        print(f"下载进程结束但未找到视频文件: {output_dir}")

    print(f"yt-dlp 下载失败（返回码 {returncode}）：{_explain_download_failure(chr(10).join(output_lines))}")
    return False


def _prepare_local_media(ref, output_dir, notify):
    """把本地媒体文件接入缓存目录；同盘用硬链接，跨盘或失败时复制。

    这样后续「切音 → 转写」流程与下载来的视频完全一致，也不改动原始文件。
    """
    source = ref.canonical_url
    if not os.path.isfile(source):
        print(f"本地文件不存在或已被移动: {source}")
        return False

    os.makedirs(output_dir, exist_ok=True)
    destination = os.path.join(output_dir, os.path.basename(source))
    source_size = os.path.getsize(source)
    if os.path.exists(destination) and os.path.getsize(destination) == source_size:
        print(f"复用已导入的本地文件: {destination}")
        return True

    notify("正在导入本地文件...")
    try:
        os.link(source, destination)
        print(f"已硬链接本地文件（不占用额外空间）: {destination}")
    except OSError:
        shutil.copy2(source, destination)
        print(f"已复制本地文件: {destination}")
    return True


def download_video(link_or_bv, progress=None):
    """下载视频（B站 / 抖音 / YouTube / 其他 yt-dlp 支持的站点）。

    参数:
        link_or_bv: 视频链接、分享文案或 BV 号
        progress: 可选回调，接收阶段描述字符串（供 UI 展示）
    返回:
        成功时返回缓存目录名（用于后续音频处理），失败返回 None
    """
    notify = progress or (lambda _message: None)

    try:
        ref = resolve_video_input(str(link_or_bv))
    except UnsupportedLinkError as error:
        print(f"链接解析失败: {error}")
        return None

    if ref.platform == "local":
        notify(f"已选择本地文件：{os.path.basename(ref.canonical_url)}")
        output_dir = f"bilibili_video/{ref.cache_key}"
        ensure_folders_exist(output_dir)
        if _has_complete_video(output_dir):
            print(f"复用已导入的本地视频: {output_dir}")
            return ref.cache_key
        if _prepare_local_media(ref, output_dir, notify):
            return ref.cache_key
        return None

    if is_short_link(ref.canonical_url):
        notify("正在解析短链接...")
        expanded = _expand_short_link(ref.canonical_url)
        if expanded and expanded != ref.canonical_url:
            try:
                ref = resolve_video_input(expanded)
            except UnsupportedLinkError as error:
                print(f"短链跳转后无法识别: {error}")
                return None
        # 展开后仍是占位 ID，说明短链失效或跳到了首页，不能把首页当视频下载
        if ref.video_id.startswith("short_"):
            print("短链未能解析出具体的视频 ID：链接可能已失效，或跳转到了非视频页面。")
            print("请改用视频详情页链接后重试；本工具不会绕过登录或访问限制。")
            return None

    print(f"识别结果: {ref.display}")
    notify(f"识别到{ref.display}")

    output_dir = f"bilibili_video/{ref.cache_key}"
    ensure_folders_exist(output_dir)

    if ref.page and ref.page > 1:
        print(f"已保留分P参数：第 {ref.page} P，缓存目录 {output_dir}")

    if _has_complete_video(output_dir):
        print(f"复用已缓存视频: {output_dir}")
        return ref.cache_key

    if _download_with_ytdlp(ref, output_dir):
        return ref.cache_key

    if ref.platform == "bilibili" and not ref.video_id.startswith("short_"):
        print("yt-dlp 未能下载，切换到 WBI 播放接口直链下载...")
        notify("切换到备用下载线路...")
        for direct in (True, False):
            try:
                return _download_bilibili_via_wbi(
                    ref.video_id, output_dir, direct=direct, page=ref.page
                )
            except Exception as error:  # noqa: BLE001 - 需要展示具体原因
                print("WBI 直链下载失败:", str(error))
        return None

    if ref.platform == "douyin":
        print("抖音视频下载失败：请确认视频为公开可访问状态，且链接未过期。")
        # 2026-09 起抖音风控会拒绝命令行直连（详情接口返回 403），
        # 仅靠 cookies 往往无法解决，因此给出立刻可用的兜底方案。
        print("说明：抖音自 2026 年起对非浏览器访问收紧风控，即使带上浏览器 cookies")
        print("      也可能返回 403；这不是链接或配置错误。")
        if not _ytdlp_cookie_arguments():
            print("      可尝试设置 YTDLP_COOKIES_FROM_BROWSER=edge 或")
            print("      YTDLP_COOKIE_FILE=<cookies.txt 路径> 后重试。")
        print("建议：在浏览器里正常播放并下载该视频，然后点击「选择文件…」")
        print("      或直接输入本地文件路径，用本地文件方式转写。")
        print("本工具不会绕过登录、付费或隐私限制。")
    return None
