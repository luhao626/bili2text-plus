"""V2 端到端验收工具（开发者用）。

用法示例：
    python tools/v2_e2e.py --url "https://www.bilibili.com/video/BV1vwME6AEpd?p=33" --mode smart
    python tools/v2_e2e.py --url "https://youtu.be/xxx" --mode subtitle-only --notes
    python tools/v2_e2e.py --legacy "outputs/20261031160632.md" --notes

行为与桌面界面的 worker 流程一致：
1. 按模式解析字幕（智能 / 强制 Whisper / 仅字幕）；
2. 需要时下载视频 → 切音 → Whisper；
3. 输出到 ``outputs/<task_id>/``（transcript.md / transcript.json / task_manifest.json）；
4. ``--notes`` 时用当前 LLM 配置生成 study_notes.md。

本工具不读取、不打印任何密钥，只用进程环境变量里的 LLM 配置。
"""

from __future__ import annotations

import argparse
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import legacy_markdown  # noqa: E402
import llm_config  # noqa: E402
import pipeline  # noqa: E402
import study_notes  # noqa: E402
import subtitles  # noqa: E402
from platforms import resolve_video_input  # noqa: E402
from transcript import SOURCE_WHISPER, TIME_APPROXIMATE  # noqa: E402


def _log(message: str) -> None:
    print(message, flush=True)


def run_subtitle(document, ref, task_id, mode, notes, paths):
    manifest = pipeline.manifest_from_document(document, task_id=task_id, mode=mode)
    pipeline.write_transcript(document, paths, manifest)
    _log(f"[输出] {paths.transcript_md}")
    _log(f"[输出] {paths.transcript_json}")
    _log(f"[来源] {document.source_type} / {document.language} / {len(document.segments)} 段")
    return manifest


def run_whisper(url, ref, task_id, mode, notes, paths, whisper_model, fallback_reason,
                video_title="", duration_ms=None):
    import speech2text
    from exAudio import process_audio_split
    from utils import download_video

    _log("[步骤] 下载视频")
    cache_key = download_video(url, progress=lambda message: _log(f"  {message}"))
    if cache_key is None:
        raise SystemExit("下载失败，无法继续真实测试")
    _log(f"[步骤] 视频缓存：bilibili_video/{cache_key}")

    _log("[步骤] 提取并切分音频")
    folder_name = process_audio_split(cache_key)
    _log(f"[步骤] 音频切片目录：audio/slice/{folder_name}")

    _log(f"[步骤] Whisper 转写（{whisper_model}）")
    speech2text.set_output_dir("outputs")
    speech2text.load_whisper(model=whisper_model)
    timeline = speech2text.run_analysis_timeline(
        folder_name, prompt=f"以下是普通话的句子。这是一个关于{cache_key}的视频。"
    )
    cues = [(item["start_ms"], item["end_ms"], item["text"]) for item in timeline if item["text"].strip()]
    if not cues:
        raise SystemExit("转写结果为空")
    document = subtitles.build_document(
        cues,
        source_type=SOURCE_WHISPER,
        language="zh",
        platform=ref.platform,
        video_id=ref.video_id,
        title=video_title or ref.title_hint or ref.video_id,
        url=ref.canonical_url,
        page=ref.page,
        duration_ms=duration_ms,
        precision=TIME_APPROXIMATE,
        provenance={
            "slice_ms": 45000,
            "time_note": "按 45 秒切片估算的近似位置，非逐句精确时间",
            "fallback_reason": fallback_reason,
        },
    )
    manifest = pipeline.manifest_from_document(
        document, task_id=task_id, mode=mode, fallback_reason=fallback_reason, whisper_model=whisper_model
    )
    pipeline.write_transcript(document, paths, manifest)
    _log(f"[输出] {paths.transcript_md}")
    _log(f"[输出] {paths.transcript_json}")
    _log(f"[来源] {document.source_type} / {len(document.segments)} 段 / 精度 approximate")
    _log(f"[V1 兼容] outputs/{folder_name}.md 由 run_analysis_timeline 同步写出")
    return document, manifest


def generate_notes(document, paths, manifest):
    settings = llm_config.load_settings()
    client = llm_config.build_client(settings)
    if client is None:
        _log("[跳过] 未配置 LLM 密钥，无法生成笔记")
        return None
    _log(f"[步骤] 生成智能笔记（{settings.model}）")
    notes = study_notes.generate_study_notes(
        document, client, settings, title=document.title, progress=lambda m: _log(f"  {m}")
    )
    notes_path = study_notes.write_study_notes(notes, document, paths.notes_md)
    manifest.llm_provider = settings.provider_label
    manifest.llm_model = settings.model
    pipeline.write_notes(notes_path, paths, manifest, notes.complete)
    _log(f"[输出] {notes_path}")
    _log(
        f"[笔记] 章节 {len(notes.chapters)} / 知识点 {len(notes.key_points)} / "
        f"丢弃引用 {notes.dropped_refs} / 完整={notes.complete}"
    )
    for warning in notes.warnings:
        _log(f"[警告] {warning}")
    return notes_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Bili2Text Plus V2 端到端验收")
    parser.add_argument("--url", help="视频链接或本地文件路径")
    parser.add_argument("--legacy", help="旧 Markdown 路径（只读）")
    parser.add_argument("--notes-only", help="复用已有任务目录，仅重新生成笔记")
    parser.add_argument(
        "--mode", default="smart", choices=["smart", "whisper", "subtitle-only"], help="文字获取方式"
    )
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument("--notes", action="store_true", help="生成智能笔记")
    parser.add_argument("--output-root", default="outputs")
    args = parser.parse_args()

    mode = {
        "smart": subtitles.MODE_SMART,
        "whisper": subtitles.MODE_WHISPER_ONLY,
        "subtitle-only": subtitles.MODE_SUBTITLE_ONLY,
    }[args.mode]

    if args.notes_only:
        paths = pipeline.TaskPaths(args.notes_only).ensure()
        document = pipeline.load_document(paths.transcript_json)
        manifest_path = paths.manifest_json
        if os.path.exists(manifest_path):
            manifest = pipeline.load_manifest(manifest_path)
        else:
            manifest = pipeline.manifest_from_document(document, task_id=os.path.basename(paths.root))
        _log(f"[复用] {len(document.segments)} 段 / 来源 {document.source_type}")
        generate_notes(document, paths, manifest)
        _log(f"[任务目录] {os.path.abspath(paths.root)}")
        return 0

    if args.legacy:
        document = legacy_markdown.load_legacy_markdown(args.legacy)
        task_id = pipeline.new_task_id(os.path.splitext(os.path.basename(args.legacy))[0], prefix="legacy_")
        paths = pipeline.TaskPaths(os.path.join(args.output_root, task_id)).ensure()
        manifest = pipeline.manifest_from_document(document, task_id=task_id, mode="legacy_markdown")
        document.write_json(paths.transcript_json)
        manifest.files = [pipeline.TRANSCRIPT_JSON]
        manifest.write(paths)
        _log(f"[旧文档] {len(document.segments)} 段，时间精度 unknown，原文件未修改")
        _log(f"[输出] {paths.transcript_json}")
        if args.notes:
            generate_notes(document, paths, manifest)
        _log(f"[任务目录] {os.path.abspath(paths.root)}")
        return 0

    if not args.url:
        parser.error("需要 --url 或 --legacy")

    ref = resolve_video_input(args.url)
    _log(f"[输入] {ref.display} | 缓存键 {ref.cache_key}")

    task_id = pipeline.new_task_id(ref.cache_key)
    paths = pipeline.TaskPaths(os.path.join(args.output_root, task_id)).ensure()
    _log(f"[任务] {os.path.abspath(paths.root)}")

    document = None
    fallback_reason = ""
    selection = None
    if ref.platform != "local" and mode != subtitles.MODE_WHISPER_ONLY:
        _log("[步骤] 查找字幕")
        try:
            selection = subtitles.resolve_subtitles(ref, mode=mode)
        except subtitles.SubtitleError as error:
            _log(f"[失败] {error}")
            return 2
        fallback_reason = selection.fallback_reason
        if selection.document is not None:
            _log(f"[字幕] {selection.reason}")
            manifest = run_subtitle(selection.document, ref, task_id, mode, args.notes, paths)
            document = selection.document
            if args.notes:
                generate_notes(document, paths, manifest)
            _log(f"[任务目录] {os.path.abspath(paths.root)}")
            return 0
        _log(f"[字幕] {fallback_reason or '未使用字幕'}")
    elif ref.platform == "local":
        fallback_reason = "本地文件默认使用 Whisper 转写"

    document, manifest = run_whisper(
        args.url, ref, task_id, mode, args.notes, paths, args.whisper_model, fallback_reason,
        video_title=selection.video_title if selection else "",
        duration_ms=selection.duration_ms if selection else None,
    )
    if args.notes:
        generate_notes(document, paths, manifest)
    _log(f"[任务目录] {os.path.abspath(paths.root)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
