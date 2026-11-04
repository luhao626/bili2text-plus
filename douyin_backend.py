"""抖音独立下载后端。

背景：2026-09 起抖音风控拒绝常规 yt-dlp 的命令行直连（详情接口 HTTP 403）。
yt-dlp 上游修复 PR #17826（仓库 viking1304/yt-dlp，提交
728fe46fbaa97d6f03efcaf9e766d4db63338b8d，截至本文件编写时尚未正式合并）
已在本机完成隔离验证。本模块用**独立 Python 环境**里的修复版 yt-dlp
执行抖音下载，与项目主环境及其 yt-dlp 版本完全隔离：
主环境的 yt-dlp 不被替换、不降级，B站 / YouTube 下载路径不受影响。

职责边界：
1. 只接受 platforms.py 已经解析出的抖音 VideoRef（URL + video_id）；
2. 自行复核 URL 协议与域名白名单，拒绝任何非抖音官方域名；
3. 通过 ``subprocess`` 参数数组调用独立环境的 yt-dlp，绝不拼接 shell 命令；
4. 下载到现有任务缓存目录，不覆盖已有缓存文件；
5. 校验产物存在、非空且位于缓存目录内，失败时给出明确错误。

本模块不读取浏览器 Cookie 数据库，不索取任何账号凭据。
"""

from __future__ import annotations

import os
import subprocess
from urllib.parse import urlparse

import platforms

# 抖音官方域名白名单（与 platforms.DOUYIN_HOSTS 保持一致）
DOUYIN_ALLOWED_HOSTS = platforms.DOUYIN_HOSTS

# 修复补丁对应的 yt-dlp 提交（PR #17826）
FIX_COMMIT = "728fe46fbaa97d6f03efcaf9e766d4db63338b8d"

# 独立环境解释器：环境变量优先，其次默认安装位置
FIX_PYTHON_ENV_VAR = "DOUYIN_YTDLP_PYTHON"
_DEFAULT_FIX_DIR_NAME = "douyin-yt-dlp-728fe46-py313"

# 下载产物允许的扩展名（与 utils 现有判定保持一致）
_VIDEO_EXTS = ("mp4", "mkv", "flv", "webm", "m4a")

_UA_DESKTOP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 可选 Cookie：与主下载器相同的两个环境变量，仅显式配置时启用
_COOKIE_ENV_VARS = ("YTDLP_COOKIES_FROM_BROWSER", "YTDLP_COOKIE_FILE")


class DouyinDownloadError(RuntimeError):
    """抖音下载失败，message 面向用户、可直接展示。"""


def default_fix_python_path() -> str:
    """默认隔离环境解释器路径（可能不存在，调用方需检查）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(
        base, "Bili2TextPlus", _DEFAULT_FIX_DIR_NAME, "Scripts", "python.exe"
    )


def resolve_fix_python() -> str | None:
    """定位独立环境解释器；找不到返回 None。"""
    candidate = (os.environ.get(FIX_PYTHON_ENV_VAR) or "").strip()
    if candidate and os.path.isfile(candidate):
        return candidate
    default_path = default_fix_python_path()
    if os.path.isfile(default_path):
        return default_path
    return None


def install_hint() -> str:
    """独立环境缺失时给用户的安装指引。"""
    default_path = default_fix_python_path()
    env_dir = os.path.dirname(os.path.dirname(default_path))
    return (
        "未找到抖音专用 yt-dlp 修复环境。请先在 PowerShell 中执行：\n"
        f"  py -3.13 -m venv \"{env_dir}\"\n"
        f"  & \"{default_path}\" -m pip install "
        f"\"yt-dlp @ git+https://github.com/viking1304/yt-dlp.git@{FIX_COMMIT}\"\n"
        f"或设置环境变量 {FIX_PYTHON_ENV_VAR} 指向已装好的 python.exe。"
    )


def validate_douyin_url(url: str) -> str:
    """复核抖音 URL：必须 HTTPS 且主机属于官方域名白名单。"""
    parsed = urlparse(url or "")
    host = (parsed.netloc or "").lower()
    if parsed.scheme != "https":
        raise DouyinDownloadError(f"抖音下载仅接受 HTTPS 链接，收到：{url}")
    if not host:
        raise DouyinDownloadError(f"链接中没有主机名，无法下载：{url}")
    for base in DOUYIN_ALLOWED_HOSTS:
        if host == base or host.endswith("." + base):
            return url
    raise DouyinDownloadError(f"域名 {host} 不在抖音官方域名白名单内，已拒绝下载。")


def _cookie_arguments() -> list[str]:
    """可选 Cookie 参数；仅当用户显式配置了对应环境变量时启用。"""
    arguments: list[str] = []
    browser = (os.environ.get("YTDLP_COOKIES_FROM_BROWSER") or "").strip()
    cookie_file = (os.environ.get("YTDLP_COOKIE_FILE") or "").strip()
    if browser:
        arguments += ["--cookies-from-browser", browser]
    if cookie_file and os.path.isfile(cookie_file):
        arguments += ["--cookies", cookie_file]
    return arguments


def _find_existing_video(output_dir: str, video_id: str) -> str | None:
    """在缓存目录中查找该视频已有的非空产物；命中则复用、不重复下载。"""
    for extension in _VIDEO_EXTS:
        candidate = os.path.join(output_dir, f"{video_id}.{extension}")
        if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
            return candidate
    return None


def _validate_output_path(path: str, output_dir: str) -> str:
    """校验产物存在、非空，且真实路径位于缓存目录内（防路径逃逸）。"""
    real_dir = os.path.realpath(output_dir)
    real_path = os.path.realpath(path)
    if os.path.dirname(real_path) != real_dir and not real_path.startswith(
        real_dir + os.sep
    ):
        raise DouyinDownloadError(f"下载产物路径异常（逃逸出缓存目录）：{path}")
    if not os.path.isfile(path):
        raise DouyinDownloadError(f"下载进程结束但未找到视频文件：{path}")
    if os.path.getsize(path) <= 0:
        raise DouyinDownloadError(f"下载产物为空文件：{path}")
    return path


def download_douyin_video(ref, output_dir: str, progress=None) -> str:
    """用隔离环境的修复版 yt-dlp 下载抖音视频。

    参数:
        ref: platforms.resolve_video_input 解析出的抖音 VideoRef
        output_dir: 现有任务缓存目录（bilibili_video/<cache_key>）
        progress: 可选回调，接收阶段描述字符串（供 UI 展示）
    返回:
        实际下载（或复用）的视频文件绝对路径
    异常:
        DouyinDownloadError: 环境缺失 / URL 非法 / 下载失败 / 产物校验不通过
    """
    notify = progress or (lambda _message: None)

    if getattr(ref, "platform", "") != "douyin":
        raise DouyinDownloadError("douyin_backend 只处理抖音视频链接。")
    if not getattr(ref, "video_id", ""):
        raise DouyinDownloadError("链接中没有视频 ID，无法下载抖音视频。")
    validate_douyin_url(ref.canonical_url)

    fix_python = resolve_fix_python()
    if fix_python is None:
        raise DouyinDownloadError(install_hint())

    os.makedirs(output_dir, exist_ok=True)

    existing = _find_existing_video(output_dir, ref.video_id)
    if existing:
        print(f"复用已缓存的抖音视频: {existing}")
        notify("复用已缓存的抖音视频")
        return _validate_output_path(existing, output_dir)

    # 输出模板固定为 视频ID.扩展名：可预测、便于校验路径归属
    output_template = os.path.join(output_dir, "%(id)s.%(ext)s")

    # 抖音为境内站点，默认强制直连（--proxy ""）；
    # 如个别网络确需代理，可设置 DOUYIN_YTDLP_PROXY 覆盖。
    proxy = (os.environ.get("DOUYIN_YTDLP_PROXY") or "").strip()

    arguments = [
        fix_python,
        "-m", "yt_dlp",
        "--no-playlist",
        "--no-mtime",
        "--no-overwrites",
        "--newline",
        "--socket-timeout", "20",
        "--referer", "https://www.douyin.com/",
        "--add-header", f"User-Agent: {_UA_DESKTOP}",
        "--add-header", "Accept-Language: zh-CN,zh;q=0.9",
        "-f", "mp4/best",
        "--merge-output-format", "mp4",
        "--proxy", proxy,
        "-o", output_template,
        ref.canonical_url,
    ] + _cookie_arguments()

    print(f"使用修复版 yt-dlp（独立环境）下载抖音视频: {ref.canonical_url}")
    notify("正在下载抖音视频…")

    try:
        process = subprocess.Popen(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
        )
    except FileNotFoundError as error:
        raise DouyinDownloadError(
            f"无法启动独立环境解释器：{fix_python}（{error}）"
        ) from error

    output_lines: list[str] = []
    with process:
        for line in iter(process.stdout.readline, ""):
            line = line.rstrip("\n")
            if not line:
                continue
            print(line)
            output_lines.append(line)
            if len(output_lines) > 80:
                output_lines.pop(0)
        process.stdout.close()
        returncode = process.wait()

    if returncode != 0:
        tail = "\n".join(output_lines[-10:])
        raise DouyinDownloadError(
            f"修复版 yt-dlp 下载失败（返回码 {returncode}）。日志末尾：\n{tail}"
        )

    downloaded = _find_existing_video(output_dir, ref.video_id)
    if downloaded is None:
        tail = "\n".join(output_lines[-5:])
        raise DouyinDownloadError(
            f"下载进程正常结束但缓存目录中没有视频文件：{output_dir}\n{tail}"
        )

    downloaded = _validate_output_path(downloaded, output_dir)
    size_mb = os.path.getsize(downloaded) / (1024 * 1024)
    print(f"抖音视频下载成功: {downloaded}（{size_mb:.2f} MB）")
    notify("下载成功，正在提取音频")
    return downloaded
