"""V2 任务输出层：独立任务目录、manifest 与安全写入。

V2 的输出结构（与 V1 的 ``outputs/*.md`` 互不覆盖）：

    outputs/<task_id>/
        transcript.md        原始字幕或 Whisper 转写
        transcript.json      可溯源时间轴（事实来源）
        study_notes.md       只有「章节结构」「核心知识点」两个栏目
        task_manifest.json   任务状态、模型、来源、输出文件（**不含任何密钥**）

设计约束：
- 任务目录一次性创建，重跑使用新的 task_id，避免覆盖已有成果；
- 所有正式文件都通过"临时文件 + 原子替换"落盘；
- manifest 只记录模型名与服务商，不写入 API Key。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from platforms import safe_filename
from transcript import (
    SOURCE_LABELS,
    TranscriptDocument,
    atomic_write_json,
    atomic_write_text,
)

TRANSCRIPT_MD = "transcript.md"
TRANSCRIPT_JSON = "transcript.json"
NOTES_MD = "study_notes.md"
MANIFEST_JSON = "task_manifest.json"


def new_task_id(key: str, *, prefix: str = "") -> str:
    """``20261031171530_BV1vwME6AEpd_p33`` —— 时间戳保证多次运行不互相覆盖。"""
    stamp = time.strftime("%Y%m%d%H%M%S")
    suffix = safe_filename(key or "task", max_length=48, fallback="task")
    return f"{prefix}{stamp}_{suffix}" if prefix else f"{stamp}_{suffix}"


@dataclass
class TaskPaths:
    root: str

    @property
    def transcript_md(self) -> str:
        return os.path.join(self.root, TRANSCRIPT_MD)

    @property
    def transcript_json(self) -> str:
        return os.path.join(self.root, TRANSCRIPT_JSON)

    @property
    def notes_md(self) -> str:
        return os.path.join(self.root, NOTES_MD)

    @property
    def manifest_json(self) -> str:
        return os.path.join(self.root, MANIFEST_JSON)

    def ensure(self) -> "TaskPaths":
        os.makedirs(self.root, exist_ok=True)
        return self


@dataclass
class TaskManifest:
    task_id: str
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    platform: str = ""
    video_id: str = ""
    page: Optional[int] = None
    url: str = ""
    title: str = ""
    mode: str = ""
    source_type: str = ""
    source_label: str = ""
    language: str = ""
    duration_ms: Optional[int] = None
    segment_count: int = 0
    subtitle_track: dict = field(default_factory=dict)
    fallback_reason: str = ""
    whisper_model: str = ""
    llm_provider: str = ""
    llm_model: str = ""
    notes_complete: Optional[bool] = None
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = {
            "task_id": self.task_id,
            "created_at": self.created_at,
            "video": {
                "platform": self.platform,
                "video_id": self.video_id,
                "page": self.page,
                "url": self.url,
                "title": self.title,
                "duration_ms": self.duration_ms,
            },
            "transcript": {
                "mode": self.mode,
                "source_type": self.source_type,
                "source_label": self.source_label,
                "language": self.language,
                "segment_count": self.segment_count,
                "subtitle_track": self.subtitle_track,
                "fallback_reason": self.fallback_reason,
                "whisper_model": self.whisper_model,
            },
            "ai": {
                "provider": self.llm_provider,
                "model": self.llm_model,
            },
            "study_notes": {
                "generated": self.notes_complete is not None,
                "complete": self.notes_complete,
            },
            "warnings": list(self.warnings),
            "files": list(self.files),
        }
        return payload

    def write(self, paths: TaskPaths) -> str:
        return atomic_write_json(paths.manifest_json, self.to_dict())

    @classmethod
    def from_dict(cls, data: dict) -> "TaskManifest":
        video = data.get("video") or {}
        transcript = data.get("transcript") or {}
        ai = data.get("ai") or {}
        notes = data.get("study_notes") or {}
        return cls(
            task_id=str(data.get("task_id") or ""),
            created_at=str(data.get("created_at") or ""),
            platform=str(video.get("platform") or ""),
            video_id=str(video.get("video_id") or ""),
            page=video.get("page"),
            url=str(video.get("url") or ""),
            title=str(video.get("title") or ""),
            duration_ms=video.get("duration_ms"),
            mode=str(transcript.get("mode") or ""),
            source_type=str(transcript.get("source_type") or ""),
            source_label=str(transcript.get("source_label") or ""),
            language=str(transcript.get("language") or ""),
            segment_count=int(transcript.get("segment_count") or 0),
            subtitle_track=dict(transcript.get("subtitle_track") or {}),
            fallback_reason=str(transcript.get("fallback_reason") or ""),
            whisper_model=str(transcript.get("whisper_model") or ""),
            llm_provider=str(ai.get("provider") or ""),
            llm_model=str(ai.get("model") or ""),
            notes_complete=notes.get("complete"),
            warnings=list(data.get("warnings") or []),
            files=list(data.get("files") or []),
        )


def manifest_from_document(
    document: TranscriptDocument,
    *,
    task_id: str,
    mode: str = "",
    fallback_reason: str = "",
    whisper_model: str = "",
) -> TaskManifest:
    provenance = document.provenance or {}
    return TaskManifest(
        task_id=task_id,
        platform=document.platform,
        video_id=document.video_id,
        page=document.page,
        url=document.url,
        title=document.title,
        mode=mode,
        source_type=document.source_type,
        source_label=SOURCE_LABELS.get(document.source_type, document.source_type),
        language=document.language,
        duration_ms=document.duration_ms,
        segment_count=len(document.segments),
        subtitle_track={
            "language": provenance.get("track_language", ""),
            "label": provenance.get("track_label", ""),
            "evidence": provenance.get("track_evidence", ""),
            "format": provenance.get("subtitle_format", ""),
            "cid": provenance.get("cid"),
            "coverage": provenance.get("coverage", {}),
        },
        fallback_reason=fallback_reason,
        whisper_model=whisper_model,
    )


def write_transcript(
    document: TranscriptDocument, paths: TaskPaths, manifest: TaskManifest
) -> None:
    """写入 transcript.md / transcript.json，并在 manifest 里登记。"""
    paths.ensure()
    document.write_json(paths.transcript_json)
    document.write_markdown(paths.transcript_md)
    manifest.segment_count = len(document.segments)
    manifest.files = [
        name
        for name in (TRANSCRIPT_MD, TRANSCRIPT_JSON)
        if os.path.exists(os.path.join(paths.root, name))
    ]
    manifest.write(paths)


def write_notes(notes_md_path: str, paths: TaskPaths, manifest: TaskManifest, complete: bool) -> None:
    manifest.notes_complete = complete
    if notes_md_path not in manifest.files:
        manifest.files.append(NOTES_MD)
    manifest.files = [name for name in dict.fromkeys(manifest.files) if name]
    manifest.write(paths)


def load_document(path: str) -> TranscriptDocument:
    with open(path, "r", encoding="utf-8") as handle:
        return TranscriptDocument.from_dict(json.load(handle))


def load_manifest(path: str) -> TaskManifest:
    with open(path, "r", encoding="utf-8") as handle:
        return TaskManifest.from_dict(json.load(handle))


def write_text_atomic(path: str, text: str) -> str:
    return atomic_write_text(path, text)
